"""Timeout resolution unit tests."""

from __future__ import annotations

from cuprum import ExecutionContext, sh
from cuprum._testing import _resolve_timeout


def test_execution_context_timeout_none_falls_through_to_scoped() -> None:
    """ExecutionContext(timeout=None) should use scoped default."""
    with sh.scoped(timeout=3.0):
        ctx = ExecutionContext(timeout=None)
        assert _resolve_timeout(timeout=None, context=ctx) == 3.0
