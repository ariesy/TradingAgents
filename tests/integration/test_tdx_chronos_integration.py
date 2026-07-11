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
