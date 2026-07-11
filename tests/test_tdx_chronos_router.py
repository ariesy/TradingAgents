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

    def test_adapter_none_falls_through_silently(self):
        with (
            mock.patch.object(tc, "get_tdx_adapter", return_value=None),
            mock.patch.object(tc, "is_a_share_via_adapter", return_value=True),
        ):
            out = interface.route_to_vendor("get_stock_data", "sh600000", "2024-12-30", "2024-12-31")
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
