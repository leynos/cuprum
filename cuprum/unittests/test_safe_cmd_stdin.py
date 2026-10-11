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
import dataclasses as dc
import time
import typing as typ

import pytest

from cuprum import ECHO, ForbiddenProgramError, ScopeConfig, TimeoutExpired, scoped
from cuprum._subprocess_stdin import _write_stdin as _real_write_stdin
from cuprum.sh import ExecutionContext, RunOutputOptions, StdinInput, StdioTarget
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.execution import assert_capture_disabled
from tests.helpers.stream_pipes import drain_blocking_payload_size

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_internals import _StageObservation
    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Parameters
    ----------
    request : pytest.FixtureRequest
        The injected fixture request whose ``param`` selects the
        asynchronous or synchronous execution strategy returned here.

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


@dc.dataclass(frozen=True, slots=True)
class _StdinFeedCase:
    """A stdin-feeding scenario: child script, stdin payload, expected stdout."""

    script: str
    stdin: StdinInput
    expected_stdout: str


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _StdinFeedCase(
                script="import sys; print(sys.stdin.read(), end='')",
                stdin=StdinInput(text="hello stdin\n"),
                expected_stdout="hello stdin\n",
            ),
            id="text",
        ),
        pytest.param(
            _StdinFeedCase(
                script="import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                stdin=StdinInput(data=b"\x00raw\xff\n"),
                expected_stdout="\x00raw\ufffd\n",
            ),
            id="raw-bytes",
        ),
    ],
)
def test_input_feeds_stdin(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    case: _StdinFeedCase,
) -> None:
    """Text and raw-byte stdin reach the child process.

    Exercised under both the ``run()`` and ``run_sync()`` execution strategies.
    """
    _, execute = execution_strategy
    command = python_builder("-c", case.script)

    result = execute(command, {"stdin": case.stdin})

    assert result.exit_code == 0, "stdin-fed command should exit cleanly"
    assert result.stdout == case.expected_stdout, (
        "the stdin payload should reach the child and be echoed back unchanged"
    )
    assert not result.stderr, "stdin feeding should not emit stderr"


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

    assert result.exit_code == 0, "encoded stdin should be consumed and exit cleanly"
    assert result.stdout == "\u2013", (
        "stdin text should be encoded with the configured cp1252 codec"
    )
    assert not result.stderr, "encoded stdin feeding should not emit stderr"


def test_input_text_and_input_bytes_conflict() -> None:
    """Supplying both stdin forms raises a validation error."""
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

    assert_capture_disabled(result)


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

    assert result.exit_code == 7, "the child's non-zero exit code should be reported"
    assert result.ok is False, "a non-zero exit must mark the result as not ok"
    assert result.stdout == "failure input", (
        "captured stdout should include the fed stdin echoed back"
    )
    assert not result.stderr, "no stderr is expected for this command"


def test_process_closing_stdin_early_is_handled(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A process that ignores stdin does not fail the command run."""
    _, execute = execution_strategy
    command = python_builder("-c", "print('done')")

    result = execute(command, {"stdin": StdinInput(text="ignored stdin")})

    assert result.exit_code == 0, "a child that ignores stdin should still exit cleanly"
    assert result.stdout == "done\n", "the child's own output should be captured"
    assert not result.stderr, "early stdin closure should not surface as stderr"


def test_input_text_encoding_failure_raises(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """UnicodeEncodeError propagates when text cannot be encoded."""
    _, execute = execution_strategy
    command = python_builder("-c", "import sys; sys.stdin.read()")
    ctx = ExecutionContext(encoding="ascii", errors="strict")
    with pytest.raises(UnicodeEncodeError):
        execute(
            command,
            {"stdin": StdinInput(text="\u00e9 non-ASCII"), "context": ctx},
        )


def test_forbidden_command_rejects_before_stdin_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Forbidden commands fail before stdin encoding is attempted."""
    _, execute = execution_strategy
    command = python_builder("-c", "import sys; sys.stdin.read()")
    ctx = ExecutionContext(encoding="ascii", errors="strict")

    with (
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
        pytest.raises(ForbiddenProgramError),
    ):
        execute(
            command,
            {"stdin": StdinInput(text="\u00e9 non-ASCII"), "context": ctx},
        )


def test_stdin_input_with_timeout_escalation(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Stdin writer task is cleaned up when the command times out."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        "import sys, time; sys.stdin.read(); time.sleep(10)",
    )
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(text="x"),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=False),
            },
        )


@pytest.mark.parametrize(
    ("capture", "mode"),
    [
        pytest.param(False, "direct", id="direct"),
        pytest.param(True, "capture", id="capture"),
    ],
)
def test_timeout_with_blocked_stdin_writer_does_not_hang(
    capture: bool,
    mode: str,
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Timeout cancels a stdin writer wedged on an unread pipe, on both paths.

    The child never reads its stdin, so a payload larger than the OS pipe
    buffer stalls the writer's ``drain()``. On timeout the runner must cancel
    that writer rather than wait on it, raising ``TimeoutExpired`` promptly
    instead of blocking on the stalled drain. Exercising both the direct output
    path (``capture=False``) and the capture path (``capture=True``, which also
    drains the stdout/stderr consumers) keeps this a regression for #117 across
    both branches.
    """
    label, execute = execution_strategy
    command = python_builder("-c", "import time; time.sleep(3600)")
    # Probe the real pipe capacity so the writer is guaranteed to stall in
    # drain() rather than assuming a ~64 KiB buffer.
    payload = b"x" * drain_blocking_payload_size()
    started = time.perf_counter()
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(data=payload),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=capture),
            },
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, (
        f"{mode}-mode timeout with a blocked stdin writer must not hang under "
        f"{label} execution; took {elapsed:.2f}s"
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
        # Probe the real pipe capacity so the writer wedges in drain() rather
        # than assuming a ~64 KiB buffer.
        payload = b"x" * drain_blocking_payload_size()
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


_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"

# The child reports its stdin byte-for-byte into the file named by its first
# argument. Reporting to a file rather than to stdout keeps the evidence
# readable on a run whose capture is disabled, and hex keeps the comparison
# exact without a codec in the middle.
_RECORD_STDIN_HEX = (
    "import pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text(sys.stdin.buffer.read().hex())"
)


def test_an_inherited_stdin_beside_a_source_is_refused(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Two answers to "where does stdin come from" are refused, not resolved.

    ``RunOutputOptions.stdin`` and the run call's ``stdin=`` each claim to say
    what the child reads. Supplying both leaves no honest rule: whichever the
    implementation honours, the other is silently dropped. The resolver reads a
    source first and consults the target only when there is none, so an
    explicit ``StdioTarget.inherit()`` beside a payload would quietly deliver
    the payload instead of the caller's stream — the same silent loss the
    construction-time rules exist to prevent, reached by a route those rules
    cannot see, because ``RunOutputOptions`` is built before the run call and
    never learns whether a source was supplied.

    The refusal is asserted through both entry points: they share the
    preparation path, but a caller meets each separately.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _ECHO_STDIN)

    with pytest.raises(ValueError, match="also supplies a stdin source"):
        execute(
            command,
            {
                "stdin": StdinInput("payload"),
                "output": RunOutputOptions(
                    capture=False,
                    stdin=StdioTarget.inherit(),
                ),
            },
        )


def test_an_explicit_stdin_pipe_beside_a_source_is_accepted(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """The near-miss: naming the pipe a payload travels through is coherent.

    ``StdioTarget.pipe()`` and a supplied source agree — the source is written
    through the pipe the target names — so refusing this would break the
    ordinary spelling for no reason. Without this case the refusal above would
    be satisfied by rejecting every combination of target and source.

    The child reports the bytes it read into a file rather than returning them
    on stdout, because this run deliberately disables capture: the payload has
    to be observable from the child's own record, not from a stream the run
    never keeps.
    """
    _, execute = execution_strategy
    record = tmp_path / "received.hex"
    command = python_builder("-c", _RECORD_STDIN_HEX, str(record))
    payload = b"payload"

    result = execute(
        command,
        {
            "stdin": StdinInput(data=payload),
            "output": RunOutputOptions(capture=False, stdin=StdioTarget.pipe()),
        },
    )

    assert result.exit_code == 0, "a payload through an explicit pipe must run"
    # A clean exit alone would be satisfied by a pipe the writer never reached;
    # the child's own record of its stdin is what proves delivery.
    assert record.read_text(encoding="utf-8") == payload.hex(), (
        "the child must have received exactly the bytes the explicit stdin "
        f"pipe supplied; expected {payload.hex()!r}, got "
        f"{record.read_text(encoding='utf-8')!r}"
    )
