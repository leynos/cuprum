"""Which failures are the stdin producer's, and which are the child's.

The streaming writer runs two operations inside one ``async for`` body: it
advances the producer, and it writes what the producer yielded to the child's
pipe. A failure from either can be an ``OSError`` in the pipe family, so the
handler's classification cannot be made from the exception type alone — a
producer raising ``BrokenPipeError`` from its own machinery is the producer
failing, while the same type raised by ``drain()`` is the child closing its end
of the pipe.

Getting this backwards is invisible in the exit code at one end and silently
loses a producer's error at the other, so both directions are pinned here. The
encoder cases cover the second boundary: building the incremental encoder is
itself fallible, and that failure has to be wrapped and cleaned up like any
other source failure.
"""

from __future__ import annotations

import asyncio
import errno
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, StdinSourceError, StdinStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


_ECHO_STDIN_BYTES = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"

# A child that never reads its stdin and outlives the test's deadline. Nothing
# drains the pipe, so the parent's write cannot fail on its own and every
# exception the writer sees came from the producer.
_NEVER_READS = "import time; time.sleep(30)"


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

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
    from tests.helpers.catalogue import python_builder as build_python_builder

    return build_python_builder()


async def _raising_with(exc: BaseException) -> cabc.AsyncIterator[bytes]:
    """Yield one chunk, then raise *exc* from the producer itself.

    Yields
    ------
    bytes
        One filler chunk, so the writer has entered its loop body before the
        producer fails.
    """
    await asyncio.sleep(0)
    yield b"chunk-0\n"
    raise exc


async def _text_chunks(*values: str) -> cabc.AsyncIterator[str]:
    """Yield *values* as an async producer of text chunks.

    Yields
    ------
    str
        Each supplied value, in the order given.
    """
    for value in values:
        await asyncio.sleep(0)
        yield value


class _UnstartableProducer:
    """An async iterable whose ``__aiter__`` raises, before yielding anything.

    ``StdinStream`` documents that ``chunks`` may be any async iterable,
    because the writer advances it with ``aiter()``. That call is itself the
    producer's code, so a producer that fails to *start* is failing exactly as
    one that fails to advance does, and the two must classify alike.
    """

    def __init__(self, exc: BaseException) -> None:
        """Raise *exc* from this producer's ``__aiter__``."""
        self._exc = exc

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Raise the recorded exception, as a failing producer would."""
        raise self._exc


# The three ways CPython spells a broken pipe. ``BrokenPipeError`` and
# ``ConnectionResetError`` are the named subclasses; the bare ``OSError`` is
# what a pipe error becomes when its errno is assigned after construction,
# which ``_is_early_close`` already accounts for on the write side.
_PIPE_ERRORS = (
    pytest.param(BrokenPipeError(32, "Broken pipe"), id="BrokenPipeError"),
    pytest.param(
        ConnectionResetError(104, "Connection reset by peer"),
        id="ConnectionResetError",
    ),
    pytest.param(OSError(errno.EPIPE, "Broken pipe"), id="OSError-EPIPE"),
)


@pytest.mark.parametrize("producer_error", _PIPE_ERRORS)
def test_a_producer_that_cannot_start_is_a_source_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    producer_error: OSError,
) -> None:
    """A pipe error from ``__aiter__`` blames the producer, not the child.

    The same classification as advancing the producer, and for the same
    reason: the child never reads, so its stdin stays open and no write of
    cuprum's can have failed. Classifying this as an early close instead
    swallows it — the run continues to the child's exit code and reports
    success for a producer that never produced anything.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _NEVER_READS)
    stream = StdinStream(_UnstartableProducer(producer_error))

    with pytest.raises(StdinSourceError) as info:
        execute(command, {"stdin": stream, "timeout": 5})

    assert info.value.__cause__ is producer_error, (
        "the producer's own exception must be chained as the cause"
    )


@pytest.mark.parametrize("producer_error", _PIPE_ERRORS)
def test_producer_pipe_error_is_a_source_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    producer_error: OSError,
) -> None:
    """A producer raising a pipe error is the producer failing, not the child.

    The child never reads, so its stdin stays open and the parent's write
    cannot fail. Every exception raised inside the writer therefore came from
    advancing the producer, and the documented contract says that becomes a
    ``StdinSourceError`` with the producer's exception chained.

    Classifying it as an early close instead swallows the failure: the run
    continues to the child's exit code and can report success for a producer
    that never finished.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _NEVER_READS)
    stream = StdinStream(_raising_with(producer_error))

    with pytest.raises(StdinSourceError) as info:
        execute(command, {"stdin": stream, "timeout": 5})

    assert info.value.__cause__ is producer_error, (
        "the producer's own exception must be chained as the cause"
    )


def test_unbuildable_encoder_is_a_source_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """An unknown codec name is a source failure, not a raw lookup error.

    ``codecs.getincrementalencoder`` raises ``LookupError`` for a name it does
    not know. Building the encoder before the guarded region let that escape
    unwrapped, so a caller got a bare ``LookupError`` from inside cuprum
    instead of the documented ``StdinSourceError``.
    """
    _, execute = execution_strategy
    command = python_builder("-c", _ECHO_STDIN_BYTES)

    with pytest.raises(StdinSourceError) as info:
        execute(
            command,
            {
                "stdin": StdinStream(_text_chunks("text")),
                "context": ExecutionContext(
                    encoding="definitely-not-a-codec", errors="strict"
                ),
            },
        )

    assert isinstance(info.value.__cause__, LookupError), (
        "the codec lookup failure must be chained as the cause"
    )


def test_unbuildable_encoder_still_closes_the_producer(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The producer is finalized even when the encoder cannot be built.

    This is the other half of the same defect. With the encoder built outside
    the ``try/finally``, ``aclose`` never ran, so a producer holding a file or
    a connection kept it open for the lifetime of the process.

    Exercised only through ``run()``: the strategy fixture's ``_RunKwargs``
    shape carries no way to pass an already-built generator for the sync
    wrapper to consume a second time.
    """
    closed: list[bool] = []

    async def producer() -> cabc.AsyncIterator[str]:
        """Yield one chunk, recording whether the consumer finalized us."""
        try:
            await asyncio.sleep(0)
            yield "text"
        finally:
            closed.append(True)

    command = python_builder("-c", _ECHO_STDIN_BYTES)

    with pytest.raises(StdinSourceError):
        asyncio.run(
            command.run(
                stdin=StdinStream(producer()),
                context=ExecutionContext(
                    encoding="definitely-not-a-codec", errors="strict"
                ),
            )
        )

    assert closed == [True], (
        "the producer's generator must be closed even on the encoder path"
    )
