"""Integration tests for SafeCmd stdin handling.

Covers stdin injection (text and bytes feeding, configured encoding,
capture-disabled and early-close behaviour, and forbidden-command/encoding
ordering) alongside the stdin writer lifecycle regressions (timeout escalation,
blocked-``drain()`` cleanup, and cancellation). Feeding and timeout scenarios
cover both the ``run()`` and ``run_sync()`` strategies; cancellation cleanup is
exercised only through ``run()`` (async), since cancelling a synchronous call is
not meaningful.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import time
import typing as typ

import pytest

from cuprum import ECHO, ForbiddenProgramError, ScopeConfig, TimeoutExpired, scoped
from cuprum._subprocess_stdin import _write_stdin as _real_write_stdin
from cuprum.sh import (
    CommandResult,
    ExecutionContext,
    RunOutputOptions,
    StdinInput,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    from cuprum._pipeline_internals import _StageObservation
    from cuprum.sh import SafeCmd


class _RunKwargs(typ.TypedDict, total=False):
    """Keyword arguments accepted by ``SafeCmd.run``/``SafeCmd.run_sync``."""

    output: RunOutputOptions | None
    timeout: float | None
    context: ExecutionContext | None
    stdin: StdinInput | None


type ExecuteFn = cabc.Callable[[SafeCmd, _RunKwargs], CommandResult]


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parametrised execution strategies for run() and run_sync()."""
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


@pytest.mark.parametrize(
    ("script", "stdin", "expected_stdout"),
    [
        pytest.param(
            "import sys; print(sys.stdin.read(), end='')",
            StdinInput(text="hello stdin\n"),
            "hello stdin\n",
            id="text",
        ),
        pytest.param(
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            StdinInput(data=b"\x00raw\xff\n"),
            "\x00raw\ufffd\n",
            id="raw-bytes",
        ),
    ],
)
def test_input_feeds_stdin(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    script: str,
    stdin: StdinInput,
    expected_stdout: str,
) -> None:
    """Text and raw-byte stdin reach the child process.

    Exercised under both the ``run()`` and ``run_sync()`` execution strategies.
    """
    _, execute = execution_strategy
    command = python_builder("-c", script)

    result = execute(command, {"stdin": stdin})

    assert result.exit_code == 0
    assert result.stdout == expected_stdout
    assert result.stderr == ""


def test_input_text_uses_configured_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Text stdin is encoded using the execution context settings."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )

    result = execute(
        command,
        {
            "stdin": StdinInput(text="\u2013"),
            "context": ExecutionContext(encoding="cp1252", errors="strict"),
        },
    )

    assert result.exit_code == 0
    assert result.stdout == "\u2013"
    assert result.stderr == ""


def test_input_text_and_input_bytes_conflict(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Supplying both stdin forms raises a validation error."""
    _ = python_builder, execution_strategy
    with pytest.raises(
        ValueError,
        match=r"text and data cannot both be provided",
    ):
        StdinInput(text="hello", data=b"hello")


def test_input_text_works_when_capture_is_disabled(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Stdin injection still works when stdout/stderr are not captured."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        "import sys; sys.exit(0 if sys.stdin.read() == 'uncaptured' else 9)",
    )

    result = execute(
        command,
        {
            "output": RunOutputOptions(capture=False),
            "stdin": StdinInput(text="uncaptured"),
        },
    )

    assert result.exit_code == 0
    assert result.stdout is None
    assert result.stderr is None


def test_nonzero_exit_code_is_captured_with_input_text(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Non-zero exits still include captured streams when stdin is supplied."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        ("import sys; data = sys.stdin.read(); print(data, end=''); sys.exit(7)"),
    )

    result = execute(command, {"stdin": StdinInput(text="failure input")})

    assert result.exit_code == 7
    assert result.ok is False
    assert result.stdout == "failure input"
    assert result.stderr == ""


def test_process_closing_stdin_early_is_handled(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A process that ignores stdin does not fail the command run."""
    _, execute = execution_strategy
    command = python_builder("-c", "print('done')")

    result = execute(command, {"stdin": StdinInput(text="ignored stdin")})

    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert result.stderr == ""


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_input_text_encoding_failure_raises(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: str,
) -> None:
    """UnicodeEncodeError propagates when text cannot be encoded."""
    command = python_builder("-c", "import sys; sys.stdin.read()")
    ctx = ExecutionContext(encoding="ascii", errors="strict")
    execute = _execute_async if execution_strategy == "async" else _execute_sync
    with pytest.raises(UnicodeEncodeError):
        execute(
            command,
            {"stdin": StdinInput(text="\u00e9 non-ASCII"), "context": ctx},
        )


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_forbidden_command_rejects_before_stdin_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: str,
) -> None:
    """Forbidden commands fail before stdin encoding is attempted."""
    command = python_builder("-c", "import sys; sys.stdin.read()")
    ctx = ExecutionContext(encoding="ascii", errors="strict")
    execute = _execute_async if execution_strategy == "async" else _execute_sync

    with (
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
        pytest.raises(ForbiddenProgramError),
    ):
        execute(
            command,
            {"stdin": StdinInput(text="\u00e9 non-ASCII"), "context": ctx},
        )


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_stdin_input_with_timeout_escalation(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: typ.Literal["async", "sync"],
) -> None:
    """Stdin writer task is cleaned up when the command times out."""
    command = python_builder(
        "-c",
        "import sys, time; sys.stdin.read(); time.sleep(10)",
    )
    execute = _execute_async if execution_strategy == "async" else _execute_sync
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(text="x"),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=False),
            },
        )


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_direct_timeout_with_blocked_stdin_writer_does_not_hang(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: typ.Literal["async", "sync"],
) -> None:
    """Direct-mode timeout cancels a stdin writer wedged on an unread pipe.

    The child never reads its stdin, so a payload larger than the OS pipe
    buffer stalls the writer's ``drain()``. On timeout the runner must cancel
    that writer rather than wait on it, raising ``TimeoutExpired`` promptly
    instead of blocking on the stalled drain (regression for #117).
    """
    command = python_builder("-c", "import time; time.sleep(3600)")
    execute = _execute_async if execution_strategy == "async" else _execute_sync
    payload = b"x" * (1024 * 1024)  # 1 MiB dwarfs the ~64 KiB pipe buffer
    started = time.perf_counter()
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(data=payload),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=False),
            },
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, (
        "direct-mode timeout with a blocked stdin writer must not hang; "
        f"took {elapsed:.2f}s"
    )


def test_stdin_input_cancellation_cleans_up_task(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a run with a blocked stdin writer propagates cancellation.

    The child never reads its stdin and the payload exceeds the OS pipe
    buffer, so the writer wedges in ``drain()``. Cancelling the run must cancel
    that blocked writer and let ``CancelledError`` propagate (rather than being
    swallowed), without hanging (regression for #117).
    """

    async def _orchestrate() -> None:
        """Start a blocked-writer command and cancel it once it is wedged."""
        command = python_builder("-c", "import time; time.sleep(30)")
        payload = b"x" * (1024 * 1024)  # 1 MiB wedges the writer's drain()
        writer_wedged = asyncio.Event()

        async def _tracked_write_stdin(
            process: asyncio.subprocess.Process,
            stdin_data: bytes,
            observation: _StageObservation,
        ) -> None:
            """Signal the writer is running, then write stdin as usual.

            The event is set synchronously before delegating; the real writer
            then buffers the oversized payload and suspends on ``drain()``
            before control returns to the awaiting orchestrator, so the writer
            is genuinely wedged when it is cancelled.
            """
            writer_wedged.set()
            await _real_write_stdin(process, stdin_data, observation)

        monkeypatch.setattr(
            "cuprum._subprocess_stdin._write_stdin", _tracked_write_stdin
        )

        task = asyncio.create_task(
            command.run(
                output=RunOutputOptions(capture=False),
                stdin=StdinInput(data=payload),
            )
        )
        # Cancel only once the writer has begun (and, given the oversized
        # payload, wedged in drain): a deterministic readiness signal rather
        # than a fixed sleep.
        await asyncio.wait_for(writer_wedged.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        assert task.cancelled(), (
            "cancellation must propagate out of run(); the task must not "
            "swallow CancelledError"
        )

    asyncio.run(_orchestrate())
