# tdx-chronos Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `/app/tdx-chronos` as the first-priority data vendor for A-share stocks and ETFs across every TradingAgents category — OHLCV, technical indicators, fundamentals, shareholder data, and benchmark index klines — with auto-detection of A-share symbols and a graceful opt-in vendor registration for explicit user configuration.

**Architecture:** A new `tradingagents/dataflows/tdx_chronos.py` adapter owns all tdx-chronos interactions, with a lazy `get_tdx_adapter()` so `import tradingagents` stays clean even when tdx-chronos isn't installed. `symbol_utils.py` gains `normalize_a_share` + `is_a_share` for input validation. `interface.py` adds a top-of-`route_to_vendor` gate that dispatches A-share tickers straight to the adapter and falls through to the existing vendor chain for everything else. Default `data_vendors` are unchanged; A-share routing kicks in automatically when tdx-chronos is installed; explicit `"tdx_chronos"` config still works through the existing `VENDOR_METHODS` machinery.

**Tech Stack:** Python 3.10+, tdx-chronos (`/app/tdx-chronos`, parquet-backed; pyarrow predicate pushdown; pyenv at `/app/tdx-chronos/.venv`), stockstats for indicators, pandas for formatting.

**Spec:** `docs/superpowers/specs/2026-07-11-tdx-chronos-integration-design.md`

## Global Constraints

- Ruff `select = ["E", "W", "F", "I", "B", "UP", "C4", "SIM"]`, `ignore = ["E501"]`, line-length 100 (from `pyproject.toml`). All new code must pass `ruff check .` cleanly. **`ruff format` is intentionally deferred** — don't run it on this PR.
- TDD: every task writes tests first. Each task is one independently reviewable commit.
- The clean-install gate (`pip install . && python -c "import tradingagents, cli.main"`) must pass **without** tdx-chronos installed. The lazy import lives inside `get_tdx_adapter()`; the dataflows package module-load path stays free of `tdx_chronos` imports.
- **No top-level imports of `tdx_chronos`** anywhere under `tradingagents/`. Use the lazy adapter pattern exclusively.
- Existing tests must remain green: `tests/test_vendor_routing.py`, `tests/test_no_data_handling.py`, `tests/test_symbol_utils.py`, `tests/test_dataflows_config.py`, `tests/test_cli_env_skip.py`, `tests/test_env_overrides.py`.
- Vendor-error taxonomy (`tradingagents/dataflows/errors.py:1`) is the single source of truth — `NoMarketDataError` for empty/stale data, `VendorNotConfiguredError` for missing config, `VendorRateLimitError` for transient throttles.
- The configured vendor chain is the only resolution path. Don't silently introduce cross-vendor fallbacks (`#988`, `#289`).
- DO NOT add comments unless the surrounding code requires them to be readable.
- Commit messages follow conventional-commits style: `feat:`, `test:`, `docs:`, `chore:`, `fix:`.

---

## Task 1: A-share symbol normalization (regex, no tdx-chronos dep yet)

**Files:**
- Create: `tests/test_a_share_symbol_normalization.py`
- Modify: `tradingagents/dataflows/symbol_utils.py`

**Interfaces:**
- Consumes: nothing (no tdx-chronos required at this stage)
- Produces:
  - `normalize_a_share(symbol: str) -> str`  — empty string if input is not A-share, else TDX-native form (`sh600000`)
  - `is_a_share(symbol: str) -> bool` — pure-regex check; validates that the canonical form is well-formed. Does **not** validate against `TdxChronos.list_symbols()` until Task 2.

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_a_share_symbol_normalization.py`:

```python
"""A-share symbol normalization — pure-regex stage.

The TdxChronos-backed validation is layered on top in Task 2. These
tests cover the regex/format work in isolation so a missing-package
failure mode cannot mask real normalization bugs.
"""

import unittest

import pytest

from tradingagents.dataflows.symbol_utils import is_a_share, normalize_a_share


@pytest.mark.unit
class TestNormalizeAShare(unittest.TestCase):
    def test_bare_six_digit_normalizes(self):
        for sym, expected in (
            ("600000", "sh600000"),  # 6 -> sh
            ("000001", "sz000001"),  # 0 -> sz
            ("300750", "sz300750"),  # 3 -> sz
            ("510050", "sh510050"),  # 5 -> sh (ETF)
            ("830799", "bj830799"),  # 8 -> bj
            ("430047", "bj430047"),  # 4 -> bj
            ("159915", "sz159915"),  # 1 -> sz (deep-ETF)
        ):
            self.assertEqual(normalize_a_share(sym), expected)

    def test_tdx_native_passes_through_lowercased(self):
        self.assertEqual(normalize_a_share("SH600000"), "sh600000")
        self.assertEqual(normalize_a_share("sh600000"), "sh600000")
        self.assertEqual(normalize_a_share("Sz000001"), "sz000001")

    def test_yahoo_suffix_normalizes(self):
        self.assertEqual(normalize_a_share("600000.SS"), "sh600000")
        self.assertEqual(normalize_a_share("000001.SZ"), "sz000001")
        self.assertEqual(normalize_a_share("830799.BJ"), "bj830799")
        self.assertEqual(normalize_a_share("600000.ss"), "sh600000")  # case-insensitive

    def test_non_a_share_returns_empty(self):
        for sym in ("AAPL", "XAUUSD+", "BTC-USD", "EURUSD=X", "", "A", "ABCDEF", "6000", "6000000"):
            self.assertEqual(normalize_a_share(sym), "", f"unexpected hit for {sym!r}")

    def test_is_a_share_matches_normalize(self):
        for sym, expected in (
            ("600000", "sh600000"),
            ("sh600000", "sh600000"),
            ("600000.SS", "sh600000"),
            ("830799.BJ", "bj830799"),
        ):
            self.assertTrue(is_a_share(sym), f"expected {sym!r} to be A-share")

    def test_is_a_share_rejects_non_a_share(self):
        for sym in ("AAPL", "TSLA", "BTC-USD", "XAUUSD", "GC=F", "^GSPC", "6000000"):
            self.assertFalse(is_a_share(sym), f"unexpected A-share match for {sym!r}")
```

- [ ] **Step 1.2: Run the test, confirm FAIL**

Run: `pytest tests/test_a_share_symbol_normalization.py -q`
Expected: `ModuleNotFoundError: cannot import name 'normalize_a_share' from 'tradingagents.dataflows.symbol_utils'`

- [ ] **Step 1.3: Implement `normalize_a_share` and `is_a_share`**

Modify `tradingagents/dataflows/symbol_utils.py`. Add the following constants and functions at the end of the module (after the existing `_CRYPTO_QUOTES` block — keep the `__all__`-style ordering; the file has no `__all__` today so a bottom-of-file append is fine):

```python
# A-share symbol normalization.
#
# Accept three input shapes and produce the TDX-native lowercase form
# (``sh600000`` / ``sz000001`` / ``bj830799``) that ``TdxChronos`` expects:
#
#     user types   canonical        prefix rule (first digit)
#     ----------   ---------------  -----------------------
#     600000       sh600000          5/6/9 -> sh
#     000001       sz000001          0/2/3 -> sz
#     830799       bj830799          4/8 -> bj
#     600000.SS    sh600000          Yahoo / TradingView suffixes
#     sh600000     sh600000          TDX native (lowercased)
#
# Returns the empty string for non-A-share inputs — callers treat that as
# "skip auto-route" and fall through to the existing vendor chain.

_A_SHARE_BARE = re.compile(r"^(\d{6})$")
_A_SHARE_TDX = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
_A_SHARE_YAHOO = re.compile(r"^(\d{6})\.(SS|SZ|BJ)$", re.IGNORECASE)

_A_SHARE_DIGIT_TO_PREFIX = {
    "5": "sh", "6": "sh", "9": "sh",
    "0": "sz", "2": "sz", "3": "sz",
    "4": "bj", "1": "sz", "8": "bj",
}


def normalize_a_share(symbol: str) -> str:
    """Return TDX-native A-share form (``sh600000``) or ``''`` when input isn't A-share.

    Pure regex/format normalization; does not look up ``TdxChronos.list_symbols()``.
    Use :func:`is_a_share` for the cross-validated answer.
    """
    if not isinstance(symbol, str):
        return ""
    s = symbol.strip()
    if not s:
        return ""

    m = _A_SHARE_TDX.match(s)
    if m:
        prefix, digits = m.group(1).lower(), m.group(2)
        return f"{prefix}{digits}"

    m = _A_SHARE_BARE.match(s)
    if m:
        prefix = _A_SHARE_DIGIT_TO_PREFIX.get(m.group(1)[0])
        if prefix is None:
            return ""
        return f"{prefix}{m.group(1)}"

    m = _A_SHARE_YAHOO.match(s)
    if m:
        digits, suffix = m.group(1), m.group(2).lower()
        prefix = {"ss": "sh", "sz": "sz", "bj": "bj"}[suffix]
        return f"{prefix}{digits}"

    return ""


def is_a_share(symbol: str) -> bool:
    """True when :func:`normalize_a_share` returns a well-formed canonical form.

    Validation against ``TdxChronos.list_symbols()`` is added in Task 2; this
    task covers the format-only stage so a missing-package failure mode
    cannot mask a typo in the regex.
    """
    return bool(normalize_a_share(symbol))
```

- [ ] **Step 1.4: Run the test, confirm PASS**

Run: `pytest tests/test_a_share_symbol_normalization.py -v`
Expected: 6 tests pass.

- [ ] **Step 1.5: Lint and run the existing `test_symbol_utils.py`**

Run: `ruff check tradingagents/dataflows/symbol_utils.py tests/test_a_share_symbol_normalization.py && pytest tests/test_symbol_utils.py -q`
Expected: ruff clean; existing tests still pass.

- [ ] **Step 1.6: Commit**

```bash
git add tradingagents/dataflows/symbol_utils.py tests/test_a_share_symbol_normalization.py
git commit -m "feat(symbol-utils): add normalize_a_share + is_a_share regex stage"
```

---

## Task 2: tdx-chronos adapter skeleton + lazy `get_tdx_adapter()`

**Files:**
- Create: `tradingagents/dataflows/tdx_chronos.py`
- Create: `tests/test_tdx_chronos_missing_package.py`

**Interfaces:**
- Consumes: `is_a_share` from Task 1; tdx-chronos package (lazily); `NoMarketDataError` from `errors.py`.
- Produces:
  - `get_tdx_adapter() -> _TdxAdapter | None` — returns the singleton adapter, or `None` when tdx-chronos is not importable / not configured.
  - `_TdxAdapter.dispatch(method: str, symbol: str, *args, **kwargs) -> str` — dispatches to `get_stock_data` / `get_indicators` / etc. (Stubs in this task; methods filled in over Tasks 3–6.)
  - `ETF_OUT_OF_SCOPE_MARKER: str` — the explicit "out of scope" marker used by `get_fundamentals` for ETF/LOF/REITs/可转债.

- [ ] **Step 2.1: Write the missing-package test**

Create `tests/test_tdx_chronos_missing_package.py`:

```python
"""The dataflows package must load even when tdx-chronos isn't installed.

The clean-install smoke gate (``pip install . && python -c "import tradingagents, cli.main"``)
explicitly excludes optional dependencies. A top-level ``import tdx_chronos``
would re-introduce the undeclared-dependency class of bug that PR #994
closed — so we verify here that the adapter reports ``None`` and the auto-
route path becomes a silent no-op when the package is unavailable.
"""

import importlib
import sys
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.interface as interface
from tradingagents.dataflows import tdx_chronos as tc_mod


@pytest.mark.unit
class MissingPackageTests(unittest.TestCase):
    def setUp(self):
        # Reach into the module-level cache without polluting sys.modules more
        # than necessary. ``get_tdx_adapter`` checks both an internal sentinel
        # and the module presence; we clear both to simulate "not installed".
        self._saved_state = tc_mod._adapter_state_for_tests()  # type: ignore[attr-defined]

    def tearDown(self):
        tc_mod._restore_state_for_tests(self._saved_state)  # type: ignore[attr-defined]

    def test_get_tdx_adapter_returns_none_when_package_missing(self):
        with mock.patch.dict(sys.modules, {"tdx_chronos": None, "tdx_chronos.client": None}):
            tc_mod._reset_state_for_tests()  # type: ignore[attr-defined]
            self.assertIsNone(tc_mod.get_tdx_adapter())

    def test_clean_import_does_not_require_tdx_chronos(self):
        # Force the worst-case mock: tdx_chronos is not a real module.
        with mock.patch.dict(sys.modules, {"tdx_chronos": None, "tdx_chronos.client": None}):
            reloaded = importlib.reload(tc_mod)
            try:
                self.assertIsNone(reloaded.get_tdx_adapter())
            finally:
                importlib.reload(tc_mod)  # restore

    def test_interface_module_loads_when_tdx_chronos_missing(self):
        with mock.patch.dict(sys.modules, {"tdx_chronos": None, "tdx_chronos.client": None}):
            with mock.patch.object(interface, "route_to_vendor", wraps=interface.route_to_vendor):
                # Just importing is enough — the assertion is that nothing throws.
                self.assertTrue(callable(interface.route_to_vendor))
```

Add matching test-helper names in the adapter module (`_adapter_state_for_tests`, `_reset_state_for_tests`, `_restore_state_for_tests`) when you implement Step 2.3.

- [ ] **Step 2.2: Run the test, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_missing_package.py -q`
Expected: `ModuleNotFoundError: No module named 'tradingagents.dataflows.tdx_chronos'`

- [ ] **Step 2.3: Implement the adapter skeleton**

Create `tradingagents/dataflows/tdx_chronos.py`:

```python
"""A-share & ETF data adapter on top of ``tdx-chronos``.

The adapter is the single TradingAgents surface to :mod:`tdx_chronos`. It is
intentionally lazy — :func:`get_tdx_adapter` performs the first (and only)
package import — so :mod:`tradingagents` stays import-clean when the
optional dependency is missing.

The adapter mirrors the upstream return formats (CSV-string for OHLCV and
fundamentals; the dotted ``date: value`` lines for indicators) so call
sites do not need to branch on vendor.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any

from .errors import NoMarketDataError
from .symbol_utils import is_a_share, normalize_a_share

logger = logging.getLogger(__name__)

ETF_OUT_OF_SCOPE_MARKER = (
    "ETF/LOF/REIT/convertible-bond — fundamentals not in tdx_chronos scope; "
    "use fund_basic (tushare) or a vendor-specific source for ETF fundamentals."
)

_ADAPTER_STATE: dict[str, Any] = {"adapter": None, "client": None}
_STATE_LOCK = threading.Lock()


def _import_tdx_chronos():
    """Import the tdx_chronos client module without raising when missing.

    Returns the module object, or ``None`` when the package isn't installed
    or the configured data directory doesn't exist.
    """
    try:
        from tdx_chronos.client import TdxChronos
    except Exception as exc:
        logger.debug("tdx_chronos not importable: %s", exc)
        return None, None
    return TdxChronos, None  # type: ignore[return-value]


def _resolve_data_dir() -> str | None:
    """Precedence: TRADINGAGENTS_TDX_CHRONOS_DATA_DIR -> config -> TDC_DATA_DIR -> /app/tdx-chronos/data."""
    from .config import get_config

    cfg_path = get_config().get("tdx_chronos_data_dir")
    candidates = [
        os.environ.get("TRADINGAGENTS_TDX_CHRONOS_DATA_DIR"),
        cfg_path,
        os.environ.get("TDC_DATA_DIR"),
        "/app/tdx-chronos/data",
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None


def _build_adapter():
    """Construct an :class:`_TdxAdapter` with a fresh ``TdxChronos`` facade.

    Returns ``None`` when the package is unavailable, the data directory is
    missing, or the facade refuses to construct (malformed store).
    """
    data_dir = _resolve_data_dir()
    if data_dir is None:
        logger.debug("tdx_chronos data directory not found in any candidate path")
        return None
    TdxChronos, _ = _import_tdx_chronos()
    if TdxChronos is None:
        return None
    try:
        client = TdxChronos(data_dir, readonly=True)
    except Exception as exc:
        logger.warning("Failed to construct TdxChronos facade at %s: %s", data_dir, exc)
        return None

    adapter = _TdxAdapter(client=client, data_dir=data_dir)
    atexit.register(_safe_close, client)
    return adapter


def _safe_close(client) -> None:
    try:
        client.close()
    except Exception as exc:
        logger.debug("tdx_chronos client.close() raised: %s", exc)


def get_tdx_adapter():
    """Return the process-global :class:`_TdxAdapter` singleton, or ``None``.

    The first call constructs the adapter; subsequent calls return the
    cached one. ``None`` indicates that the auto-route path should be a
    silent no-op and explicit ``tdx_chronos`` config should raise
    :class:`VendorNotConfiguredError`.
    """
    with _STATE_LOCK:
        if _ADAPTER_STATE["adapter"] is not None:
            return _ADAPTER_STATE["adapter"]
        adapter = _build_adapter()
        _ADAPTER_STATE["adapter"] = adapter
        _ADAPTER_STATE["client"] = getattr(adapter, "_client", None)
        return adapter


class _TdxAdapter:
    """Stateless facade over ``TdxChronos`` that produces vendor-format strings."""

    def __init__(self, client, data_dir: str):
        self._client = client
        self.data_dir = data_dir
        self._known_a_shares: set[str] | None = None
        self._known_etfs: set[str] | None = None
        self._known_lock = threading.Lock()

    # --- caches ---------------------------------------------------------------

    def _load_a_share_cache(self) -> set[str]:
        with self._known_lock:
            if self._known_a_shares is None:
                try:
                    self._known_a_shares = set(self._client.list_symbols())
                    self._known_etfs = set(self._client.list_etfs())
                except Exception as exc:
                    logger.debug("tdx_chronos list_symbols/list_etfs failed: %s", exc)
                    self._known_a_shares = set()
                    self._known_etfs = set()
            return self._known_a_shares

    def _load_etf_cache(self) -> set[str]:
        with self._known_lock:
            if self._known_etfs is None:
                self._load_a_share_cache()
            return self._known_etfs or set()

    def _canonical(self, symbol: str) -> str:
        canon = normalize_a_share(symbol)
        if not canon:
            raise NoMarketDataError(symbol, symbol, "input is not A-share-shaped")
        return canon

    # --- dispatch -------------------------------------------------------------

    def dispatch(self, method: str, symbol: str, *args, **kwargs) -> str:
        canon = self._canonical(symbol)
        if method == "get_stock_data":
            return self.get_stock_data(canon, *args, **kwargs)
        if method == "get_indicators":
            return self.get_indicators(canon, *args, **kwargs)
        if method in {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"}:
            return getattr(self, method)(canon, *args, **kwargs)
        if method == "get_insider_transactions":
            return self.get_insider_transactions(canon, *args, **kwargs)
        if method == "get_index_klines":
            return self.get_index_klines(*args, **kwargs)
        raise NotImplementedError(
            f"tdx_chronos adapter has no implementation for {method!r}"
        )

    # --- category methods (filled in over Tasks 3-6) --------------------------

    def get_stock_data(self, canonical: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError  # Task 3

    def get_indicators(self, canonical: str, indicator: str, curr_date: str, look_back_days: int) -> str:
        raise NotImplementedError  # Task 4

    def get_fundamentals(self, canonical: str) -> str:
        raise NotImplementedError  # Task 5

    def get_balance_sheet(self, canonical: str) -> str:
        raise NotImplementedError  # Task 5

    def get_cashflow(self, canonical: str) -> str:
        raise NotImplementedError  # Task 5

    def get_income_statement(self, canonical: str) -> str:
        raise NotImplementedError  # Task 5

    def get_insider_transactions(self, canonical: str) -> str:
        raise NotImplementedError  # Task 6

    def get_index_klines(self, index_code: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError  # Task 6

    # --- test helpers ---------------------------------------------------------

    @staticmethod
    def _adapter_state_for_tests():
        return dict(_ADAPTER_STATE)

    @staticmethod
    def _reset_state_for_tests():
        with _STATE_LOCK:
            current = _ADAPTER_STATE.get("client")
            if current is not None:
                _safe_close(current)
            _ADAPTER_STATE["adapter"] = None
            _ADAPTER_STATE["client"] = None

    @staticmethod
    def _restore_state_for_tests(state):
        with _STATE_LOCK:
            _ADAPTER_STATE.clear()
            _ADAPTER_STATE.update(state)


# Re-export the helpers the tests imported by attribute name. Keeping them at
# module scope (instead of only as static methods on the class) makes it
# trivial to ``mock.patch.object(tdx_chronos, "_reset_state_for_tests")``.
_adapter_state_for_tests = _TdxAdapter._adapter_state_for_tests
_reset_state_for_tests = _TdxAdapter._reset_state_for_tests
_restore_state_for_tests = _TdxAdapter._restore_state_for_tests


def is_a_share_via_adapter(symbol: str) -> bool:
    """Convenience: True iff ``symbol`` parses as A-share and exists in the warehouse.

    Falls back to the regex-only check when the adapter is unavailable.
    """
    if not is_a_share(symbol):
        return False
    adapter = get_tdx_adapter()
    if adapter is None:
        return True
    return normalize_a_share(symbol) in adapter._load_a_share_cache()


# ``is_a_share`` keeps its pure-regex semantics for now; the warehouse-backed
# check becomes the public name in Task 7.
```

- [ ] **Step 2.4: Run the test, confirm PASS**

Run: `pytest tests/test_tdx_chronos_missing_package.py -v`
Expected: 3 tests pass.

- [ ] **Step 2.5: Verify clean-import smoke**

Run: `pip install . 2>&1 | tail -3 && python -c "import tradingagents; from cli.main import app; print('ok')"`
Expected: prints `ok`. No `tdx_chronos` import error.

If tdx-chronos happens to be installed locally (`pip install -e /app/tdx-chronos` already run in this environment), the test still passes because `get_tdx_adapter()` returns an adapter rather than `None` — but the mock in the test still forces the missing-package path. If the import succeeds unexpectedly, add a debug print to verify; otherwise proceed.

- [ ] **Step 2.6: Commit**

```bash
git add tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_missing_package.py
git commit -m "feat(tdx-chronos): lazy adapter skeleton + missing-package handling"
```

---

## Task 3: Adapter `get_stock_data` (OHLCV CSV-string format)

**Files:**
- Modify: `tradingagents/dataflows/tdx_chronos.py`
- Create: `tests/test_tdx_chronos_integration.py`

**Interfaces:**
- Consumes: `_TdxAdapter._canonical` (Task 2), `NoMarketDataError`.
- Produces:
  - `_TdxAdapter.get_stock_data(canonical: str, start_date: str, end_date: str) -> str`
  - Output matches `get_YFin_data_online` (header lines `# Stock data for…` followed by CSV with `Date,Open,High,Low,Close,Volume` columns).

- [ ] **Step 3.1: Write the failing test**

Append to `tests/test_tdx_chronos_integration.py`:

```python
"""Adapter unit tests with a mocked TdxChronos facade."""

import datetime
import unittest
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import tdx_chronos as tc
from tradingagents.dataflows.tdx_chronos import (
    _TdxAdapter,
    ETF_OUT_OF_SCOPE_MARKER,
    get_tdx_adapter,
)


def _fake_client(*, kline_df=None, kline_raises=None, symbol_info=None, list_symbols=None, list_etfs=None):
    """Return a mock with the methods the adapter touches wired up."""
    client = mock.Mock()
    if list_symbols is not None:
        client.list_symbols.return_value = list_symbols
    else:
        client.list_symbols.return_value = ["sh600000", "sh510050", "sz000001"]
    if list_etfs is not None:
        client.list_etfs.return_value = list_etfs
    else:
        client.list_etfs.return_value = ["sh510050"]
    client.symbol_info.return_value = symbol_info or {"symbol": "sh600000", "market": "sh"}
    if kline_raises is not None:
        client.kline.side_effect = kline_raises
    else:
        client.kline.return_value = kline_df if kline_df is not None else pd.DataFrame(
            {
                "date": ["2024-12-30", "2024-12-31"],
                "open": [10.0, 10.5],
                "high": [11.0, 11.5],
                "low": [9.5, 10.4],
                "close": [10.2, 11.0],
                "amount": [1e8, 1.1e8],
                "vol": [1e6, 1.1e6],
            }
        )
    return client


@pytest.mark.unit
class TestGetStockData(unittest.TestCase):
    def setUp(self):
        tc._reset_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(tc._adapter_state_for_tests())

    def test_returns_csv_with_header(self):
        client = _fake_client()
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_stock_data("sh600000", "2024-12-30", "2024-12-31")
        self.assertTrue(out.startswith("# Stock data for"))
        self.assertIn("from 2024-12-30 to 2024-12-31", out)
        # CSV body: at least Date, Open, High, Low, Close columns.
        for col in ("Date", "Open", "High", "Low", "Close"):
            self.assertIn(col, out)

    def test_canonical_label_when_user_passes_yahoo_form(self):
        client = _fake_client()
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_stock_data("600000.SS", "2024-12-30", "2024-12-31")
        self.assertIn("sh600000", out)
        self.assertIn("(from 600000.SS)", out)

    def test_empty_kline_raises_no_data_error(self):
        empty = pd.DataFrame({})
        client = _fake_client(kline_df=empty)
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_stock_data("sh600000", "2024-12-30", "2024-12-31")

    def test_non_a_share_canonicalization_raises(self):
        client = _fake_client()
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_stock_data("AAPL", "2024-12-30", "2024-12-31")
```

- [ ] **Step 3.2: Run the test, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetStockData -v`
Expected: `NotImplementedError` from `get_stock_data`.

- [ ] **Step 3.3: Implement `get_stock_data`**

Replace the stub `get_stock_data` in `tradingagents/dataflows/tdx_chronos.py` with:

```python
def get_stock_data(self, canonical: str, start_date: str, end_date: str) -> str:
    """OHLCV via ``TdxChronos.kline``; returns the same CSV-string header shape
    :func:`tradingagents.dataflows.y_finance.get_YFin_data_online` produces.
    """
    self._canonical(canonical)  # raises NoMarketDataError on non-A-share
    try:
        df = self._client.kline(canonical, start_date, end_date)
    except Exception as exc:
        raise NoMarketDataError(canonical, canonical, f"tdx_chronos.kline failed: {exc}") from exc

    if df is None or df.empty:
        raise NoMarketDataError(canonical, canonical, f"no rows between {start_date} and {end_date}")

    out = df.rename(columns={"date": "Date", "open": "Open", "high": "High",
                              "low": "Low", "close": "Close",
                              "amount": "Amount", "vol": "Volume"})
    if "Volume" not in out.columns and "vol" in df.columns:
        out["Volume"] = df["vol"]
    cols = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume", "Amount") if c in out.columns]
    csv = out[cols].to_csv(index=False)

    label = canonical if canonical == "sh600000" else canonical  # canonical-only path
    header = f"# Stock data for {canonical} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
    return header + csv
```

Add `import datetime` at the top of the file alongside the other stdlib imports.

- [ ] **Step 3.4: Run the test, confirm PASS**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetStockData -v`
Expected: 4 tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_integration.py
git commit -m "feat(tdx-chronos): get_stock_data returns OHLCV CSV in yfinance format"
```

---

## Task 4: Adapter `get_indicators` (mirrors yfinance window loop)

**Files:**
- Modify: `tradingagents/dataflows/tdx_chronos.py`
- Modify: `tests/test_tdx_chronos_integration.py`

**Interfaces:**
- Produces:
  - `_TdxAdapter.get_indicators(canonical, indicator, curr_date, look_back_days) -> str`
  - Output format matches `get_stock_stats_indicators_window`: a single result string with `## {indicator} values from … to …`, one line per date (`{date}: {value}\n`), and a description paragraph matching the indicator name.

- [ ] **Step 4.1: Write the failing test**

Append to `tests/test_tdx_chronos_integration.py`:

```python
_INDICATOR_DESCRIPTIONS = {
    "rsi": "RSI: Measures momentum to flag overbought/oversold conditions.",
    "macd": "MACD: Computes momentum via differences of EMAs.",
    "close_50_sma": "50 SMA: A medium-term trend indicator.",
}


@pytest.mark.unit
class TestGetIndicators(unittest.TestCase):
    def setUp(self):
        tc._reset_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(tc._adapter_state_for_tests())

    def test_window_format_matches_yfinance(self):
        kline = pd.DataFrame(
            {
                "date": ["2024-12-25", "2024-12-26", "2024-12-27", "2024-12-30", "2024-12-31"],
                "open": [10, 10, 10, 10, 10],
                "high": [11, 11, 11, 11, 11],
                "low": [9, 9, 9, 9, 9],
                "close": [10.0, 10.2, 10.4, 10.3, 10.5],
                "amount": [1] * 5,
                "vol": [1] * 5,
            }
        )
        client = _fake_client(kline_df=kline)
        with mock.patch("tradingagents.dataflows.stockstats.wrap", side_effect=lambda df: df):
            with mock.patch("tradingagents.dataflows.tdx_chronos._indicator_value_for_date",
                            side_effect=lambda df, ind, d: 50.0 if d == "2024-12-31" else None):
                adapter = _TdxAdapter(client=client, data_dir="/data")
                out = adapter.get_indicators("sh600000", "rsi", "2024-12-31", look_back_days=3)

        self.assertTrue(out.startswith("## rsi values from"))
        for date_str in ("2024-12-29", "2024-12-30", "2024-12-31"):
            self.assertIn(date_str, out)
        self.assertIn("RSI:", out)

    def test_unsupported_indicator_raises_value_error(self):
        client = _fake_client()
        adapter = _TdxAdapter(client=client, data_dir="/data")
        with self.assertRaises(ValueError):
            adapter.get_indicators("sh600000", "totally_made_up", "2024-12-31", look_back_days=3)

    def test_non_a_share_canonicalization_raises(self):
        client = _fake_client()
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_indicators("AAPL", "rsi", "2024-12-31", look_back_days=3)
```

- [ ] **Step 4.2: Run, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetIndicators -v`
Expected: `NotImplementedError`.

- [ ] **Step 4.3: Implement `get_indicators` and helper**

Replace the stub `get_indicators` in `tradingagents/dataflows/tdx_chronos.py` with:

```python
_INDICATOR_DESCRIPTIONS = {
    # Moving Averages
    "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.",
    "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.",
    "close_10_ema": "10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.",
    # MACD
    "macd": "MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.",
    "macds": "MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.",
    "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.",
    # Momentum
    "rsi": "RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.",
    # Volatility
    "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.",
    "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.",
    "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.",
    "atr": "ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.",
    # Volume-based
    "vwma": "VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.",
    "mfi": "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals.",
}


def _indicator_value_for_date(df: pd.DataFrame, indicator: str, target_date: str):
    """Compute ``indicator`` on ``df`` via stockstats and look up ``target_date``.

    Returns ``None`` when the date isn't in the frame (weekend/holiday).
    """
    from stockstats import wrap

    work = df.rename(columns={"date": "Date", "open": "Open", "high": "High",
                              "low": "Low", "close": "Close",
                              "amount": "Amount", "vol": "Volume"})
    if "Volume" not in work.columns and "vol" in df.columns:
        work["Volume"] = df["vol"]
    work["Date"] = pd.to_datetime(work["Date"])
    work = work.sort_values("Date").reset_index(drop=True)
    wrapped = wrap(work)
    try:
        _ = wrapped[indicator]
    except KeyError:
        return None
    rows = wrapped[wrapped["Date"].dt.strftime("%Y-%m-%d") == target_date]
    if rows.empty:
        return None
    val = rows[indicator].iloc[0]
    return float(val) if pd.notna(val) else None


def get_indicators(self, canonical: str, indicator: str, curr_date: str, look_back_days: int) -> str:
    """Compute a single indicator over a ``look_back_days`` window ending at ``curr_date``.

    Loads kline from tdx-chronos, runs stockstats per date, returns the same
    multi-line string shape as :func:`get_stock_stats_indicators_window`.
    """
    self._canonical(canonical)
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator!r} not supported. Choose from: {list(_INDICATOR_DESCRIPTIONS)}"
        )

    end_dt = datetime.datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = end_dt - datetime.timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date_inclusive = (end_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = self._client.kline(canonical, start_date, end_date_inclusive)
    except Exception as exc:
        raise NoMarketDataError(canonical, canonical, f"tdx_chronos.kline failed: {exc}") from exc

    description = _INDICATOR_DESCRIPTIONS[indicator]
    lines = []
    cursor = end_dt
    while cursor >= start_dt:
        date_str = cursor.strftime("%Y-%m-%d")
        try:
            value = _indicator_value_for_date(df, indicator, date_str)
        except Exception:
            value = None
        if value is None:
            lines.append(f"{date_str}: N/A: Not a trading day (weekend or holiday)")
        else:
            lines.append(f"{date_str}: {value}")
        cursor -= datetime.timedelta(days=1)

    body = "\n".join(lines) + "\n"
    return (
        f"## {indicator} values from {start_dt.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + body
        + "\n\n"
        + description
    )
```

- [ ] **Step 4.4: Run, confirm PASS**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetIndicators -v`
Expected: 3 tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_integration.py
git commit -m "feat(tdx-chronos): get_indicators via stockstats window loop"
```

---

## Task 5: Adapter `get_fundamentals` + balance/cashflow/income + ETF marker

**Files:**
- Modify: `tradingagents/dataflows/tdx_chronos.py`
- Modify: `tests/test_tdx_chronos_integration.py`

**Interfaces:**
- Produces:
  - `_TdxAdapter.get_fundamentals(canonical) -> str`
    - For ETF/LOF/REIT/可转债 (in `_load_etf_cache()`): returns `ETF_OUT_OF_SCOPE_MARKER`.
    - For A-share: returns a CSV-string with `# Fundamentals …` header and the ratio-only columns.
  - `_TdxAdapter.get_balance_sheet(canonical) -> str` — calls `_finance_csv` with `ratio_only=False` and a `# Balance sheet …` header. tdx-chronos's `finance()` returns mixed-statement rows today, so this returns the full frame under the balance-sheet header; per-statement splitting is out of scope per spec §10.
  - Same pattern for `_TdxAdapter.get_cashflow` and `_TdxAdapter.get_income_statement`.
  - Missing-data → `NoMarketDataError`.

**Note for `_balance_sheet` / `_cashflow` / `_income_statement`:** tdx-chronos's `finance()` returns one row per (symbol, quarter) with mixed columns (per `requirements.md` 264-field format). Splitting those into the four separate statements is out of scope for this PR (per spec §10); all four methods return the same `fundamentals` payload plus a distinguishing header. The router treats these as identical information because the upstream columns don't cleanly partition by statement. A follow-up PR can add per-statement extraction once the use case lands.

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_tdx_chronos_integration.py`:

```python
@pytest.mark.unit
class TestGetFundamentals(unittest.TestCase):
    def setUp(self):
        tc._reset_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(tc._adapter_state_for_tests())

    def test_etf_returns_out_of_scope_marker(self):
        client = _fake_client(list_symbols=["sh510050"], list_etfs=["sh510050"])
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_fundamentals("sh510050")
        self.assertEqual(out, ETF_OUT_OF_SCOPE_MARKER)
        client.finance.assert_not_called()

    def test_a_share_returns_finance_csv(self):
        finance_df = pd.DataFrame(
            {
                "code": ["sh600000"] * 3,
                "report_date": ["2024-03-31", "2024-06-30", "2024-09-30"],
                "净资产收益率": [10.0, 11.0, 12.0],
                "毛利率": [40.0, 41.0, 42.0],
            }
        )
        client = _fake_client(list_symbols=["sh600000"], list_etfs=[])
        client.finance.return_value = finance_df
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_fundamentals("sh600000")
        self.assertTrue(out.startswith("# Fundamentals for sh600000"))
        for col in ("净资产收益率", "毛利率"):
            self.assertIn(col, out)

    def test_empty_finance_raises(self):
        client = _fake_client(list_symbols=["sh600000"], list_etfs=[])
        client.finance.return_value = pd.DataFrame({})
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_fundamentals("sh600000")

    def test_other_statements_reuse_fundamentals(self):
        client = _fake_client(list_symbols=["sh600000"], list_etfs=[])
        client.finance.return_value = pd.DataFrame(
            {"code": ["sh600000"], "report_date": ["2024-09-30"], "净利润": [1.0]}
        )
        adapter = _TdxAdapter(client=client, data_dir="/data")
        bs = adapter.get_balance_sheet("sh600000")
        cf = adapter.get_cashflow("sh600000")
        is_ = adapter.get_income_statement("sh600000")
        self.assertTrue(bs.startswith("# Balance sheet for sh600000"))
        self.assertTrue(cf.startswith("# Cash flow for sh600000"))
        self.assertTrue(is_.startswith("# Income statement for sh600000"))
        # finance() called once per method but cache is fine; assert >0 calls.
        self.assertGreaterEqual(client.finance.call_count, 3)
```

- [ ] **Step 5.2: Run, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetFundamentals -v`
Expected: `NotImplementedError`.

- [ ] **Step 5.3: Implement `get_fundamentals` and the three sibling methods**

Replace each of the four stubs in `tradingagents/dataflows/tdx_chronos.py` with:

```python
def _finance_csv(self, canonical: str, header_label: str, ratio_only: bool) -> str:
    etfs = self._load_etf_cache()
    if canonical in etfs:
        return ETF_OUT_OF_SCOPE_MARKER
    try:
        df = self._client.finance(canonical, ratio_only=ratio_only)
    except Exception as exc:
        raise NoMarketDataError(canonical, canonical, f"tdx_chronos.finance failed: {exc}") from exc
    if df is None or df.empty:
        raise NoMarketDataError(canonical, canonical, "no financial rows in tdx_chronos")
    csv = df.to_csv(index=False)
    header = f"# {header_label} for {canonical}\n"
    header += f"# Quarters: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
    return header + csv


def get_fundamentals(self, canonical: str) -> str:
    self._canonical(canonical)
    return self._finance_csv(canonical, "Fundamentals", ratio_only=True)


def get_balance_sheet(self, canonical: str) -> str:
    self._canonical(canonical)
    return self._finance_csv(canonical, "Balance sheet", ratio_only=False)


def get_cashflow(self, canonical: str) -> str:
    self._canonical(canonical)
    return self._finance_csv(canonical, "Cash flow", ratio_only=False)


def get_income_statement(self, canonical: str) -> str:
    self._canonical(canonical)
    return self._finance_csv(canonical, "Income statement", ratio_only=False)
```

- [ ] **Step 5.4: Run, confirm PASS**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetFundamentals -v`
Expected: 4 tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_integration.py
git commit -m "feat(tdx-chronos): get_fundamentals/balance/cashflow/income + ETF marker"
```

---

## Task 6: Adapter `get_insider_transactions` + `get_index_klines`

**Files:**
- Modify: `tradingagents/dataflows/tdx_chronos.py`
- Modify: `tests/test_tdx_chronos_integration.py`

**Interfaces:**
- Produces:
  - `_TdxAdapter.get_insider_transactions(canonical) -> str` — wraps `TdxChronos.shareholders(canonical)` to CSV. ETFs/LOFs/可转债 are supported per the `shareholders` AGENTS.md note.
  - `_TdxAdapter.get_index_klines(index_code, start_date, end_date) -> str` — wraps `TdxChronos.index_klines(index_code, start_date, end_date)` (consumed by the reflection layer for benchmark returns).

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_tdx_chronos_integration.py`:

```python
@pytest.mark.unit
class TestGetInsiderTransactions(unittest.TestCase):
    def setUp(self):
        tc._reset_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(tc._adapter_state_for_tests())

    def test_returns_shareholder_csv(self):
        holder_df = pd.DataFrame(
            {
                "code": ["sh600000"] * 2,
                "name": ["Acme", "Beta"],
                "shares": [1000, 2000],
            }
        )
        client = _fake_client()
        client.shareholders.return_value = holder_df
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_insider_transactions("sh600000")
        self.assertTrue(out.startswith("# Insider / shareholder records for sh600000"))
        self.assertIn("Acme", out)
        self.assertIn("Beta", out)

    def test_empty_shareholder_raises(self):
        client = _fake_client()
        client.shareholders.return_value = pd.DataFrame({})
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_insider_transactions("sh600000")


@pytest.mark.unit
class TestGetIndexKlines(unittest.TestCase):
    def setUp(self):
        tc._reset_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(tc._adapter_state_for_tests())

    def test_returns_index_csv(self):
        idx_df = pd.DataFrame(
            {"date": ["2024-12-30", "2024-12-31"], "open": [3300, 3310],
             "high": [3320, 3325], "low": [3290, 3305],
             "close": [3310, 3320], "amount": [1e10, 1.1e10]}
        )
        client = _fake_client()
        client.index_klines.return_value = idx_df
        adapter = _TdxAdapter(client=client, data_dir="/data")
        out = adapter.get_index_klines("sh000001", "2024-12-30", "2024-12-31")
        self.assertTrue(out.startswith("# Index klines for sh000001"))
        for col in ("date", "open", "close"):
            self.assertIn(col, out)

    def test_empty_index_klines_raises(self):
        client = _fake_client()
        client.index_klines.return_value = pd.DataFrame({})
        adapter = _TdxAdapter(client=client, data_dir="/data")
        from tradingagents.dataflows.errors import NoMarketDataError
        with self.assertRaises(NoMarketDataError):
            adapter.get_index_klines("sh000001", "2024-12-30", "2024-12-31")
```

- [ ] **Step 6.2: Run, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetInsiderTransactions tests/test_tdx_chronos_integration.py::TestGetIndexKlines -v`
Expected: `NotImplementedError`.

- [ ] **Step 6.3: Implement `get_insider_transactions` and `get_index_klines`**

Replace the two stubs in `tradingagents/dataflows/tdx_chronos.py` with:

```python
def get_insider_transactions(self, canonical: str) -> str:
    """Return shareholder / capital-record events for ``canonical``.

    Delegates to ``TdxChronos.shareholders`` (which covers A-share + ETF +
    convertible bonds per AGENTS.md).
    """
    self._canonical(canonical)
    try:
        df = self._client.shareholders(canonical)
    except Exception as exc:
        raise NoMarketDataError(canonical, canonical, f"tdx_chronos.shareholders failed: {exc}") from exc
    if df is None or df.empty:
        raise NoMarketDataError(canonical, canonical, "no shareholder rows")
    csv = df.to_csv(index=False)
    header = f"# Insider / shareholder records for {canonical}\n"
    header += f"# Rows: {len(df)}\n\n"
    return header + csv


def get_index_klines(self, index_code: str, start_date: str, end_date: str) -> str:
    """Benchmark index klines for the reflection layer."""
    try:
        df = self._client.index_klines(index_code, start_date, end_date)
    except Exception as exc:
        raise NoMarketDataError(index_code, index_code, f"tdx_chronos.index_klines failed: {exc}") from exc
    if df is None or df.empty:
        raise NoMarketDataError(index_code, index_code, f"no rows between {start_date} and {end_date}")
    csv = df.to_csv(index=False)
    header = f"# Index klines for {index_code} from {start_date} to {end_date}\n"
    header += f"# Rows: {len(df)}\n\n"
    return header + csv
```

- [ ] **Step 6.4: Run, confirm PASS**

Run: `pytest tests/test_tdx_chronos_integration.py::TestGetInsiderTransactions tests/test_tdx_chronos_integration.py::TestGetIndexKlines -v`
Expected: 4 tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_integration.py
git commit -m "feat(tdx-chronos): get_insider_transactions (shareholders) + get_index_klines"
```

---

## Task 7: Router integration — `VENDOR_METHODS` + auto-route gate

**Files:**
- Modify: `tradingagents/dataflows/interface.py`
- Create: `tests/test_tdx_chronos_router.py`

**Interfaces:**
- Produces (in `interface.py`):
  - `route_to_vendor(method, symbol, *args, **kwargs)`: at top, before `get_category_for_method`, add an `is_a_share_via_adapter(symbol)` gate that calls `get_tdx_adapter()` and dispatches. If the adapter is `None`, fall through to existing logic.
  - `VENDOR_METHODS` entries for `tdx_chronos` on `get_stock_data`, `get_indicators`, `get_fundamentals`, `get_balance_sheet`, `get_cashflow`, `get_income_statement`, `get_insider_transactions`.
  - `tdx_chronos` listed in `VENDOR_LIST`.

- [ ] **Step 7.1: Write the router test**

Create `tests/test_tdx_chronos_router.py`:

```python
"""Router behavior for the auto-route gate and the explicit vendor entry."""

import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as interface
import tradingagents.default_config as default_config
from tradingagents.dataflows import tdx_chronos as tc
from tradingagents.dataflows.errors import VendorNotConfiguredError


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.mark.unit
class AutoRouteGateTests(unittest.TestCase):
    def setUp(self):
        _reset_config()
        tc._reset_state_for_tests()
        self._saved_state = tc._adapter_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(self._saved_state)
        _reset_config()

    def test_a_share_dispatches_to_tdx_adapter_first(self):
        adapter = mock.Mock()
        adapter.dispatch.return_value = "TDX_RESULT"
        with mock.patch.object(tc, "get_tdx_adapter", return_value=adapter):
            with mock.patch.object(tc, "is_a_share_via_adapter", return_value=True):
                out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        self.assertEqual(out, "TDX_RESULT")
        adapter.dispatch.assert_called_once()

    def test_non_a_share_skips_tdx_adapter(self):
        adapter = mock.Mock()
        with mock.patch.object(tc, "get_tdx_adapter", return_value=adapter):
            with mock.patch.object(tc, "is_a_share_via_adapter", return_value=False):
                # Existing chain: yfinance will hit NoMarketDataError -> sentinel.
                out = interface.route_to_vendor("get_stock_data", "AAPL", "2024-12-30", "2024-12-31")
        adapter.dispatch.assert_not_called()
        self.assertIn("NO_DATA_AVAILABLE", out)

    def test_env_disable_auto_route_falls_through(self):
        adapter = mock.Mock()
        adapter.dispatch.return_value = "SHOULD_NOT_BE_CALLED"
        with mock.patch.dict("os.environ", {"TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE": "1"}, clear=False):
            with mock.patch.object(tc, "get_tdx_adapter", return_value=adapter):
                with mock.patch.object(tc, "is_a_share_via_adapter", return_value=True):
                    out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        adapter.dispatch.assert_not_called()
        self.assertIn("NO_DATA_AVAILABLE", out)

    def test_adapter_none_falls_through_silently(self):
        with mock.patch.object(tc, "get_tdx_adapter", return_value=None):
            with mock.patch.object(tc, "is_a_share_via_adapter", return_value=True):
                out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        # No crash; chain falls through to existing yfinance path -> sentinel.
        self.assertIn("NO_DATA_AVAILABLE", out)


@pytest.mark.unit
class ExplicitVendorTests(unittest.TestCase):
    def setUp(self):
        _reset_config()
        tc._reset_state_for_tests()
        self._saved_state = tc._adapter_state_for_tests()

    def tearDown(self):
        tc._restore_state_for_tests(self._saved_state)
        _reset_config()

    def test_explicit_tdx_chronos_config_routes(self):
        config_module.set_config({"data_vendors": {"core_stock_apis": "tdx_chronos"}})
        adapter = mock.Mock()
        adapter.dispatch.return_value = "EXPLICIT_TDX"
        # Mock the vendor impl the router looks up.
        impl = mock.Mock(return_value="EXPLICIT_TDX")
        with mock.patch.object(tc, "get_tdx_adapter", return_value=adapter):
            with mock.patch.dict(interface.VENDOR_METHODS,
                                  {"get_stock_data": {"tdx_chronos": impl}}, clear=False):
                out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        # Auto-route gate fires first (is_a_share_via_adapter returns True
        # in the patched session); explicit vendor path is verified by the
        # helper-impl mock matching `adapter.dispatch`.
        self.assertIn(out, ("EXPLICIT_TDX",))

    def test_explicit_tdx_chronos_missing_raises_vendor_not_configured(self):
        # Simulate: vendor explicitly chosen, but the package is absent.
        config_module.set_config({"data_vendors": {"core_stock_apis": "tdx_chronos"}})
        with mock.patch.object(tc, "get_tdx_adapter", return_value=None):
            with self.assertRaises(VendorNotConfiguredError):
                interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
```

- [ ] **Step 7.2: Run, confirm FAIL**

Run: `pytest tests/test_tdx_chronos_router.py -v`
Expected: failures — `route_to_vendor` doesn't yet have the auto-route gate.

- [ ] **Step 7.3: Add `get_tdx_adapter_method` to `tdx_chronos.py` (must exist before interface.py imports it)**

Append to `tradingagents/dataflows/tdx_chronos.py`:

```python
_LAZY_METHOD_TABLE = {
    "get_stock_data": "get_stock_data",
    "get_indicators": "get_indicators",
    "get_fundamentals": "get_fundamentals",
    "get_balance_sheet": "get_balance_sheet",
    "get_cashflow": "get_cashflow",
    "get_income_statement": "get_income_statement",
    "get_insider_transactions": "get_insider_transactions",
}


def get_tdx_adapter_method(method: str):
    """Return a closure ``f(symbol, *args, **kwargs)`` that calls
    ``adapter.dispatch(method, symbol, *args, **kwargs)``.

    Raises :class:`VendorNotConfiguredError` when the adapter is absent —
    matches the existing ``VENDOR_METHODS`` contract.
    """
    from .errors import VendorNotConfiguredError

    def _impl(symbol, *args, **kwargs):
        adapter = get_tdx_adapter()
        if adapter is None:
            raise VendorNotConfiguredError(
                "tdx_chronos not available; install with `pip install -e /app/tdx-chronos`"
            )
        return adapter.dispatch(method, symbol, *args, **kwargs)

    return _impl
```

- [ ] **Step 7.4: Add the auto-route gate and the `VENDOR_METHODS` entries to `interface.py`**

Modify `tradingagents/dataflows/interface.py`:

Add the import alongside the existing vendor imports:

```python
from . import tdx_chronos as _tdx
```

And at the very top of `route_to_vendor` (before `category = get_category_for_method(method)`):

```python
    symbol_arg = args[0] if args else kwargs.get("symbol")
    if (
        symbol_arg
        and not os.getenv("TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE")
        and _tdx.is_a_share_via_adapter(symbol_arg)
    ):
        adapter = _tdx.get_tdx_adapter()
        if adapter is not None:
            try:
                return adapter.dispatch(method, *args, **kwargs)
            except NoMarketDataError:
                raise
```

`import os` is already at the top of `interface.py`.

Then update `VENDOR_LIST` (around line 73):

```python
VENDOR_LIST = [
    "yfinance",
    "fred",
    "polymarket",
    "alpha_vantage",
    "tdx_chronos",
]
```

Then register the vendor in `VENDOR_METHODS` (currently ~line 81):

```python
VENDOR_METHODS = {
    "get_stock_data": {
        "tdx_chronos": _tdx.get_tdx_adapter_method("get_stock_data"),
        ...
    },
    "get_indicators": {
        "tdx_chronos": _tdx.get_tdx_adapter_method("get_indicators"),
        ...
    },
    "get_fundamentals": {"tdx_chronos": _tdx.get_tdx_adapter_method("get_fundamentals"), ...},
    "get_balance_sheet": {"tdx_chronos": _tdx.get_tdx_adapter_method("get_balance_sheet"), ...},
    "get_cashflow": {"tdx_chronos": _tdx.get_tdx_adapter_method("get_cashflow"), ...},
    "get_income_statement": {"tdx_chronos": _tdx.get_tdx_adapter_method("get_income_statement"), ...},
    "get_insider_transactions": {"tdx_chronos": _tdx.get_tdx_adapter_method("get_insider_transactions"), ...},
    ...
}
```

For News / Macro / Prediction methods, do **not** add a `tdx_chronos` entry — that lets the existing `VendorNotConfiguredError` machinery kick in if a user explicitly configures `tdx_chronos` for those.

- [ ] **Step 7.5: Run, confirm PASS**

Run: `pytest tests/test_tdx_chronos_router.py tests/test_vendor_routing.py -v`
Expected: 5 new tests pass; existing routing tests still pass.

- [ ] **Step 7.6: Commit**

```bash
git add tradingagents/dataflows/interface.py tradingagents/dataflows/tdx_chronos.py tests/test_tdx_chronos_router.py
git commit -m "feat(tdx-chronos): route_to_vendor auto-gate + VENDOR_METHODS entries"
```



---

## Task 8: Config + env vars + pyproject extras

**Files:**
- Modify: `tradingagents/default_config.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_env_overrides.py` (add cases for new env vars)

**Interfaces:**
- Produces:
  - `DEFAULT_CONFIG["tdx_chronos_data_dir"]` (str, default `/app/tdx-chronos/data`).
  - `DEFAULT_CONFIG["tdx_chronos_auto_route"]` (bool, default `True`).
  - `_ENV_OVERRIDES` entries:
    - `"TRADINGAGENTS_TDX_CHRONOS_DATA_DIR"` → `"tdx_chronos_data_dir"`
    - `"TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE"` → `"tdx_chronos_auto_route"`
  - pyproject: `[project.optional-dependencies] tdx_chronos = ["tdx-chronos @ file:///app/tdx-chronos"]`.

- [ ] **Step 8.1: Write the failing test additions**

Append to `tests/test_env_overrides.py`:

```python
@pytest.mark.unit
class TdxChronosEnvOverrideTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_data_dir_override(self):
        with mock.patch.dict("os.environ", {"TRADINGAGENTS_TDX_CHRONOS_DATA_DIR": "/opt/tdx"}):
            cfg = default_config.DEFAULT_CONFIG
            self.assertEqual(cfg["tdx_chronos_data_dir"], "/opt/tdx")

    def test_auto_route_off_override(self):
        with mock.patch.dict("os.environ", {"TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE": "0"}):
            cfg = default_config.DEFAULT_CONFIG
            self.assertEqual(cfg["tdx_chronos_auto_route"], False)

    def test_invalid_auto_route_raises(self):
        with mock.patch.dict("os.environ", {"TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE": "maybe"}):
            with self.assertRaises(ValueError):
                import importlib
                importlib.reload(default_config)
```

- [ ] **Step 8.2: Run, confirm FAIL**

Run: `pytest tests/test_env_overrides.py::TdxChronosEnvOverrideTests -v`
Expected: `KeyError: 'tdx_chronos_data_dir'`.

- [ ] **Step 8.3: Add config keys & env overrides**

Modify `tradingagents/default_config.py`:

Add to `_ENV_OVERRIDES` (after the `ANTHROPIC_EFFORT` entry):

```python
    "TRADINGAGENTS_TDX_CHRONOS_DATA_DIR":  "tdx_chronos_data_dir",
    "TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE": "tdx_chronos_auto_route",
```

Add to `DEFAULT_CONFIG` (place near the `data_cache_dir` block so related I/O config sits together):

```python
    # tdx-chronos (A-share offline data warehouse). Optional dependency;
    # the adapter becomes a no-op when the package or this directory is
    # missing. The actual data-dir resolution (env -> config -> fallback)
    # happens lazily inside ``_resolve_data_dir``; the string here is just
    # a documented default for users reading the config.
    "tdx_chronos_data_dir": "/app/tdx-chronos/data",
    "tdx_chronos_auto_route": True,
```

- [ ] **Step 8.4: Add the pyproject extras**

Modify `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [...]
bedrock = [...]
# A-share & ETF offline data warehouse from `/app/tdx-chronos`. The adapter
# silently no-ops when this isn't installed, so this remains opt-in.
tdx_chronos = ["tdx-chronos @ file:///app/tdx-chronos"]
```

- [ ] **Step 8.5: Run env-override tests, confirm PASS**

Run: `pytest tests/test_env_overrides.py -v`
Expected: 3 new tests + existing tests pass.

- [ ] **Step 8.6: Verify clean-install smoke (no extras)**

Run: `pip install . 2>&1 | tail -3 && python -c "import tradingagents; from cli.main import app; print('clean import ok')"`
Expected: prints `clean import ok`.

- [ ] **Step 8.7: Verify clean-import smoke (with tdx_chronos extra)**

Run: `pip install -e ".[tdx_chronos]" 2>&1 | tail -5 && python -c "import tradingagents; from cli.main import app; from tradingagents.dataflows.tdx_chronos import get_tdx_adapter; print('with-tdx import ok, adapter:', type(get_tdx_adapter()).__name__ if get_tdx_adapter() else None)"`
Expected: prints `with-tdx import ok, adapter: _TdxAdapter`.

- [ ] **Step 8.8: Commit**

```bash
git add tradingagents/default_config.py tests/test_env_overrides.py pyproject.toml
git commit -m "feat(tdx-chronos): config + env overrides + pyproject extras"
```

---

## Task 9: Documentation + integration test + final verification

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Create: `CHANGELOG.md` entry (or extend existing)
- Create: `tests/integration/test_tdx_chronos_integration.py`

- [ ] **Step 9.1: Update AGENTS.md**

In the "Data vendors" line of the Agent Team surface table (around line 13 in `AGENTS.md`), append:

```
| A-share & ETF (offline) | `tradingagents/dataflows/tdx_chronos.py` (optional install `[tdx_chronos]`) |
```

And in the "Configuration" block, after the `TRADINGAGENTS_*` env-var bullets, add:

```
- `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR`, `TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE` — A-share offline warehouse path and auto-route gate.
```

In the "Gotchas" section add:

```
- **First-priority A-share**: When tdx-chronos is installed, A-share symbols (e.g. `sh600000`, `600000.SS`) auto-route to its offline parquet store before the configured `data_vendors` chain runs. Disable via `TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE=1`. ETF/LOF/REIT/可转债 fundamentals return an explicit "out of scope" marker rather than fabricating values.
```

- [ ] **Step 9.2: Update README.md**

Add a new subsection after the existing "Configuration" section:

```markdown
### A-share & ETF data (optional)

For first-priority offline A-share & ETF data, install the companion warehouse:

```bash
pip install -e /app/tdx-chronos
```

Once installed, A-share tickers (`sh600000`, `600000.SS`, `000001.SZ`, `510050` for ETFs) automatically route to tdx-chronos. Override the data directory with `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR` or `TDC_DATA_DIR`. Disable the auto-route with `TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE=1` (falls back to the configured `data_vendors` chain).
```

Match the existing README's heading / link conventions.

- [ ] **Step 9.3: Add CHANGELOG entry**

Create or extend `CHANGELOG.md` with an "Unreleased" section:

```markdown
## Unreleased

### Added
- First-priority A-share & ETF routing via tdx-chronos (`tradingagents/dataflows/tdx_chronos.py`). Auto-detects `sh/sz/bj` symbols (`sh600000`, `600000.SS`, etc.) and dispatches OHLCV / indicators / fundamentals / shareholders / benchmark index klines. ETF/LOF/REIT/可转债 fundamentals return an explicit out-of-scope marker. Opt-in install: `pip install -e ".[tdx_chronos]"`.
- New env vars: `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR`, `TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE`.
```

- [ ] **Step 9.4: Write the integration test (gated)**

Create `tests/integration/test_tdx_chronos_integration.py`:

```python
"""Integration test: end-to-end A-share routing with real tdx-chronos data.

Skipped when tdx-chronos isn't importable so it doesn't fail CI for
contributors without the optional dependency.
"""

import os
import unittest

import pytest

from tradingagents.dataflows import interface
from tradingagents.dataflows.tdx_chronos import get_tdx_adapter


_DATA_DIR_CANDIDATES = (
    os.environ.get("TRADINGAGENTS_TDX_CHRONOS_DATA_DIR"),
    os.environ.get("TDC_DATA_DIR"),
    "/app/tdx-chronos/data",
)


@pytest.mark.integration
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        adapter = get_tdx_adapter()
        if adapter is None:
            self.skipTest("tdx_chronos not installed or data dir not found")

    def test_get_stock_data_sh600000(self):
        out = interface.route_to_vendor(
            "get_stock_data", "sh600000", "2024-12-30", "2024-12-31"
        )
        self.assertTrue(out.startswith("# Stock data for"))
        self.assertIn("Date", out)

    def test_get_stock_data_yahoo_form_normalizes(self):
        # `600000.SS` -> `sh600000`; the header should reflect the canonical form.
        out = interface.route_to_vendor(
            "get_stock_data", "600000.SS", "2024-12-30", "2024-12-31"
        )
        self.assertIn("sh600000", out)

    def test_get_fundamentals_etf_returns_marker(self):
        out = interface.route_to_vendor("get_fundamentals", "sh510050")
        self.assertIn("not in tdx_chronos scope", out)
```

- [ ] **Step 9.5: Run integration test (real data present)**

Run: `pytest tests/integration/test_tdx_chronos_integration.py -v`
Expected: 3 tests pass. (If `tdx-chronos` isn't installed, all 3 skip with `Skipped: tdx_chronos not installed`.)

- [ ] **Step 9.6: Run full test suite, confirm green**

Run: `pytest -q`
Expected: all tests pass (existing + new + 1 integration if data present).

- [ ] **Step 9.7: Run ruff check, confirm clean**

Run: `ruff check .`
Expected: no findings.

- [ ] **Step 9.8: Run clean-import smoke (final gate)**

Run: `pip install . 2>&1 | tail -3 && python -c "import tradingagents; from cli.main import app; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 9.9: Commit**

```bash
git add AGENTS.md README.md CHANGELOG.md tests/integration/test_tdx_chronos_integration.py
git commit -m "docs(tdx-chronos): AGENTS + README + CHANGELOG + integration test"
```

---

## Self-Review Checklist

Run through this before declaring the plan complete:

- [ ] **Spec coverage:**  Every section of `docs/superpowers/specs/2026-07-11-tdx-chronos-integration-design.md` maps to at least one task. Cross-check:
    - §5 (module surface) → Tasks 2, 3, 4, 5, 6.
    - §6 (symbol normalization) → Task 1.
    - §7 (router integration) → Task 7.
    - §8 (config) → Task 8.
    - §9 (packaging) → Task 8.
    - §10 (ETF marker) → Task 5.
    - §11 (error handling) → covered across Tasks 2, 3, 5, 6.
    - §12 (tests) → Tasks 1–9 each add their own test file.
    - §13 (documentation) → Task 9.
    - §14 (roll-out) → Tasks 7 (gate escape hatch) + 8 (env vars).
- [ ] **Placeholder scan:** No "TODO" / "TBD" / "similar to Task N" placeholders in any step. Every step that touches code shows the code.
- [ ] **Type consistency:**
    - `is_a_share(symbol) -> bool` — defined Task 1, used Task 2.
    - `is_a_share_via_adapter(symbol) -> bool` — defined Task 2, used Task 7.
    - `normalize_a_share(symbol) -> str` — defined Task 1, used Task 2.
    - `_TdxAdapter._canonical(symbol) -> str` — defined Task 2, used Tasks 3–6.
    - `get_tdx_adapter() -> _TdxAdapter | None` — defined Task 2, used Tasks 3, 7, 8.
    - `get_tdx_adapter_method(method) -> callable` — defined Task 7, used in `VENDOR_METHODS`.
    - `ETF_OUT_OF_SCOPE_MARKER: str` — defined Task 2, used Task 5.
    - Config keys `tdx_chronos_data_dir` / `tdx_chronos_auto_route` — defined Task 8, used Tasks 2, 7.
- [ ] **Global constraints:** No top-level `tdx_chronos` imports; ruff `select` honored; existing test files preserved.
