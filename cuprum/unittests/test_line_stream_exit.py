"""Unit contracts for failed ``SafeCmd.lines()`` exit cleanup."""

from __future__ import annotations

import asyncio
import types
import typing as typ

import pytest

from cuprum import _line_stream
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait import _DrainContext


class _TimeoutHandledError(Exception):
    """Sentinel raised by the patched timeout translator."""


def _failed_run() -> tuple[object, list[LineStreamPhase], list[object]]:
    """Build the smallest run shape needed by failed-exit cleanup."""
    phases: list[LineStreamPhase] = []
    order: list[object] = []

    def emit(phase: LineStreamPhase, _details: object = None) -> None:
        """Record one lifecycle phase in its emission order."""
        phases.append(phase)
        order.append(phase)

    run = types.SimpleNamespace(
        process=types.SimpleNamespace(pid=123),
        tasks=types.SimpleNamespace(discard_on_cancel=asyncio.Event()),
        telemetry=types.SimpleNamespace(emit=emit),
    )
    return run, phases, order


@pytest.mark.parametrize(
    ("error", "capture", "expected_phases"),
    [
        (
            TimeoutError("deadline expired"),
            True,
            (
                LineStreamPhase.TIMEOUT,
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
        (
            asyncio.CancelledError(),
            False,
            (
                LineStreamPhase.CANCELLED,
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
        (
            ValueError("consumer failed"),
            False,
            (
                LineStreamPhase.TEARDOWN_STARTED,
                LineStreamPhase.TEARDOWN_COMPLETED,
            ),
        ),
    ],
)
def test_failed_line_stream_exit_reconciles_with_the_outcome_capture_policy(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    capture: bool,
    expected_phases: tuple[LineStreamPhase, ...],
) -> None:
    """Timeout captures output; cancellation and failures discard it."""
    run, phases, order = _failed_run()
    contexts: list[_DrainContext] = []
    timeout_calls: list[tuple[str | None, str | None]] = []
    execution = typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(capture=True, observation=object(), timeout=0.5),
    )

    async def wait_for_exit(
        _process: asyncio.subprocess.Process,
        _execution: _SubprocessExecution,
    ) -> tuple[int, float]:
        """Raise the parametrized exit-path failure."""
        await asyncio.sleep(0)
        raise error

    async def reconcile(_tasks: object, context: _DrainContext) -> tuple[str, str]:
        """Record the cleanup context and produce captured output."""
        contexts.append(context)
        order.append("reconcile")
        await asyncio.sleep(0)
        return "stdout", "stderr"

    async def shield(operation: cabc.Awaitable[tuple[str, str]]) -> tuple[str, str]:
        """Await the patched cleanup operation without changing its outcome."""
        return await operation

    def handle_timeout(
        _error: TimeoutError,
        *,
        stdout_text: str | None,
        stderr_text: str | None,
        timeout: float | None,
    ) -> None:
        """Record translated timeout output and stop the test path."""
        del timeout
        timeout_calls.append((stdout_text, stderr_text))
        raise _TimeoutHandledError

    monkeypatch.setattr(
        _line_stream, "_wait_for_exit_code_within_timeout", wait_for_exit
    )
    monkeypatch.setattr(_line_stream, "_reconcile_run_tasks", reconcile)
    monkeypatch.setattr(_line_stream, "_shielded_cleanup", shield)
    monkeypatch.setattr(_line_stream, "_handle_stream_timeout", handle_timeout)

    if isinstance(error, TimeoutError):
        with pytest.raises(_TimeoutHandledError):
            asyncio.run(
                _line_stream._wait_for_line_stream_exit(
                    typ.cast("_LineStreamRun", run),
                    execution,
                )
            )
        assert timeout_calls == [("stdout", "stderr")], (
            f"timeout translation must retain reconciled capture, got {timeout_calls!r}"
        )
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(
                _line_stream._wait_for_line_stream_exit(
                    typ.cast("_LineStreamRun", run),
                    execution,
                )
            )
        assert caught.value is error, (
            f"failed exit must preserve its original exception, got {caught.value!r}"
        )

    assert [context.capture for context in contexts] == [capture], (
        f"cleanup must use the outcome capture policy, got {contexts!r}"
    )
    assert contexts[0].pid == 123, (
        f"cleanup must retain the child PID, got {contexts!r}"
    )
    assert phases == list(expected_phases), (
        f"failed exit must emit the expected lifecycle phases, got {phases!r}"
    )
    assert order.index(LineStreamPhase.TEARDOWN_STARTED) < order.index("reconcile"), (
        f"teardown must start before reconciliation, got {order!r}"
    )
    assert order.index("reconcile") < order.index(LineStreamPhase.TEARDOWN_COMPLETED), (
        f"teardown completion must follow reconciliation, got {order!r}"
    )
