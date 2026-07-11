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

import pandas as pd

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
            f"## {indicator} values from {start_dt.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + body
            + "\n\n"
            + description
        )

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
