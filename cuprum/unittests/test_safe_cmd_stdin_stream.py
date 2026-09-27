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
import time
import typing as typ

import pytest

from cuprum import sh
from cuprum.sh import ExecutionContext, StdinSourceError, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

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


# A child that never reads its stdin and outlives the test's deadline. Nothing
# drains the pipe, so a producer failure cannot be mistaken for a child that
# consumed the input and exited.
_NEVER_READS = "import time; time.sleep(30)"

# A child that reads one byte and exits, so a producer failing after its first
# chunk fails against a child that is already gone.
_READS_ONE_BYTE = "import sys; sys.stdin.buffer.read(1)"


async def _raising_after(
    count: int,
    *,
    exc: BaseException | None = None,
) -> cabc.AsyncIterator[bytes]:
    """Yield *count* chunks, then raise the producer's own failure.

    Parameters
    ----------
    count : int
        How many chunks to yield before failing. Zero fails on the first pull.
    exc : BaseException | None
        The failure to raise. Defaults to a ``RuntimeError``.

    Yields
    ------
    bytes
        One filler chunk per iteration before the failure.
    """
    for index in range(count):
        await asyncio.sleep(0)
        yield f"chunk-{index}\n".encode()
    msg = "producer exploded"
    raise exc if exc is not None else RuntimeError(msg)


def test_producer_failure_beats_the_timeout(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A dead producer is reported as such, not as a slow child.

    The child never reads and outlives the deadline, so a run that merely
    awaits the exit can only learn about the producer failure after the
    deadline expires. Reporting ``TimeoutError`` there blames the child for a
    failure that was already known the moment it happened.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _NEVER_READS)
    stream = StdinStream(chunks=_raising_after(0))

    started = time.perf_counter()
    with pytest.raises(StdinSourceError) as info:
        execute(command, {"stdin": stream, "timeout": 5})
    elapsed = time.perf_counter() - started

    assert isinstance(info.value.__cause__, RuntimeError), (
        "the producer's own exception must be chained as the cause"
    )
    assert elapsed < 2.0, (
        "the failure must be raised when it happens, not after the deadline; "
        f"took {elapsed:.2f}s against a 5s timeout"
    )


def test_producer_failure_after_a_chunk_still_reaches_the_caller(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A producer failing mid-stream is an error even when the child exits first.

    This is the late half of the same contract: the child reads its byte and
    goes, so the failure arrives after the exit has already settled. The run
    must not report the child's clean exit as if the input had completed.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _READS_ONE_BYTE)
    stream = StdinStream(chunks=_raising_after(1))

    with pytest.raises(StdinSourceError) as info:
        execute(command, {"stdin": stream, "timeout": 5})

    assert isinstance(info.value.__cause__, RuntimeError), (
        "a late producer failure must keep its cause too"
    )


def test_no_producer_failure_is_invented_when_the_child_exits_early(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """The negative control: an early child-side close is not a producer failure.

    A producer that keeps yielding into a closed pipe is behaving normally for
    a child like ``head``. If the rendezvous treated the writer's early
    completion as a failure, every such run would raise — so this case asserts
    the opposite, and is what keeps the failure assertions above from being
    satisfied by an implementation that raises indiscriminately.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _READS_ONE_BYTE)

    async def endless() -> cabc.AsyncIterator[bytes]:
        """Yield far more than the child will read."""
        for index in range(10_000):
            await asyncio.sleep(0)
            yield f"{index}\n".encode()

    result = execute(command, {"stdin": StdinStream(chunks=endless()), "timeout": 10})

    assert result.exit_code == 0, "an early child-side close is a clean exit"


def test_timeout_leaves_no_stdin_writer_behind(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A timed-out run reclaims its writer rather than leaking the task.

    Only the asynchronous strategy is exercised, and it is driven by hand
    rather than through the fixture: ``asyncio.all_tasks()`` needs a running
    loop, so the pending-task census has to happen *inside* the coroutine. By
    the time ``asyncio.run`` returns, the loop it built is closed and the
    census would be empty whatever the implementation did.
    """
    command = python_builder("-c", _NEVER_READS)

    async def blocking() -> cabc.AsyncIterator[bytes]:
        """Yield one chunk, then park forever like a stalled producer."""
        yield b"first\n"
        # ASYNC110: parking is the fixture. An ``asyncio.Event`` would need a
        # setter and a lifetime, and the writer's cancellation is what ends
        # this — so there is nothing for an event to signal.
        while True:  # ruff: ignore[async-busy-wait] - deliberate stall.
            await asyncio.sleep(10)

    async def census_after_timeout() -> list[asyncio.Task[object]]:
        """Time the run out, then report what is still pending."""
        with pytest.raises(TimeoutError):
            await command.run(stdin=StdinStream(chunks=blocking()), timeout=0.2)
        # Teardown is several continuations deep (cancel, drain, reconcile), so
        # one bare ``sleep(0)`` may not be enough turns to see them settle.
        await asyncio.sleep(0.1)
        return [
            task
            for task in asyncio.all_tasks()
            if not task.done() and task is not asyncio.current_task()
        ]

    leaked = asyncio.run(census_after_timeout())
    assert not leaked, f"a timed-out run must leave no pending task; got {leaked}"


# The bounded-memory evidence. Every other test in this module checks *what*
# reaches the child; this pair checks how far ahead the writer may run while
# doing it, which no assertion about the delivered bytes can see. The child
# publishes a marker file once it has read its first byte, so the producer can
# date each pull against "the child has consumed something".
_PACE_SCRIPT = "\n".join((
    "import pathlib, sys",
    "sys.stdin.buffer.read(1)",
    "pathlib.Path(sys.argv[1]).write_text('read')",
    "sys.stdin.buffer.read()",
))

# 1 MiB in 4 KiB chunks: comfortably past any pipe buffer, so a pull-after-drain
# writer fills the pipe and stops while an eager one runs to the end.
_CHUNK = b"x" * 4096
_CHUNK_COUNT = 256

# A quarter of the payload, and roughly four times the pipe capacity (the Linux
# default is 64 KiB, so sixteen of these chunks). The gap between the two is
# what makes the bound evidence rather than a number that happens to hold: a
# writer that drains the producer first must cross it.
_READ_AHEAD_CAP = _CHUNK_COUNT // 4


class _PullRecorder:
    """A producer counting the pulls that precede the child's first read."""

    def __init__(self, consumed: cabc.Callable[[], bool]) -> None:
        """Record pulls against *consumed*, the child-side progress marker."""
        self._consumed = consumed
        self.pulls = 0
        self.pulls_before_child_read = 0

    async def chunks(self) -> cabc.AsyncIterator[bytes]:
        """Yield filler chunks, dating each pull against the marker.

        The ``sleep(0)`` is the suspension point that makes this an async
        generator to the tooling, and it mirrors a producer that awaits between
        chunks rather than returning one.

        Yields
        ------
        bytes
            One filler chunk per pull.
        """
        for _ in range(_CHUNK_COUNT):
            if not self._consumed():
                self.pulls_before_child_read += 1
            self.pulls += 1
            await asyncio.sleep(0)
            yield _CHUNK


async def _drained_first(
    producer: cabc.AsyncIterator[bytes],
) -> cabc.AsyncIterator[bytes]:
    """Collect *producer* into a list, then replay it: the eager shape.

    This is the negative control. It is what a writer that resolved its source
    up front would do to the same producer, and it must cross the bound the
    streaming writer stays under.

    Yields
    ------
    bytes
        The buffered chunks, in the order they were collected.
    """
    buffered = [chunk async for chunk in producer]
    for chunk in buffered:
        yield chunk


def _paced_run(
    execute: ExecuteFn,
    command: SafeCmd,
    wrap: cabc.Callable[[cabc.AsyncIterator[bytes]], cabc.AsyncIterator[bytes]],
    marker: Path,
) -> _PullRecorder:
    """Run the paced child against a recorded producer and return the counts.

    Returns
    -------
    _PullRecorder
        The producer, whose counters describe how far ahead the writer ran.
    """
    recorder = _PullRecorder(marker.exists)
    result = execute(
        command,
        {"stdin": StdinStream(wrap(recorder.chunks())), "timeout": 30},
    )
    assert result.exit_code == 0, "the paced child should exit cleanly"
    assert recorder.pulls == _CHUNK_COUNT, (
        "every chunk must still be delivered, whatever the writer's shape"
    )
    return recorder


def test_a_slow_reader_bounds_how_far_the_writer_runs_ahead(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """The writer pulls only as far ahead as the pipe lets it.

    This is the invariant the type exists for: a caller must be able to feed a
    child more data than they would ever hold in one buffer. The observable is
    the pull counter, because retained memory is not something a test can read
    off a finished run — the writer's progress is.

    The bound is deliberately loose. It is the pipe capacity that limits how
    far ahead a streaming writer can get, and that capacity is a property of
    the host rather than of cuprum, so the assertion is "far short of
    everything" rather than a count that would pin the pipe size.
    """
    _, execute = execution_strategy
    marker = tmp_path / "read"
    command = python_builder("-c", _PACE_SCRIPT, str(marker))

    recorder = _paced_run(
        execute, command, wrap=lambda producer: producer, marker=marker
    )

    assert recorder.pulls_before_child_read < _READ_AHEAD_CAP, (
        "a pull-after-drain writer must not drain the producer before the child "
        f"reads: {recorder.pulls_before_child_read} of {recorder.pulls} chunks "
        "were pulled with nothing consumed"
    )


def test_the_bound_discriminates_an_eager_writer(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """The negative control: draining the producer first crosses the bound.

    Without this case the bound would be consistent with an implementation that
    buffered the whole payload — a number that happens to hold proves nothing
    on its own. The assertion is one-sided rather than an exact count because
    the control races a child that must boot a Python interpreter, and the
    claim being made is about which side of the cap the eager shape lands on,
    not about how quickly it gets there.
    """
    _, execute = execution_strategy
    marker = tmp_path / "read"
    command = python_builder("-c", _PACE_SCRIPT, str(marker))

    recorder = _paced_run(execute, command, wrap=_drained_first, marker=marker)

    assert recorder.pulls_before_child_read >= _READ_AHEAD_CAP, (
        "an eager collector must break the bound the streaming writer holds, "
        f"or the assertion above is vacuous; only "
        f"{recorder.pulls_before_child_read} of {recorder.pulls} chunks were "
        "pulled with nothing consumed"
    )
