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
        for sym, _expected in (
            ("600000", "sh600000"),
            ("sh600000", "sh600000"),
            ("600000.SS", "sh600000"),
            ("830799.BJ", "bj830799"),
        ):
            self.assertTrue(is_a_share(sym), f"expected {sym!r} to be A-share")

    def test_is_a_share_rejects_non_a_share(self):
        for sym in ("AAPL", "TSLA", "BTC-USD", "XAUUSD", "GC=F", "^GSPC", "6000000"):
            self.assertFalse(is_a_share(sym), f"unexpected A-share match for {sym!r}")
