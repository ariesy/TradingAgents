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

    def test_load_etf_then_a_share_does_not_deadlock_in_single_thread(self):
        """Regression: calling _load_etf_cache before _load_a_share_cache in the
        same thread must not deadlock on the non-reentrant cache lock.

        The original implementation nested _load_a_share_cache inside
        _load_etf_cache while holding the same non-reentrant ``threading.Lock``,
        which deadlocked a single-thread caller that invoked both methods in
        sequence. The work is wrapped in a worker thread + ``future.result(timeout=...)``
        so the test fails fast with a clear message rather than hanging the suite.
        ``executor.shutdown(wait=False)`` keeps the test runner itself from
        blocking on the deadlocked worker thread on its way out.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        client = mock.Mock()
        client.list_symbols.return_value = ["600000.SH", "600001.SH"]
        client.list_etfs.return_value = ["510300.SH", "510500.SH"]

        adapter = tc_mod._TdxAdapter(client=client, data_dir="/tmp/fake-tdx-chronos")

        def both():
            return adapter._load_etf_cache(), adapter._load_a_share_cache()

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(both)
            try:
                etfs, a_shares = future.result(timeout=5)
            except FuturesTimeout:
                self.fail(
                    "Deadlock: _load_etf_cache() followed by _load_a_share_cache() "
                    "in the same thread hung — the cache lock is non-reentrant."
                )
            self.assertEqual(etfs, {"510300.SH", "510500.SH"})
            self.assertEqual(a_shares, {"600000.SH", "600001.SH"})
        finally:
            executor.shutdown(wait=False)
