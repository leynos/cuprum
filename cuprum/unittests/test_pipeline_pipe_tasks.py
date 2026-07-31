"""Which pipe-task outcomes a pipeline tolerates, and which must surface.

`_surface_unexpected_pipe_failures` decides that: a downstream stage exiting
early is expected and must not fail the pipeline, while anything else stopped
the data moving and has to reach the caller. This lives beside the module it
tests rather than with the descriptor-lifecycle fault injection, which is a
separate concern.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._pipeline_pipe_tasks import _surface_unexpected_pipe_failures

_SUPPRESSED_PIPE_ERRORS = (BrokenPipeError, ConnectionResetError)
_RESULT_TAGS = [
    "ok",
    "broken_pipe",
    "conn_reset",
    "value_error",
    "runtime_error",
    "cancelled",
]


def _make_pipe_result(tag: str) -> object:
    """Materialise a pipe-task result for the given tag as a fresh object."""
    if tag == "ok":
        return object()
    if tag == "broken_pipe":
        return BrokenPipeError("downstream closed early")
    if tag == "conn_reset":
        return ConnectionResetError("peer reset")
    if tag == "value_error":
        return ValueError("unexpected pipe failure")
    if tag == "cancelled":
        # A cancelled pump task. `CancelledError` derives from `BaseException`,
        # not `Exception`, so it is exactly the case an `Exception` guard drops.
        return asyncio.CancelledError()
    return RuntimeError("unexpected pipe failure")


@given(tags=st.lists(st.sampled_from(_RESULT_TAGS), max_size=8))
def test_surface_raises_first_unexpected_and_suppresses_pipe_errors(
    *,
    tags: list[str],
) -> None:
    """The first non-pipe failure surfaces; pipe errors and values do not.

    The oracle is deliberately over ``BaseException``: a cancelled pump task
    delivered no bytes, so letting it pass as success would report a pipeline
    that never finished moving data as having completed.
    """
    results = [_make_pipe_result(tag) for tag in tags]
    unexpected = [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(result, _SUPPRESSED_PIPE_ERRORS)
    ]

    if unexpected:
        with pytest.raises(
            (ValueError, RuntimeError, asyncio.CancelledError),
        ) as exc_info:
            _surface_unexpected_pipe_failures(results)
        assert exc_info.value is unexpected[0], (
            "the earliest unexpected exception must be the one raised"
        )
    else:
        # All results are either plain values or suppressed pipe errors.
        _surface_unexpected_pipe_failures(results)
