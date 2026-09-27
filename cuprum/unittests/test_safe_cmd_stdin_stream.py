"""Integration tests for streaming stdin via ``StdinStream``.

``StdinInput`` feeds one complete payload and is covered in
``test_safe_cmd_stdin.py``. This module covers the streaming source: chunk
delivery, the pull-after-drain bound, incremental encoding, and the codec
settings a ``str`` chunk is written with.

The encoding cases exist as regression pins. The streaming writer once read
``encoding`` and ``errors`` off the process object, whose absence made two
``getattr`` fallbacks win every time — so a caller asking for ``cp1252``
silently got UTF-8, and ``errors="strict"`` silently degraded to ``replace``.
Both are invisible in the child's exit code, which is why they are asserted
against the child's raw stdin bytes rather than against a decoded string.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import sh
from cuprum.sh import ExecutionContext, StdinSourceError, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


_ECHO_STDIN_BYTES = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


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
        The injected fixture request whose ``param`` selects the asynchronous
        or synchronous execution strategy returned here.

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


async def _chunks(*values: str | bytes) -> cabc.AsyncIterator[str | bytes]:
    """Yield *values* as an async producer.

    The ``sleep(0)`` suspension point is what makes this an async generator in
    the sense the tooling recognizes, and it mirrors how a real producer
    behaves: it suspends between chunks rather than being a coroutine that
    returns one. Without it, ``unused-async`` flags the declaration.

    Yields
    ------
    str | bytes
        Each supplied value, in the order given.
    """
    for value in values:
        await asyncio.sleep(0)
        yield value


def _stream_command(python_builder: cabc.Callable[..., SafeCmd]) -> SafeCmd:
    """Build a child that echoes its raw stdin bytes back to stdout."""
    return python_builder("-c", _ECHO_STDIN_BYTES)


def test_stream_chunks_reach_child_in_order(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Every chunk the producer yields reaches the child, in order."""
    _, execute = execution_strategy
    command = _stream_command(python_builder)

    result = execute(
        command,
        {"stdin": StdinStream(_chunks("alpha", "-", "beta", "-", "gamma"))},
    )

    assert result.exit_code == 0, "a streamed producer should exit cleanly"
    assert result.stdout == "alpha-beta-gamma", (
        "every chunk should reach the child, in the order produced"
    )
    assert result.stderr == "", "streaming stdin should not emit stderr"


def test_stream_bytes_chunks_are_written_verbatim(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """``bytes`` chunks bypass the encoder entirely."""
    _, execute = execution_strategy
    command = _stream_command(python_builder)

    result = execute(command, {"stdin": StdinStream(_chunks(b"\x00\xffraw"))})

    assert result.exit_code == 0, "raw-byte streaming should exit cleanly"
    # Capture decodes the child's stdout as text, so the non-UTF-8 byte the
    # child echoed back surfaces as a replacement character. That byte is the
    # evidence: had the encoder touched this chunk, it would not be there.
    assert result.stdout == "\x00�raw", (
        "bytes chunks should reach the child unchanged, without encoding"
    )


def test_stream_text_chunks_use_configured_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """``str`` chunks are encoded with the context's encoding.

    This is the regression pin for the ignored-context defect: reading the
    codec off the process always fell back to UTF-8, so a ``cp1252`` caller
    received UTF-8 bytes and only noticed if it inspected them.
    """
    _, execute = execution_strategy
    command = _stream_command(python_builder)
    en_dash = "\u2013"  # EN DASH: absent from cp1252, encoded 0x96 by it

    result = execute(
        command,
        {
            "stdin": StdinStream(_chunks(en_dash)),
            "context": ExecutionContext(encoding="cp1252", errors="strict"),
        },
    )

    assert result.exit_code == 0, "cp1252-encoded streaming should exit cleanly"
    assert result.stdout == en_dash.encode("cp1252").decode("cp1252"), (
        "the child should receive exactly the cp1252 bytes, not UTF-8"
    )
    assert result.stdout.encode("cp1252") == b"\x96", (
        "an en dash must arrive as the single cp1252 byte 0x96; UTF-8 would be "
        "three bytes and this assertion is what distinguishes them"
    )


def test_stream_survives_early_child_close(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A child that closes stdin early is normal, not a source failure.

    The producer yields far more than the child will ever read, so the writer
    is still pulling when the child closes its stdin and the pipe write fails.
    That condition is recorded as an ``early_close`` observation and the run
    continues to the child's exit code; it must not surface as a
    ``StdinSourceError``.

    Exercised only through ``run()``: the assertion needs the event hook, and
    the strategy fixture's ``_RunKwargs`` shape carries no way to register one.
    """
    events: list[ExecEvent] = []
    # 64 chunks of 4 KiB is 256 KiB, comfortably past any OS pipe buffer, so
    # the writer cannot have finished before the child exits.
    chunk = f"{'x' * 4096}\n"
    command = python_builder("-c", "import sys; sys.stdin.readline(); print('done')")

    with sh.observe(events.append):
        result = asyncio.run(
            command.run(
                stdin=StdinStream(_chunks(*(chunk for _ in range(64)))),
            )
        )

    assert result.exit_code == 0, "an early child-side close should not fail the run"
    assert result.stdout == "done\n", "the child's own output should survive"
    early_close = [
        ev
        for ev in events
        if ev.phase == "stdin_error" and ev.operation == "early_close"
    ]
    assert early_close, (
        "the broken pipe should be classified as an early close; without this "
        "the run could pass by draining every chunk the child never read"
    )


def test_stream_text_chunks_honour_strict_errors(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """An unencodable ``str`` chunk fails the run under ``errors="strict"``.

    The payload form does not raise here — ``StdinInput.resolve`` encodes
    before the spawn, so the caller sees a bare ``UnicodeEncodeError``. A
    producer cannot be pulled that early without defeating the bound, so the
    failure surfaces from the writer instead, wrapped in the documented
    ``StdinSourceError`` with the encoder's error chained. What matters is
    that it *fails*: before the fix the setting was ignored and the character
    was silently replaced, so the child saw a plausible substitute and the
    run exited 0.
    """
    _, execute = execution_strategy
    command = _stream_command(python_builder)

    with pytest.raises(StdinSourceError, match="stdin producer failed") as info:
        execute(
            command,
            {
                "stdin": StdinStream(_chunks("\u2013")),
                "context": ExecutionContext(encoding="ascii", errors="strict"),
            },
        )

    assert isinstance(info.value.__cause__, UnicodeEncodeError), (
        "the encoder failure should be chained as the cause"
    )
