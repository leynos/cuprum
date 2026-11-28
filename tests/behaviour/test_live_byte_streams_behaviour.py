"""Behavioural scenarios for owned live byte streams.

A handle holds asyncio primitives bound to the loop that spawned them, so each
scenario runs its whole story inside one ``asyncio.run`` and hands the ``Then``
steps a record of what the caller observed. The assertions are about bytes,
closures, and whether the child was still live at a barrier — never about read
sizes or timing.

These scenarios complement ``cuprum/unittests/test_live_byte_streams.py``: that
module proves the same contract under an external session watchdog, while these
steps keep the published Gherkin behaviour executable in the ordinary suite.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import (
    ByteStreamReader,
    ExecutionContext,
    LineEvent,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    StdinStream,
    StreamClosure,
    sh,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import BytesCommandResult

_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="live-bdd")
_PYTHON = sh.make(Program(sys.executable), catalogue=_CATALOGUE)


@dc.dataclass(frozen=True, slots=True)
class _LiveObservation:
    """What one live-byte story observed, in the order the steps assert."""

    live: bytes = b""
    stderr: bytes = b""
    result: BytesCommandResult | None = None
    stdout_closure: StreamClosure | None = None
    stderr_closure: StreamClosure | None = None
    child_was_live: bool = False
    stderr_was_live: bool = False
    error: BaseException | None = None


type _LiveStory = cabc.Callable[[], cabc.Coroutine[typ.Any, typ.Any, _LiveObservation]]


def _ignore_line(_event: LineEvent) -> None:
    """Discard one observed line, proving only that the option is accepted."""


async def _read_all(reader: ByteStreamReader) -> bytes:
    """Consume one reader to its end and return the reconstructed bytes."""
    received = bytearray()
    async for chunk in reader:
        received.extend(chunk.data)
    return bytes(received)


async def _prefix_story() -> _LiveObservation:
    """Observe a stdout prefix, then release the stdin token through a barrier."""
    released = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Yield the token only after the prefix has been observed."""
        await released.wait()
        yield b"token\n"

    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'prefix:')\n"
        "sys.stdout.buffer.flush()\n"
        "line = sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'after:' + line)\n"
        "sys.stdout.buffer.flush()\n"
    )
    handle = await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
        context=ExecutionContext(cancel_grace=0.2),
    )
    head = bytearray()
    async for chunk in handle.stdout:
        head.extend(chunk.data)
        if b"prefix:" in head:
            break
    child_was_live = handle.result is None
    released.set()
    rest = bytearray(await _read_all(handle.stdout))
    result = await handle.wait()
    return _LiveObservation(
        live=bytes(head) + bytes(rest),
        result=result,
        stdout_closure=handle.stdout.closure,
        child_was_live=child_was_live,
    )


async def _independent_eof_story() -> _LiveObservation:
    """Close stdout in the child while stderr and the child remain live."""
    released = asyncio.Event()

    async def produce() -> cabc.AsyncIterator[bytes]:
        """Yield the stderr token only after stdout's EOF is observed."""
        await released.wait()
        yield b"go\n"

    code = (
        "import os, sys\n"
        "sys.stdout.buffer.write(b'out')\n"
        "sys.stdout.buffer.flush()\n"
        "null = os.open(os.devnull, os.O_WRONLY)\n"
        "os.dup2(null, 1)\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stderr.buffer.write(b'err')\n"
        "sys.stderr.buffer.flush()\n"
    )
    async with await _PYTHON("-c", code).start_bytes(
        stdin=StdinStream(produce()),
    ) as handle:
        out = await _read_all(handle.stdout)
        stdout_closure = handle.stdout.closure
        stderr_was_live = handle.stderr.closure is None
        child_was_live = handle.result is None
        released.set()
        err = await _read_all(handle.stderr)
        stderr_closure = handle.stderr.closure
        result = await handle.wait()
    return _LiveObservation(
        live=out,
        stderr=err,
        result=result,
        stdout_closure=stdout_closure,
        stderr_closure=stderr_closure,
        child_was_live=child_was_live,
        stderr_was_live=stderr_was_live,
    )


async def _line_observer_story() -> _LiveObservation:
    """Attempt to start a byte stream with a caller-supplied line observer."""
    options = RunOutputOptions(on_line=_ignore_line)
    try:
        await _PYTHON("-c", "pass").start_bytes(output=options)
    except BaseException as error:  # ruff: ignore[blind-except] - the scenario reports whatever refusal it receives
        return _LiveObservation(error=error)
    return _LiveObservation()


@given(
    "a live byte command that waits for a token after a prefix",
    target_fixture="live_story",
)
def given_prefix_story() -> _LiveStory:
    """Return the story that exchanges a prefix for a stdin token."""
    return _prefix_story


@given(
    "a live byte command that closes stdout and then writes stderr",
    target_fixture="live_story",
)
def given_independent_eof_story() -> cabc.Callable[
    [], cabc.Awaitable[_LiveObservation]
]:
    """Return the story that observes an independent stdout EOF."""
    return _independent_eof_story


@given(
    "a byte command configured with a line observer",
    target_fixture="live_story",
)
def given_line_observer_story() -> _LiveStory:
    """Return the story that is refused before a child exists."""
    return _line_observer_story


@when(
    "the caller observes the prefix and releases the token",
    target_fixture="observation",
)
@when("the caller consumes stdout to its end", target_fixture="observation")
@when("the caller starts it as a live byte stream", target_fixture="observation")
def when_the_story_runs(
    live_story: _LiveStory,
) -> _LiveObservation:
    """Run the scenario's whole story inside one event loop."""
    return asyncio.run(live_story())


@when("the caller consumes stderr to its end", target_fixture="observation")
def when_stderr_is_consumed(observation: _LiveObservation) -> _LiveObservation:
    """Return the story's already-complete observation."""
    return observation


@then("the live stdout bytes are the prefix followed by the token")
def then_live_bytes_are_the_exchange(observation: _LiveObservation) -> None:
    """Assert the prefix and the released token arrive in order."""
    assert observation.live == b"prefix:after:token\n", (
        f"live bytes were {observation.live!r}"
    )
    assert observation.child_was_live, "the child must be live at the barrier"


@then("the settled byte result carries the same bytes")
def then_capture_matches(observation: _LiveObservation) -> None:
    """Assert the settled result carries exactly the live bytes."""
    assert observation.result is not None, "the run must have settled"
    assert observation.result.stdout == observation.live, (
        "capture must equal the live bytes"
    )
    assert observation.result.ok, "the child must exit successfully"


@then("stdout reports EOF")
def then_stdout_reports_eof(observation: _LiveObservation) -> None:
    """Assert the closed stream reaches a clean EOF."""
    assert observation.stdout_closure is StreamClosure.EOF, (
        f"stdout ended as {observation.stdout_closure!r}"
    )


@then("stderr is still live and the child is still running")
def then_stderr_and_child_are_live(observation: _LiveObservation) -> None:
    """Assert the other stream and the process are unaffected."""
    assert observation.stderr_was_live, "stderr must still be live"
    assert observation.child_was_live, "the child must still be running"


@then("stderr reports EOF")
def then_stderr_reports_eof(observation: _LiveObservation) -> None:
    """Assert the second stream reaches its own clean end."""
    assert observation.stderr == b"err", f"stderr was {observation.stderr!r}"
    assert observation.stderr_closure is StreamClosure.EOF, (
        f"stderr ended as {observation.stderr_closure!r}"
    )


@then("a ValueError naming on_line is raised")
def then_on_line_is_refused(observation: _LiveObservation) -> None:
    """Assert the contradictory observer is refused before a child exists."""
    assert isinstance(observation.error, ValueError), (
        f"expected ValueError, got {observation.error!r}"
    )
    assert "on_line" in str(observation.error), (
        f"the refusal must name on_line, got {observation.error!s}"
    )


@scenario(
    "../features/live_byte_streams.feature",
    "A stdout prefix releases a stdin producer",
)
def test_stdout_prefix_releases_stdin() -> None:
    """Declare and gate the prefix-before-input scenario."""


@scenario(
    "../features/live_byte_streams.feature",
    "stdout reaches its own end while stderr stays live",
)
def test_stdout_eof_is_independent() -> None:
    """Declare and gate the independent-EOF scenario."""


@scenario(
    "../features/live_byte_streams.feature",
    "A line observer is refused before a child exists",
)
def test_line_observer_is_refused() -> None:
    """Declare and gate the option-refusal scenario."""
