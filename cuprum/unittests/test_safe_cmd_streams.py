"""Unit tests for SafeCmd captured-stream execution cleanup.

Covers the streamed (``capture=True``) execution path: cancelling an in-flight
run and handling an unexpected stdin-writer failure must both cancel and drain
the stdout/stderr consumer tasks rather than leaving them orphaned (regressions
for #117).
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import RunOutputOptions, StdinInput
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that constructs ``SafeCmd`` instances for Python.
    """
    return build_python_builder()


def test_streamed_run_cancellation_cleans_up_task(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Cancelling an in-flight streamed (capture=True) run does not hang.

    Output capture routes execution through ``_run_subprocess_with_streams``,
    so cancelling the run must cancel and drain the stdout/stderr consumer
    tasks via ``_wait_for_exit_code``'s ``CancelledError`` branch rather than
    deadlocking on a still-pending reader (regression for #117).
    """

    async def _orchestrate() -> None:
        """Start a streaming command and cancel it once output is observed."""
        command = python_builder(
            "-c",
            "import time; [print('tick', flush=True) or time.sleep(0.02) "
            "for _ in range(600)]",
        )
        streaming = asyncio.Event()

        def on_event(ev: ExecEvent) -> None:
            """Signal once a stdout consumer has delivered a line."""
            if ev.phase == "stdout":
                streaming.set()

        with (
            scoped(ScopeConfig(allowlist=frozenset([command.program]))),
            sh.observe(on_event),
        ):
            task = asyncio.create_task(
                command.run(output=RunOutputOptions(capture=True)),
            )
            # Cancel only after a stream consumer has actually delivered a
            # line: a deterministic readiness signal rather than a fixed sleep.
            await asyncio.wait_for(streaming.wait(), timeout=2.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            assert task.cancelled(), (
                "cancellation must propagate out of the streamed run; the task "
                "must not swallow CancelledError"
            )

    asyncio.run(_orchestrate())


def test_streamed_run_reconciles_consumers_on_stdin_writer_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing stdin writer must not orphan the stream consumer tasks.

    On the streamed (capture) path, an unexpected exception raised from
    ``await stdin_task`` must still cancel and drain the stdout/stderr consumer
    tasks before it propagates, rather than leaving them pending (regression
    for #117).
    """

    class _InjectedStdinError(Exception):
        """Sentinel error injected by the fake stdin writer."""

    recorded: list[asyncio.Task[str | None]] = []

    async def _raise_stdin(
        process: asyncio.subprocess.Process,
        stdin_data: bytes,
        observation: object,
    ) -> None:
        """Close stdin, then fail the writer with an injected error."""
        _ = (stdin_data, observation)
        await asyncio.sleep(0)  # yield like the real writer's drain() await
        if process.stdin is not None:
            process.stdin.close()
        raise _InjectedStdinError

    def _spawn_blocking_consumers(
        process: object,
        execution: object,
        spawn_context: object,
    ) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
        """Return two never-completing consumer tasks and record them."""
        _ = (process, execution, spawn_context)

        async def _block() -> str | None:
            """Block until cancelled during stdin-failure cleanup."""
            await asyncio.Event().wait()

        tasks = (asyncio.create_task(_block()), asyncio.create_task(_block()))
        recorded.extend(tasks)
        return tasks

    monkeypatch.setattr("cuprum._subprocess_stdin._write_stdin", _raise_stdin)
    monkeypatch.setattr(
        "cuprum._subprocess_execution._spawn_stream_consumers",
        _spawn_blocking_consumers,
    )

    command = python_builder("-c", "pass")

    async def _drive() -> None:
        """Run the streamed command and inspect consumers within the loop."""
        with pytest.raises(_InjectedStdinError):
            await command.run(
                output=RunOutputOptions(capture=True),
                stdin=StdinInput(data=b"payload"),
            )
        # Assert inside the still-running loop, before asyncio.run tears it
        # down: loop teardown would itself cancel any orphaned tasks and mask
        # the bug, so only the reconcile path can have finished these here.
        assert len(recorded) == 2, "streamed path must spawn two stream consumers"
        assert all(task.cancelled() for task in recorded), (
            "stdout/stderr consumers must be cancelled and drained when the "
            "stdin writer fails, not left orphaned as pending tasks"
        )

    asyncio.run(_drive())
