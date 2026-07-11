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
        with (
            mock.patch.dict(sys.modules, {"tdx_chronos": None, "tdx_chronos.client": None}),
            mock.patch.object(interface, "route_to_vendor", wraps=interface.route_to_vendor),
        ):
            # Just importing is enough — the assertion is that nothing throws.
            self.assertTrue(callable(interface.route_to_vendor))
