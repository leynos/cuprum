"""Property-based tests for SafeCmd stdin injection.

Covers:
- StdinInput.resolve() encoding invariants across Unicode text and codecs.
- Exact byte-for-byte payload delivery for arbitrary binary payloads.
- Exact byte-for-byte delivery for arbitrary *streamed* chunk lists.
- Clean timeout termination with arbitrary stdin payloads.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from cuprum import TimeoutExpired
from cuprum.sh import ExecutionContext, RunOutputOptions, StdinInput, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder wrapping the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that constructs a SafeCmd for the running interpreter.

    """
    return build_python_builder()


# ---------------------------------------------------------------------------
# Suite 1: StdinInput.resolve encoding invariants (pure unit — no subprocess)
# ---------------------------------------------------------------------------

_ENCODINGS = ["utf-8", "utf-16", "utf-32", "latin-1", "ascii"]
_ERROR_HANDLERS = ["replace", "ignore", "backslashreplace"]


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    text=st.text(),
    encoding=st.sampled_from(_ENCODINGS),
    errors=st.sampled_from(_ERROR_HANDLERS),
)
def test_resolve_text_always_returns_bytes(
    text: str,
    encoding: str,
    errors: str,
) -> None:
    """StdinInput.resolve() always returns bytes for any text/encoding/errors combo.

    'strict' is excluded from error handlers here because arbitrary text cannot
    always be encoded strictly in every codec (e.g. non-ASCII in 'ascii').
    That is correct behaviour, not a defect, and is tested separately.
    """
    ctx = ExecutionContext(encoding=encoding, errors=errors)
    result = StdinInput(text=text).resolve(ctx)
    assert isinstance(result, bytes)
    # The result must itself be decodable with the same settings.
    result.decode(encoding, errors)


@settings(max_examples=200, deadline=None, derandomize=True)
@given(payload=st.binary(min_size=0, max_size=4096))
def test_resolve_data_is_identity(payload: bytes) -> None:
    """StdinInput(data=...).resolve() returns the payload unchanged for any ctx."""
    ctx = ExecutionContext()
    result = StdinInput(data=payload).resolve(ctx)
    assert result == payload


@settings(max_examples=200, deadline=None, derandomize=True)
@given(text=st.text(alphabet=st.characters(max_codepoint=127)))
def test_resolve_text_strict_ascii_roundtrips(text: str) -> None:
    """ASCII-safe text resolves and round-trips exactly under strict ascii."""
    ctx = ExecutionContext(encoding="ascii", errors="strict")
    result = StdinInput(text=text).resolve(ctx)
    assert isinstance(result, bytes)
    assert result.decode("ascii") == text


# ---------------------------------------------------------------------------
# Suite 2: Exact payload delivery across arbitrary binary sizes (subprocess)
# ---------------------------------------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(min_size=0, max_size=64 * 1024))
def test_stdin_payload_delivered_exactly(
    python_builder: cabc.Callable[..., SafeCmd],
    payload: bytes,
) -> None:
    """Every byte of the stdin payload arrives at the subprocess unchanged.

    The subprocess reads its entire stdin and writes the hex representation to
    stdout, allowing byte-perfect comparison without encoding concerns.
    """
    command = python_builder(
        "-c",
        (
            "import sys; "
            "sys.stdout.buffer.write(sys.stdin.buffer.read().hex().encode()); "
            "sys.stdout.buffer.flush()"
        ),
    )
    result: CommandResult = command.run_sync(
        stdin=StdinInput(data=payload),
        output=RunOutputOptions(capture=True, echo=False),
    )
    assert result.exit_code == 0
    assert result.stdout == payload.hex()


@settings(
    max_examples=20,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.binary(min_size=0, max_size=4096),
    encoding=st.sampled_from(_ENCODINGS),
)
def test_stdin_text_payload_delivered_with_configured_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    payload: bytes,
    encoding: str,
) -> None:
    """Text decoded via ctx.encoding arrives at the subprocess as its byte form.

    We round-trip: decode the payload bytes to text (ignoring errors), then
    feed that text back through StdinInput with the same encoding, and assert
    the subprocess receives the re-encoded form.
    """
    text = payload.decode(encoding, errors="replace")
    expected = text.encode(encoding, errors="replace")
    ctx = ExecutionContext(encoding=encoding, errors="replace")
    # Resolve separately to decouple stdin encoding from stdout decoding
    # (the subprocess produces ASCII hex output, not encoding-configured text).
    stdin_bytes = StdinInput(text=text).resolve(ctx)
    command = python_builder(
        "-c",
        (
            "import sys; "
            "sys.stdout.buffer.write(sys.stdin.buffer.read().hex().encode()); "
            "sys.stdout.buffer.flush()"
        ),
    )
    result: CommandResult = command.run_sync(
        stdin=StdinInput(data=stdin_bytes),
        output=RunOutputOptions(capture=True, echo=False),
    )
    assert result.exit_code == 0
    assert result.stdout == expected.hex()


# ---------------------------------------------------------------------------
# Suite 3: Clean termination on timeout with arbitrary stdin payloads
# ---------------------------------------------------------------------------


@settings(
    max_examples=20,
    deadline=10_000,  # 10 s wall-clock budget per example
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.binary(min_size=0, max_size=16 * 1024),
    timeout=st.floats(min_value=0.05, max_value=0.25),
)
def test_timeout_always_terminates_with_stdin(
    python_builder: cabc.Callable[..., SafeCmd],
    payload: bytes,
    timeout: float,
) -> None:
    """TimeoutExpired is always raised and the call never hangs.

    The subprocess reads all of its stdin then sleeps indefinitely; with a
    short timeout the runner must cancel the stdin writer task and raise
    TimeoutExpired without deadlocking, regardless of payload size.
    """
    command = python_builder(
        "-c",
        "import sys, time; sys.stdin.buffer.read(); time.sleep(3600)",
    )
    with pytest.raises(TimeoutExpired):
        command.run_sync(
            stdin=StdinInput(data=payload),
            timeout=timeout,
            output=RunOutputOptions(capture=False),
        )


# ---------------------------------------------------------------------------
# Suite 4: Exact delivery for arbitrary *streamed* chunk lists (subprocess)
# ---------------------------------------------------------------------------


def _classify_chunks(chunks: list[str | bytes]) -> str:
    """Name the class of chunk list a generated example belongs to.

    Returns
    -------
    str
        One of ``empty-list``, ``all-empty``, ``empty-chunks``, ``binary``,
        ``text``, or ``mixed``.
    """
    if not chunks:
        return "empty-list"
    if not any(chunks):
        return "all-empty"
    if any(not chunk for chunk in chunks):
        return "empty-chunks"
    kinds = {isinstance(chunk, str) for chunk in chunks}
    if len(kinds) > 1:
        return "mixed"
    return "text" if kinds.pop() else "binary"


# The child echoes its stdin as hex, so the comparison is byte-perfect and
# independent of the capture encoding. The payload suites above inline the
# same script; this one names it because the property's tail flush is the
# thing under test and the script must not vary with it.
_HEX_STDIN_CHILD = (
    "import sys; "
    "sys.stdout.buffer.write(sys.stdin.buffer.read().hex().encode()); "
    "sys.stdout.buffer.flush()"
)


async def _yield_chunks(chunks: list[str | bytes]) -> cabc.AsyncIterator[str | bytes]:
    """Yield *chunks* as an async producer, suspending between each.

    Yields
    ------
    str | bytes
        Each chunk in the order supplied.
    """
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


def _stream_chunks(chunk: st.SearchStrategy[str | bytes]) -> st.SearchStrategy:
    """Strategy for a chunk list that always reaches the empty cases.

    A bare ``st.lists(...)`` samples the empty list only sometimes, and a
    property about streaming delivery is at its most informative exactly when
    there is nothing to deliver or nothing but empty chunks: those are the
    inputs where a missing final encoder flush shows up. The forced examples
    on the test below supply both classes unconditionally, so neither depends
    on the generator's luck.

    Returns
    -------
    st.SearchStrategy
        A strategy for lists of ``str`` and ``bytes`` chunks.
    """
    return st.lists(chunk, max_size=40)


def _classify_chunks(chunks: list[str | bytes]) -> str:
    """Name the class of chunk list a generated example belongs to.

    Returns
    -------
    str
        One of ``empty-list``, ``all-empty``, ``empty-chunks``, ``binary``,
        ``text``, or ``mixed``.
    """
    if not chunks:
        return "empty-list"
    if not any(chunks):
        return "all-empty"
    if any(not chunk for chunk in chunks):
        return "empty-chunks"
    kinds = {isinstance(chunk, str) for chunk in chunks}
    if len(kinds) > 1:
        return "mixed"
    return "text" if kinds.pop() else "binary"


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(chunks=_stream_chunks(st.binary(max_size=512) | st.text(max_size=512)))
@example(chunks=[])
@example(chunks=[b""])
@example(chunks=[""])
@example(chunks=["", b"", ""])
def test_streamed_chunks_are_delivered_exactly_once(
    python_builder: cabc.Callable[..., SafeCmd],
    chunks: list[str | bytes],
) -> None:
    """The child receives the concatenation of the encoded chunks, exactly.

    This is the streaming twin of ``test_stdin_payload_delivered_exactly``
    above, and it covers what that case structurally cannot: a payload
    delivered as one ``write`` has no chunk boundaries to get wrong. Here the
    boundaries are the whole subject — a chunk dropped at a boundary, a chunk
    written twice after a partial drain, or a final encoder flush omitted when
    the last chunk is ``str`` all show up as a hex mismatch.

    The child is the same hex-echoing one the payload property uses, so the
    comparison is byte-perfect and independent of the capture encoding.

    The four forced examples are the non-vacuity lever rather than decoration.
    The empty list, a single empty chunk, and a run of empty chunks are the
    smallest inputs that separate "flushed" from "not flushed", and Hypothesis
    reports forced examples in its statistics without any extra bookkeeping:
    a run that never reached them is visible there rather than inferred from a
    passing test.
    """
    expected = b"".join(
        chunk.encode("utf-8") if isinstance(chunk, str) else chunk for chunk in chunks
    )
    command = python_builder("-c", _HEX_STDIN_CHILD)
    event(f"chunks: {_classify_chunks(chunks)}")

    result: CommandResult = asyncio.run(
        command.run(
            stdin=StdinStream(_yield_chunks(chunks)),
            output=RunOutputOptions(capture=True, echo=False),
        )
    )

    assert result.exit_code == 0, "a streamed producer should exit cleanly"
    assert result.stdout == expected.hex(), (
        "the child must receive the concatenation of every chunk, with no "
        "reordering, duplication, or truncation at a chunk boundary"
    )
