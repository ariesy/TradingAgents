"""Shim that re-exports :func:`stockstats.wrap` under the dataflows namespace.

Kept tiny on purpose — the runtime code imports ``from stockstats import wrap``
directly. This module exists so tests can patch
``tradingagents.dataflows.stockstats.wrap`` without changing the import path.
"""

from stockstats import wrap

__all__ = ["wrap"]
