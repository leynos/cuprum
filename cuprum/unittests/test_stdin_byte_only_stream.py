r"""A stream that produced no text must not have an encoder built for it.

``StdinStream`` documents that ``bytes`` chunks bypass encoding and arrive
verbatim. The final flush broke that promise: it built an incremental encoder
in order to ask whether one was holding anything, and for several codecs the
answer to the first ``encode`` call is a byte-order mark — including
``encode("", final=True)``, which is the shape a flush takes. A byte-only
producer under ``utf-8-sig`` therefore sent ``b"raw\xef\xbb\xbf"``, an empty
producer sent a bare mark where the child should have seen end-of-file, and an
encoding name this build does not know failed a run whose chunks never needed
a codec at all.

The fix flushes only an encoder that text processing has already initialized.
These cases pin the exemption from the child's side, and the counterpart below
pins that the exemption did not also silence the flush a text-bearing stream
owes.

The byte-only suite lives here rather than beside the other streaming cases
because it is about the *boundary* between the two chunk kinds — which one may
cause a codec to exist — and not about chunk delivery, encoding settings, or
backpressure. Each assertion reads the child's raw stdin from a file, because
the differences being tested are invisible in decoded text: a byte-order mark
decodes cleanly, and a run that failed from an unresolvable codec name looks
identical to one that succeeded once the failure is absorbed.
"""

from __future__ import annotations

import asyncio
import codecs
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, RunOutputOptions, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

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
    """Yield *values* as an async producer, suspending between each.

    Yields
    ------
    str | bytes
        Each supplied value, in the order given.
    """
    for value in values:
        await asyncio.sleep(0)
        yield value


# The child reports its raw stdin bytes into the file named by its first
# argument. Hex keeps the comparison exact and independent of the capture
# encoding, and a file rather than stdout keeps the evidence readable whether
# or not the run captures output.
_RECORD_STDIN_HEX = (
    "import pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text(sys.stdin.buffer.read().hex())"
)

# The codecs whose incremental encoder emits a byte-order mark, and they emit
# it from that encoder's *first* ``encode`` call — including
# ``encode("", final=True)``, which is exactly what a final flush makes.
# Building one is therefore enough to put the mark on the wire, with no text in
# the stream to justify it.
_BOM_ENCODINGS = ("utf-8-sig", "utf-16", "utf-32")

# The byte-only producer shapes that must reach the child with no encoder
# involved. The empty producer and the one yielding only empty chunks are kept
# apart deliberately: the first never enters the per-chunk path at all, while
# the second is pulled through it and produces nothing.
_BYTE_ONLY_PRODUCERS = (
    pytest.param((b"raw",), id="bytes"),
    pytest.param((), id="no-chunks"),
    pytest.param((b"", b""), id="empty-bytes"),
)


@pytest.mark.parametrize("encoding", _BOM_ENCODINGS)
@pytest.mark.parametrize("values", _BYTE_ONLY_PRODUCERS)
def test_a_byte_only_stream_is_never_encoded(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
    encoding: str,
    values: tuple[bytes, ...],
) -> None:
    """``bytes`` chunks reach the child untouched by any codec."""
    _, execute = execution_strategy
    record = tmp_path / "stdin.hex"
    command = python_builder("-c", _RECORD_STDIN_HEX, str(record))

    result = execute(
        command,
        {
            "stdin": StdinStream(_chunks(*values)),
            "context": ExecutionContext(encoding=encoding, errors="strict"),
        },
    )

    assert result.exit_code == 0, "a byte-only stream should exit cleanly"
    expected = b"".join(values)
    assert record.read_text(encoding="utf-8") == expected.hex(), (
        f"the child must receive exactly the bytes produced under {encoding!r}, "
        f"with no codec involved; expected {expected.hex()!r}"
    )


def test_a_byte_only_stream_needs_no_valid_codec(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A codec this build does not know cannot fail a run that never encodes.

    Building the encoder to flush it made the encoding name load-bearing for
    every stream, so a producer yielding only ``bytes`` failed with
    ``LookupError`` raised from a codec its chunks never needed.

    Capture is switched off so the unknown name reaches exactly one boundary.
    A captured stream is decoded — under the caller's encoding — on its way
    into the result, so leaving capture on would let the run succeed while a
    consumer failed to decode, with that consumer's error absorbed and only a
    teardown record to show for it. The child writes its record to a file and
    nothing to a captured stream, so capture is not what is under test here.
    """
    _, execute = execution_strategy
    record = tmp_path / "stdin.hex"
    command = python_builder("-c", _RECORD_STDIN_HEX, str(record))

    result = execute(
        command,
        {
            "stdin": StdinStream(_chunks(b"raw")),
            "context": ExecutionContext(
                encoding="cuprum-no-such-codec",
                errors="strict",
            ),
            "output": RunOutputOptions(capture=False),
        },
    )

    assert result.exit_code == 0, "an unknown codec is not a stdin failure"
    assert record.read_text(encoding="utf-8") == b"raw".hex(), (
        "the caller's bytes must arrive even though the encoding they named "
        "cannot be resolved"
    )


def test_a_later_text_chunk_still_flushes_the_encoder(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A stream that reaches text keeps its final flush, raw bytes and all.

    The counterpart to the cases above: exempting a byte-only stream from the
    flush must not exempt one that produced text, and the two are told apart by
    whether an encoder exists rather than by what the first chunk happened to
    be. Text arriving *after* raw bytes still builds one, so it still flushes.

    The mark lands where the text starts rather than at the front of the
    stream, and that is the honest expectation rather than an artifact of this
    change: cuprum writes chunks in order and never rewrites a byte it has
    already sent, so a stream mixing raw bytes ahead of text under a
    byte-order-mark codec puts the mark where the first text is encoded. A
    caller who needs the mark first must encode the raw portion themselves or
    supply it as ``str``.
    """
    _, execute = execution_strategy
    record = tmp_path / "stdin.hex"
    command = python_builder("-c", _RECORD_STDIN_HEX, str(record))

    result = execute(
        command,
        {
            "stdin": StdinStream(_chunks(b"raw", "text")),
            "context": ExecutionContext(encoding="utf-8-sig", errors="strict"),
        },
    )

    assert result.exit_code == 0, "a mixed stream should exit cleanly"
    expected = b"raw" + codecs.BOM_UTF8 + b"text"
    assert record.read_text(encoding="utf-8") == expected.hex(), (
        "the raw bytes must stay untouched and the text must still carry the "
        f"codec's mark; expected {expected.hex()!r}"
    )
