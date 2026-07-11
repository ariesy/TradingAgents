"""Adapter unit tests with a mocked TdxChronos facade."""

import datetime  # noqa: F401  scaffold for Tasks 4-6
import unittest
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import tdx_chronos as tc
from tradingagents.dataflows.tdx_chronos import (
    ETF_OUT_OF_SCOPE_MARKER,
    _TdxAdapter,
    get_tdx_adapter,  # noqa: F401  scaffold for Tasks 4-6
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
        with (
            mock.patch("tradingagents.dataflows.stockstats.wrap", side_effect=lambda df: df),
            mock.patch(
                "tradingagents.dataflows.tdx_chronos._indicator_value_for_date",
                side_effect=lambda df, ind, d: 50.0 if d == "2024-12-31" else None,
            ),
        ):
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
        self.assertGreaterEqual(client.finance.call_count, 3)
