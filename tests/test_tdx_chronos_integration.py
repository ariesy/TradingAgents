"""Adapter unit tests with a mocked TdxChronos facade."""

import datetime  # noqa: F401  scaffold for Tasks 4-6
import unittest
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import tdx_chronos as tc
from tradingagents.dataflows.tdx_chronos import (
    ETF_OUT_OF_SCOPE_MARKER,  # noqa: F401  scaffold for Tasks 4-6
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
