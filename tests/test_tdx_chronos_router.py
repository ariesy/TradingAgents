"""Router behavior for the auto-route gate and the explicit vendor entry."""

import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as interface
import tradingagents.default_config as default_config
from tradingagents.dataflows import tdx_chronos as tc
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _no_data(symbol):
    def impl(s, *a, **k):
        raise NoMarketDataError(s, s, "no rows")
    return impl


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
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        self.assertEqual(out, "TDX_RESULT")
        adapter.dispatch.assert_called_once()

    def test_non_a_share_skips_tdx_adapter(self):
        adapter = mock.Mock()
        failing_yf = mock.Mock(side_effect=_no_data("AAPL"))
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=False),
            mock.patch.dict(
                interface.VENDOR_METHODS,
                {"get_stock_data": {"yfinance": failing_yf, "alpha_vantage": failing_yf}},
                clear=False,
            ),
        ):
            out = interface.route_to_vendor(
                "get_stock_data", "AAPL", "2024-12-30", "2024-12-31"
            )
        adapter.dispatch.assert_not_called()
        self.assertIn("NO_DATA_AVAILABLE", out)

    def test_env_disable_auto_route_falls_through(self):
        adapter = mock.Mock()
        adapter.dispatch.return_value = "SHOULD_NOT_BE_CALLED"
        with (
            mock.patch.dict(
                "os.environ",
                {"TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE": "1"},
                clear=False,
            ),
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        adapter.dispatch.assert_not_called()
        self.assertIn("NO_DATA_AVAILABLE", out)

    def test_env_disable_zero_does_not_disable(self):
        """``=0`` parses as "do not disable" — must NOT silence auto-route.

        Previously the gate used ``os.getenv`` truthiness, so any non-empty
        value (including ``"0"``) silently turned auto-route off.
        """
        adapter = mock.Mock()
        adapter.dispatch.return_value = "TDX_HIT"
        with (
            mock.patch.dict(
                "os.environ",
                {"TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE": "0"},
                clear=False,
            ),
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        adapter.dispatch.assert_called_once()
        self.assertEqual(out, "TDX_HIT")

    def test_env_disable_invalid_raises(self):
        with (
            mock.patch.dict(
                "os.environ",
                {"TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE": "maybe"},
                clear=False,
            ),
            self.assertRaises(ValueError),
        ):
            interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")

    def test_adapter_none_falls_through_silently(self):
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=None),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
        self.assertIn("NO_DATA_AVAILABLE", out)

    def test_no_market_error_in_auto_route_returns_no_data_sentinel(self):
        """When TDX has no data AND the configured vendor chain also has no
        data, the call returns the same NO_DATA_AVAILABLE sentinel as before
        — but the seed ``last_no_data`` now comes from the TDX attempt, so the
        chain runs instead of being short-circuited.
        """
        adapter = mock.Mock()
        adapter.dispatch.side_effect = NoMarketDataError("sh600000", "sh600000", "no rows")
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor(
                "get_stock_data", "sh600000", "2024-12-30", "2024-12-31"
            )
        # The chain vendors were tried and each raised NoMarketDataError too,
        # so the sentinel bubbles up with the *chain's* last error (the TDX
        # detail "no rows" is the seed, so it's preserved in the message).
        self.assertIn("NO_DATA_AVAILABLE", out)
        self.assertIn("sh600000", out)
        self.assertIn("no rows", out)

    def test_tdx_no_data_falls_through_to_chain_success(self):
        """When TDX raises NoMarketDataError but a configured vendor can serve
        the symbol, the chain wins — the sentinel is NOT returned. This is
        the user-facing guarantee: TDX is a *prioritized* source, not a hard
        gate.
        """
        adapter = mock.Mock()
        adapter.dispatch.side_effect = NoMarketDataError("sh600000", "sh600000", "no rows")
        # Configure a single-vendor chain with a stub that returns data.
        config_module.set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        yf_impl = mock.Mock(return_value="YF_FALLBACK")
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
            mock.patch.dict(
                interface.VENDOR_METHODS,
                {"get_stock_data": {"yfinance": yf_impl}},
            ),
        ):
            out = interface.route_to_vendor(
                "get_stock_data", "sh600000", "2024-12-30", "2024-12-31"
            )
        adapter.dispatch.assert_called_once()
        yf_impl.assert_called_once()
        self.assertEqual(out, "YF_FALLBACK")
        self.assertNotIn("NO_DATA_AVAILABLE", out)


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
        impl = mock.Mock(return_value="EXPLICIT_TDX")
        with (
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
            mock.patch.object(tc, "get_tdx_adapter", return_value=adapter),
            mock.patch.dict(
                interface.VENDOR_METHODS,
                {"get_stock_data": {"tdx_chronos": impl}},
                clear=False,
            ),
        ):
            out = interface.route_to_vendor(
                "get_stock_data", "sh600000", "2024-12-30", "2024-12-31"
            )
        self.assertIn(out, ("EXPLICIT_TDX",))

    def test_explicit_tdx_chronos_missing_raises_vendor_not_configured(self):
        config_module.set_config({"data_vendors": {"core_stock_apis": "tdx_chronos"}})
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=None),
            self.assertRaises(VendorNotConfiguredError),
        ):
            interface.route_to_vendor(
                "get_stock_data", "sh600000", "2024-12-30", "2024-12-31"
            )
