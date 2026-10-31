"""Synchronous entry points refuse to run inside a running event loop.

``SafeCmd.run_sync``, ``SafeCmd.run_bytes_sync``, ``Pipeline.run_sync``, and
``Pipeline.run_bytes_sync`` each own the event loop they create. Called from a
coroutine they must raise :class:`RunningEventLoopError` -- a documented Cuprum
error naming the asynchronous alternative -- without constructing the coroutine
they would otherwise discard.
"""

from __future__ import annotations

import asyncio
import typing as typ
import warnings

import pytest

from cuprum import Pipeline, RunningEventLoopError, SafeCmd

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.catalogue import PythonCatalogue


def _call_safe_cmd_sync(command: SafeCmd, _pipeline: Pipeline) -> object:
    """Invoke ``SafeCmd.run_sync``."""
    return command.run_sync()


def _call_safe_cmd_bytes_sync(command: SafeCmd, _pipeline: Pipeline) -> object:
    """Invoke ``SafeCmd.run_bytes_sync``."""
    return command.run_bytes_sync()


def _call_pipeline_sync(_command: SafeCmd, pipeline: Pipeline) -> object:
    """Invoke ``Pipeline.run_sync``."""
    return pipeline.run_sync()


def _call_pipeline_bytes_sync(_command: SafeCmd, pipeline: Pipeline) -> object:
    """Invoke ``Pipeline.run_bytes_sync``."""
    return pipeline.run_bytes_sync()


_ENTRY_POINTS = [
    pytest.param(
        _call_safe_cmd_sync,
        "run_sync()",
        "run()",
        id="safe-cmd-run-sync",
    ),
    pytest.param(
        _call_safe_cmd_bytes_sync,
        "run_bytes_sync()",
        "run_bytes()",
        id="safe-cmd-run-bytes-sync",
    ),
    pytest.param(
        _call_pipeline_sync,
        "run_sync()",
        "run()",
        id="pipeline-run-sync",
    ),
    pytest.param(
        _call_pipeline_bytes_sync,
        "run_bytes_sync()",
        "run_bytes()",
        id="pipeline-run-bytes-sync",
    ),
]


@pytest.mark.parametrize(
    ("call_entry_point", "entry_point", "async_alternative"),
    _ENTRY_POINTS,
)
def test_sync_entry_point_refuses_a_running_event_loop(
    python_catalogue_env: PythonCatalogue,
    call_entry_point: cabc.Callable[[SafeCmd, Pipeline], object],
    entry_point: str,
    async_alternative: str,
) -> None:
    """Every synchronous entry point raises a documented error inside a loop."""
    command = python_catalogue_env.builder("-c", "print('hi')")
    pipeline = command | command

    async def call_inside_loop() -> tuple[
        RunningEventLoopError, list[warnings.WarningMessage]
    ]:
        # Yield once so the calling loop is demonstrably running, not merely
        # scheduled.
        await asyncio.sleep(0)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(RunningEventLoopError) as excinfo:
                call_entry_point(command, pipeline)
        return excinfo.value, list(caught)

    error, caught = asyncio.run(call_inside_loop())

    assert isinstance(error, RuntimeError), (
        "the typed error should remain catchable as RuntimeError"
    )
    assert error.entry_point == entry_point, (
        "the refusal must name the refused entry point"
    )
    assert error.async_alternative == async_alternative, (
        "the refusal must name the asynchronous alternative"
    )
    assert entry_point in str(error), (
        "the error message must include the refused entry point"
    )
    assert async_alternative in str(error), (
        "the error message must include the asynchronous alternative"
    )
    assert not [
        warning for warning in caught if "was never awaited" in str(warning.message)
    ], "the refused call must not construct a coroutine it never awaits"


def test_run_sync_still_executes_without_a_running_loop(
    python_catalogue_env: PythonCatalogue,
) -> None:
    """The loop guard does not disturb the ordinary synchronous path."""
    command = python_catalogue_env.builder("-c", "print('hi')")

    result = command.run_sync()

    assert result.stdout == "hi\n", (
        "run_sync must return the child stdout when no event loop is running"
    )
