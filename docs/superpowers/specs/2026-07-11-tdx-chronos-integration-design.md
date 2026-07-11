# tdx-chronos Integration — A-share & ETF First-Priority Data Path

- **Date:** 2026-07-11
- **Status:** Approved design (pending plan)
- **Repo:** `/app/TradingAgents`
- **Source data warehouse:** `/app/tdx-chronos` (sibling project, parquet-based offline A-share warehouse)

## 1. Goal

Make `/app/tdx-chronos` the **first-priority** data source for A-share stocks and ETFs across every data category the trading framework currently supports (OHLCV prices, technical indicators, fundamental data, benchmark index klines, shareholder data), with auto-detection of A-share symbols and a graceful opt-in vendor registration for explicit user configuration.

Non-A-share tickers (US, Hong Kong, Europe, forex, crypto) are **not** affected; their routing is unchanged.

## 2. Why

- tdx-chronos already contains the complete offline A-share universe (~12,279 symbols: stocks, ETFs, LOFs, REITs, convertible bonds) with full historical K-line, finance, and shareholder data — no rate limits, no API keys, no network dependency.
- Currently, yfinance is the only OHLCV vendor. A-share users rely on Yahoo's `600000.SS` / `000001.SZ` quotes, which Yahoo Finance has restricted intermittently (the user-visible failure: "No data found for this date range" or stale row guard triggering).
- The codebase already has a tested `VENDOR_METHODS` plug-in surface (`dataflows/interface.py:97`) and a typed error taxonomy (`dataflows/errors.py`) — the integration can follow the existing pattern instead of inventing a new one.

## 3. Scope

### In scope

- Auto-detection + normalization of A-share symbol formats: bare 6-digit (`600000`), Yahoo-suffix (`600000.SS`, `000001.SZ`, `830799.BJ`), TDX-native (`sh600000`).
- New `tdx_chronos` vendor covering: `core_stock_apis`, `technical_indicators`, `fundamental_data`, plus benchmark `index_klines` for the reflection layer.
- A-share symbol validation against `TdxChronos.list_symbols()` (one-shot per process) so typos like `6000000.SS` don't silently fall through.
- ETF/LOF/REIT/convertible-bond handling for fundamentals — these are explicitly **not** in tdx-chronos's `tdxfin.zip` scope, so calls return an explicit marker rather than fabricating.
- Lazy / optional install — `pip install -e /app/tdx-chronos` becomes a documented prerequisite, not a hard dep that breaks clean-import smoke.

### Out of scope

- News data (`get_news`, `get_global_news`, `get_insider_transactions`): tdx-chronos has no news source; these keep their current vendors (yfinance / alpha_vantage).
- Macro data (`get_macro_indicators`) and prediction markets (`get_prediction_markets`): already domain-specific to FRED / Polymarket.
- Vendor-specific reasoning/thinking knobs: provider-level, not data-source-level.
- Replacing the benchmark map's `"^NSEI"`, `"^FTSE"` etc. — only the SSE/SZSE entries get routed through tdx-chronos's `index_klines()`.

## 4. Architecture

```
                            ┌─────────────────────────────┐
   core_stock_tools (tool)  │  route_to_vendor(method,…)  │
   ─────────────►           └────────────┬────────────────┘
                                         │
                                         ▼
                          ┌──────────────────────────────┐
                          │ is_a_share(normalize(sym))?   │
                          └─────────┬─────────────────┬──┘
                              YES   ▼                 ▼  NO
                ┌──────────────────────────┐   ┌───────────────────────┐
                │ _tdx_adapter.dispatch(…) │   │ existing vendor chain │
                │  – core_stock_apis       │   │  (yfinance, alpha…)   │
                │  – technical_indicators  │   │  unchanged            │
                │  – fundamental_data      │   │                       │
                │  – benchmark index kline │   │                       │
                │  – shareholders          │   │                       │
                └──────────┬───────────────┘   └───────────────────────┘
                           ▼
                  ┌────────────────────────┐
                  │ TdxChronos(data_dir)   │   ← tdx_chronos package
                  │ kline / finance / ...  │     (lazy-loaded dep)
                  └────────────────────────┘
```

Two routing paths, both end up in the same module:

1. **Auto-routing (first priority)** — `route_to_vendor` calls `is_a_share(symbol)` first. If true, dispatch to the tdx-chronos adapter directly. This bypasses the configured `data_vendors` chain for A-share.
2. **Explicit opt-in** — `tdx_chronos` is added to `VENDOR_METHODS` for all categories tdx-chronos covers, so users who explicitly set `"core_stock_apis": "tdx_chronos"` (or `"tdx_chronos,yfinance"`) still work end-to-end. The auto-route gate skips when the symbol isn't A-share, so non-A-share tickers in a mixed chain always go to the other vendor.

## 5. Module: `tradingagents/dataflows/tdx_chronos.py` (new)

A single new file under `dataflows/`, mirroring the shape of `y_finance.py` / `alpha_vantage.py`.

### 5.1 Public surface

```python
class _TdxAdapter:
    def __init__(self, data_dir: str): ...           # one TdxChronos() facade
    def dispatch(self, method: str, symbol: str, *args, **kwargs) -> str: ...
    # category-specific helpers (called by dispatch)
    def get_stock_data(symbol, start, end) -> str
    def get_indicators(symbol, indicator, curr_date, look_back_days) -> str
    def get_fundamentals(symbol) -> str
    def get_balance_sheet(symbol) -> str
    def get_cashflow(symbol) -> str
    def get_income_statement(symbol) -> str
    def get_insider_transactions(symbol) -> str     # delegates to shareholders()
    def get_index_klines(index_code, start, end) -> str

def get_tdx_adapter() -> _TdxAdapter | None: ...     # lazy; None if unavailable

ETF_OUT_OF_SCOPE_MARKER = (
    "ETF/LOF/REIT/convertible-bond — fundamentals not in tdx_chronos scope; "
    "use fund_basic (tushare) or a vendor-specific source for ETF fundamentals."
)
```

### 5.2 Lazy loading

- `get_tdx_adapter()` instantiates the adapter **on first A-share call**, not at import time. This keeps the clean-install smoke (`pip install . && python -c "import tradingagents, cli.main"`) passing without `tdx-chronos` installed.
- A process-global cache holds one adapter. `close()` is called on it during interpreter shutdown (via `atexit`) to release the SQLite WAL lock.

### 5.3 Data-dir resolution

Order of precedence:

1. Env var `TDC_DATA_DIR` (system-level, owned by tdx-chronos's own cron convention).
2. `DEFAULT_CONFIG["tdx_chronos_data_dir"]` (TradingAgents-level; overridable by `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR`).
3. Hardcoded fallback: `"/app/tdx-chronos/data"`.

Failure modes:

- Adapter instance fails to construct (package missing, path doesn't exist): `get_tdx_adapter()` returns `None`. Auto-route is silently disabled; `route_to_vendor` proceeds as if no tdx-chronos is configured.
- Adapter exists but a particular call errors (e.g., parquet corruption): the call raises `NoMarketDataError(symbol, canonical, detail)`. Router converts to `NO_DATA_AVAILABLE` sentinel.

### 5.4 Return-type contract

Every adapter method returns the **existing CSV-string format** that the upstream tools already consume (the same shape `get_YFin_data_online` returns with its `# Stock data for…` header). This lets `core_stock_tools.get_stock_data` stay unchanged and avoids parsing-format churn downstream.

## 6. Symbol normalization — extension to `symbol_utils.py`

### 6.1 New helpers

```python
_A_SHARE_BARE = re.compile(r"^\d{6}$")                    # 600000
_A_SHARE_TDX = re.compile(r"^(sh|sz|bj)\d{6}$", re.I)     # sh600000
_A_SHARE_YAHOO = re.compile(r"^(\d{6})\.(SS|SZ|BJ)$", re.I)  # 600000.SS

# Q: 4/8 prefix -> bj, 5/6/9 -> sh, 0/2/3 -> sz.  ETFs/LOFs/REITs follow the same rule.
_TDX_PREFIX = {"5": "sh", "6": "sh", "9": "sh",
               "0": "sz", "2": "sz", "3": "sz",
               "4": "bj", "8": "bj"}

def normalize_a_share(symbol: str) -> str:
    """Return TDX-native form (sh600000) or empty string when input is not A-share."""

def is_a_share(symbol: str) -> bool:
    """True iff `normalize_a_share` returns a non-empty string AND the symbol
    is in TdxChronos.list_symbols() (one-shot cached set)."""
```

### 6.2 Active validation

Two process-globals, both built lazily on first call (separately, since they serve different purposes):

- `_KNOWN_A_SHARES: set[str]` — built from `TdxChronos.list_symbols()` and used by `is_a_share` to confirm the normalized symbol actually exists in tdx-chronos. Typos and delistings fail here rather than at the parquet read.
- `_KNOWN_ETFS: set[str]` — built from `TdxChronos.list_etfs()` and used by `get_tdx_fundamentals` for the "out of scope" marker.

If tdx-chronos isn't importable, both validations are skipped — `is_a_share` falls back to regex-only (still rejects non-numeric / wrong-digit-count inputs); the adapter's ETF check is bypassed and `get_tdx_fundamentals` proceeds to `tdx.finance()`, which returns an empty DataFrame for symbols outside `tdxfin.zip` and the adapter raises `NoMarketDataError`.

### 6.3 Test coverage

Unit tests in `tests/test_symbol_utils.py`:

- All accepted forms round-trip to the same canonical TDX form.
- Mixed case handled (`SH600000`, `sh600000`, `sh600000`).
- `6000000` (7 digits) rejected.
- Random non-numeric strings (`AAPL`, `XAUUSD+`, `BTC-USD`) return `False` — non-A-share remains non-A-share.
- The validation cache stays valid across multiple calls within a process.

## 7. Router integration — `interface.py`

### 7.1 New import

```python
from .tdx_chronos import ETF_OUT_OF_SCOPE_MARKER, get_tdx_adapter
```

### 7.2 VENDOR_LIST

Add `"tdx_chronos"` to the list.

### 7.3 VENDOR_METHODS additions

```python
"get_stock_data": {
    "tdx_chronos": get_tdx_stock_data,
    "alpha_vantage": ...,
    "yfinance": ...,
},
"get_indicators": {
    "tdx_chronos": get_tdx_indicators,
    "alpha_vantage": ...,
    "yfinance": ...,
},
"get_fundamentals": {
    "tdx_chronos": get_tdx_fundamentals,
    "alpha_vantage": ...,
    "yfinance": ...,
},
# same for balance_sheet, cashflow, income_statement, insider_transactions
```

For the methods that tdx-chronos does **not** cover (news, global news, macro, prediction), the entry is intentionally omitted — explicit `tdx_chronos` config raises `VendorNotConfiguredError`, matching the existing "vendor chain is the only resolution path" rule (per AGENTS.md / #988).

### 7.4 `route_to_vendor` flow change

```python
def route_to_vendor(method: str, *args, **kwargs):
    symbol = args[0] if args else kwargs.get("symbol")

    # First-priority A-share path
    if (not os.getenv("TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE")
            and symbol and is_a_share(symbol)):
        adapter = get_tdx_adapter()
        if adapter is not None:
            try:
                return adapter.dispatch(method, *args, **kwargs)
            except NoMarketDataError:
                raise     # let the router produce NO_DATA_AVAILABLE
            # NOTE: bare ValueError from the adapter is intentionally NOT caught —
            # it surfaces a real bug in tdx_chronos, not a missing-data condition.

    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(",")]
    ...
```

Key invariants:

- The auto-route short-circuits **only** when `is_a_share(symbol)` is true AND the adapter is constructible.
- `NoMarketDataError` propagates so the existing `NO_DATA_AVAILABLE` sentinel logic still fires.
- A broken adapter (raised `Exception` outside `NoMarketDataError`) is **not** silently swallowed — that's a real bug worth surfacing.
- The `TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE` env var is the documented escape hatch for users who want the configured chain to win even on A-share symbols.

## 8. Config additions — `default_config.py`

### 8.1 New default keys

```python
DEFAULT_CONFIG = {
    ...
    "tdx_chronos_data_dir": os.getenv(
        "TRADINGAGENTS_TDX_CHRONOS_DATA_DIR",
        os.getenv("TDC_DATA_DIR", "/app/tdx-chronos/data"),
    ),
    "tdx_chronos_auto_route": True,   # set False via TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE=0
    ...
}
```

### 8.2 Env overrides

Two new entries in `_ENV_OVERRIDES`:

- `"TRADINGAGENTS_TDX_CHRONOS_DATA_DIR"` → `"tdx_chronos_data_dir"` (string).
- `"TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE"` → `"tdx_chronos_auto_route"` (bool, coerced via existing `_coerce`).

### 8.3 `data_vendors` defaults

**Unchanged.** Auto-routing is the new behavior; explicit configuration remains opt-in via `"core_stock_apis": "tdx_chronos"` etc. Changing the default would surprise existing users.

## 9. Packaging & dependencies

### 9.1 Optional install group

```toml
[project.optional-dependencies]
tdx_chronos = ["tdx-chronos @ file:///app/tdx-chronos"]
```

A single new optional group. Released-form tdx-chronos (when published) can replace the `file://` reference without API changes.

### 9.2 Dev install

For contributors running the auto-route integration tests, the `tdx_chronos` extra is installed manually:

```bash
pip install -e ".[dev,tdx_chronos]"
```

The `dev` extras **do not** gain a `tdx-chronos` reference — keeping the install surface lean for contributors who don't have the sibling project.

### 9.3 README update

A new section documents:

```bash
# Prerequisite for A-share first-priority routing:
pip install -e /app/tdx-chronos
```

And mentions the env vars `TDC_DATA_DIR`, `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR`, `TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE`.

### 9.4 Clean-import smoke guarantee

- `tradingagents/dataflows/tdx_chronos.py` does **not** import `tdx_chronos` at module top-level. The import lives inside `get_tdx_adapter()` so `import tradingagents` (and `from cli.main import app`) succeed without the package installed.
- A `tests/test_tdx_chronos_missing_package.py` test asserts this: delete `tdx_chronos` from `sys.modules`, run `importlib.import_module("tradingagents.dataflows.tdx_chronos")`, assert `get_tdx_adapter()` returns `None` (rather than raising).

## 10. ETF/LOF/REIT/converter-bond fundamentals

The chosen behavior — **adapter returns explicit marker**:

```python
def get_tdx_fundamentals(symbol: str) -> str:
    canonical = normalize_a_share(symbol)
    etfs = _tdx_etfs_cache()   # process-global set, built from list_etfs()
    if canonical in etfs:
        return ETF_OUT_OF_SCOPE_MARKER
    df = _client().finance(canonical, ratio_only=True)
    if df.empty:
        raise NoMarketDataError(symbol, canonical, "no financial rows in tdx_chronos")
    return _format_finance_csv(df)
```

The marker is one line of plain English with the words "not in tdx_chronos scope" — short enough to keep token usage flat, explicit enough that an LLM reading it cannot mistake it for fabricated numbers.

## 11. Error handling summary

| Condition                                                     | Behavior                                                                                                                                |
|---------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| `tdx-chronos` not installed                                   | Auto-route silently disabled; explicit `tdx_chronos` config → `VendorNotConfiguredError`.                                               |
| `TDC_DATA_DIR` / config unset or path missing                 | Same as above.                                                                                                                          |
| Symbol not in tdx-chronos (typo, delisted)                    | `NoMarketDataError(symbol, canonical)` → router → `NO_DATA_AVAILABLE` sentinel.                                                         |
| ETF/LOF/REIT/可转债 passed to `get_fundamentals`              | Adapter returns `ETF_OUT_OF_SCOPE_MARKER` literal. Agent sees one-line explicit reason.                                                  |
| TdxChronos parquet read error                                 | Adapter raises `NoMarketDataError` (treated as empty result, not a crash). Network/IO errors logged at WARNING.                         |
| Adapter raises a non-vendor error                             | Bubbles up to the caller untouched — broken integration surfaces immediately so it can't masquerade as a no-data condition.              |
| `atexit` close() fails (external chmod)                       | Logged, not raised — the WAL unlock is operational hygiene, not a correctness requirement.                                              |

## 12. Tests — additions to `tests/`

### 12.1 New files

| File                                                     | Marker        | Purpose                                                                                                                                                                  |
|----------------------------------------------------------|---------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `tests/test_tdx_chronos_integration.py`                  | `unit`        | Mocks `tdx_chronos.client.TdxChronos` (and the lazy import) for the adapter logic; tests each method's empty/stale/format paths; verifies the ETF marker.               |
| `tests/test_a_share_symbol_normalization.py`              | `unit`        | All accepted input forms, regex rejection cases, case insensitivity, validation cache.                                                                                   |
| `tests/test_tdx_chronos_router.py`                       | `unit`        | Auto-route gate fires for A-share symbols; explicit `tdx_chronos` config wins when `auto_route=False` env var; non-A-share symbols untouched.                          |
| `tests/test_tdx_chronos_missing_package.py`               | `unit`        | With `tdx_chronos` removed from `sys.modules`, the adapter reports None; auto-route silently skips; explicit config raises `VendorNotConfiguredError`.                  |

### 12.2 Integration test (optional)

A single `pytest.mark.integration` test in `tests/integration/` that:

- Reads real data from `/app/tdx-chronos/data`.
- Calls `route_to_vendor("get_stock_data", "sh600000", ...)` end-to-end.
- Asserts the returned CSV-string starts with the `# Stock data for sh600000` header and contains the expected OHLCV columns.

The integration test is **skipped** when `tdx-chronos` isn't importable, so it doesn't break CI for contributors who haven't installed the optional dep.

### 12.3 Existing tests that must remain green

`tests/test_vendor_routing.py`, `tests/test_no_data_handling.py`, `tests/test_symbol_utils.py`, `tests/test_dataflows_config.py`, `tests/test_cli_env_skip.py`, `tests/test_env_overrides.py`, the clean-install import smoke (`pip install . && python -c "import tradingagents, cli.main"`). New code is additive; the only edit to `interface.py` is the addition of a top-level guard in `route_to_vendor` that explicitly **does not** apply to non-A-share symbols.

## 13. Documentation

- **AGENTS.md** — one new bullet in the "Data vendors" line listing `tdx_chronos` for A-share, plus a "Gotchas" entry on `TDC_DATA_DIR`.
- **README.md** — short "A-share users" subsection linking to `/app/tdx-chronos`'s setup and the env-var escape hatch.
- **CHANGELOG.md** — new entry under the unreleased section listing vendor additions and the auto-routing behavior.

## 14. Roll-out & migration

This change is **non-breaking**:

- Default `data_vendors` strings are unchanged; users who don't touch config keep getting yfinance for everything.
- A-share users who **do** have `tdx-chronos` installed at `/app/tdx-chronos/data` automatically get first-priority routing on A-share tickers. US tickers (e.g. `AAPL`) keep going to yfinance.
- The escape hatch `TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE=1` reverts to pre-change behavior immediately.
- No deprecations, no removals.

## 15. Open questions deferred

- **Convertible-bond klines** — confirmed working in tdx-chronos (per its `list_symbols()` returning `sh110xxx` etc.); treated identically to A-shares for routing. No user-facing distinction unless the conversation brings it up.
- **`shareholders_history`** — not yet a TradingAgents tool; if it becomes one, this design accommodates it by adding one more entry in `VENDOR_METHODS["get_shareholders_history"]`.
- **A-share ADR routing** — Nasdaq-listed ADRs (`600000` doesn't exist there, but `TCEHY` exists for Tencent) are not part of the A-share routing path. They're already handled by yfinance as ordinary US tickers.
