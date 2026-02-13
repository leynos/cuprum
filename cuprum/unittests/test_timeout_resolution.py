"""Timeout resolution unit tests."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ExecutionContext, ScopeConfig, sh
from cuprum._testing import _resolve_timeout

_CONTEXT_OMITTED = object()


def test_execution_context_timeout_none_falls_through_to_scoped() -> None:
    """ExecutionContext(timeout=None) should use scoped default."""
    with sh.scoped(ScopeConfig(timeout=3.0)):
        ctx = ExecutionContext(timeout=None)
        assert _resolve_timeout(timeout=None, context=ctx) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("timeout", "context_timeout", "scoped_timeout", "expected"),
    [
        pytest.param(1.5, 2.0, 3.0, 1.5, id="explicit-timeout-wins"),
        pytest.param(None, 2.0, 3.0, 2.0, id="context-timeout-wins"),
        pytest.param(None, _CONTEXT_OMITTED, 3.0, 3.0, id="scoped-default"),
        pytest.param(None, None, None, None, id="no-timeout"),
    ],
)
def test_resolve_timeout_precedence(
    timeout: float | None,
    context_timeout: float | object | None,
    scoped_timeout: float | None,
    expected: float | None,
) -> None:
    """Timeout precedence respects explicit, context, and scoped defaults."""
    context = None
    if context_timeout is not _CONTEXT_OMITTED:
        context = ExecutionContext(timeout=typ.cast("float | None", context_timeout))

    with sh.scoped(ScopeConfig(timeout=scoped_timeout)):
        assert _resolve_timeout(timeout=timeout, context=context) == expected
