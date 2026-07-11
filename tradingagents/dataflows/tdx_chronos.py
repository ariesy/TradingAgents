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
import datetime
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


def _import_tdx_chronos() -> tuple[Any, Any]:
    """Import the tdx_chronos client module without raising when missing.

    Returns the module object, or ``None`` when the package isn't installed
    or the configured data directory doesn't exist.
    """
    try:
        from tdx_chronos.client import TdxChronos
    except Exception as exc:
        logger.debug("tdx_chronos not importable: %s", exc)
        return None, None
    return TdxChronos, None


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

    def _populate_caches(self) -> None:
        """Populate both known-symbol caches from the client.

        Must be called with :attr:`_known_lock` held. Kept lock-free here so
        it can be invoked from either cache loader without triggering
        re-entrant deadlock on the non-reentrant ``_known_lock`` (a single
        thread calling ``_load_etf_cache()`` then ``_load_a_share_cache()``
        used to nest the lock; this method makes that legal).
        """
        try:
            self._known_a_shares = set(self._client.list_symbols())
            self._known_etfs = set(self._client.list_etfs())
        except Exception as exc:
            logger.debug("tdx_chronos list_symbols/list_etfs failed: %s", exc)
            self._known_a_shares = set()
            self._known_etfs = set()

    def _load_a_share_cache(self) -> set[str]:
        with self._known_lock:
            if self._known_a_shares is None:
                self._populate_caches()
            return self._known_a_shares

    def _load_etf_cache(self) -> set[str]:
        with self._known_lock:
            if self._known_etfs is None:
                self._populate_caches()
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
        """OHLCV via ``TdxChronos.kline``; returns the same CSV-string header shape
        :func:`tradingagents.dataflows.y_finance.get_YFin_data_online` produces.
        """
        user_input = canonical
        canon = self._canonical(user_input)
        try:
            df = self._client.kline(canon, start_date, end_date)
        except Exception as exc:
            raise NoMarketDataError(canon, canon, f"tdx_chronos.kline failed: {exc}") from exc

        if df is None or df.empty:
            raise NoMarketDataError(canon, canon, f"no rows between {start_date} and {end_date}")

        out = df.rename(
            columns={
                "date": "Date", "open": "Open", "high": "High",
                "low": "Low", "close": "Close",
                "amount": "Amount", "vol": "Volume",
            }
        )
        if "Volume" not in out.columns and "vol" in df.columns:
            out["Volume"] = df["vol"]
        cols = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume", "Amount") if c in out.columns]
        csv = out[cols].to_csv(index=False)

        label = canon if canon == user_input else f"{canon} (from {user_input})"
        header = f"# Stock data for {label} from {start_date} to {end_date}\n"
        header += f"# Total records: {len(out)}\n"
        header += f"# Data retrieved on: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        return header + csv

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
