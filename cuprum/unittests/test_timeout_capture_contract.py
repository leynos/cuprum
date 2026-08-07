"""Tests for the capture contract a timed-out single command must honour.

A capturing run reports its streams as strings, and ``TimeoutExpired`` is no
exception: it carries whatever was captured before the deadline fired, as text.
Honouring that cannot depend on scheduling luck. Once the process is dead the
readers are a turn away from EOF, and on interpreters where the exit is
observed before those EOF events are processed a drain that cancels
immediately loses the capture entirely. These tests pin both halves of the
answer — the bounded window that lets an imminent EOF land, and the empty
string a reader with nothing to show still reports — and the obligation that
window creates: a run cancelled while waiting it out must still leave no reader
running behind it.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum import Program, TimeoutExpired, sh
from cuprum._streams import _drain, _StreamConfig
from cuprum._subprocess_drain import (
    _CAPTURE_EOF_GRACE_S,
    _drain_stream_consumers,
)
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import child_argv, python_interpreter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_PARTIAL_TEXT = "partial output"


def _capture_config(*, capture: bool) -> _StreamConfig:
    """Build a stream config that captures to memory without echoing."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="strict",
    )


def _reader_with_partial_output(config: _StreamConfig) -> asyncio.StreamReader:
    """Build a reader holding partial output whose EOF never arrives."""
    reader = asyncio.StreamReader()
    reader.feed_data(_PARTIAL_TEXT.encode(config.encoding))
    return reader


async def _never_reaches_eof() -> str | None:
    """Block as a reader does on a pipe whose EOF never arrives."""
    await asyncio.Event().wait()
    return None


async def _reaches_eof_late(text: str, turns: int) -> str | None:
    """Return ``text`` after ``turns`` scheduling turns, as a late EOF would."""
    for _ in range(turns):
        await asyncio.sleep(0)
    return text


def test_capturing_drain_reports_empty_text_for_a_reader_with_no_capture() -> None:
    """A capturing drain reports the empty string for a reader that never ran.

    The reader here never reaches EOF, so the drain cancels it and it yields no
    text at all. The run still promised captured output, so the contract is met
    with the empty string rather than broken with ``None``.
    """

    async def run_case() -> None:
        """Drain two permanently wedged readers under a capturing run."""
        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_never_reaches_eof()),
        )

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            capture=True,
        )

        assert stdout_text == "", (
            f"a capturing drain must report stdout as text, got {stdout_text!r}"
        )
        assert stderr_text == "", (
            f"a capturing drain must report stderr as text, got {stderr_text!r}"
        )

    asyncio.run(run_case())


def test_capturing_drain_waits_for_an_imminent_eof() -> None:
    """A capturing drain lets a reader a few turns from EOF deliver its capture.

    This is the shape of the loss seen when a process exit is observed before
    the pipe's EOF events are processed: the readers are still parked, but only
    briefly. Cancelling them straight away would discard output the run did
    capture.
    """

    async def run_case() -> None:
        """Drain readers that settle a few turns after the process died."""
        consumers = (
            asyncio.create_task(_reaches_eof_late("out", turns=3)),
            asyncio.create_task(_reaches_eof_late("err", turns=3)),
        )

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            capture=True,
        )

        assert stdout_text == "out", (
            f"a reader that reached EOF must keep its capture, got {stdout_text!r}"
        )
        assert stderr_text == "err", (
            f"a reader that reached EOF must keep its capture, got {stderr_text!r}"
        )

    asyncio.run(run_case())


def test_capturing_drain_settles_its_readers_when_cancelled_mid_grace() -> None:
    """Cancelling during the EOF grace window still settles both readers.

    The grace window is the one place a timed-out capturing run suspends while
    it still owns two reader tasks. ``asyncio.wait`` does not cancel what it
    waits on, so a caller cancelling here would strand both readers if the
    drain simply let the cancellation through. The drain must reconcile them
    first and only then let the cancellation continue on its way.
    """

    async def run_case() -> None:
        """Cancel a capturing drain while it waits out the grace window."""
        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_never_reaches_eof()),
        )
        drain = asyncio.create_task(
            _drain_stream_consumers(consumers, capture=True),
        )
        # Let the drain reach the grace-period wait, well inside its window,
        # so the cancellation lands where the readers are still pending.
        await asyncio.sleep(_CAPTURE_EOF_GRACE_S / 5)
        assert not drain.done(), "the drain must still be inside its grace window"
        assert not any(task.done() for task in consumers), (
            "the readers must still be pending when the cancellation lands"
        )

        drain.cancel()

        with pytest.raises(asyncio.CancelledError):
            await drain
        assert all(task.done() for task in consumers), (
            "a cancelled drain must leave no reader running behind it"
        )

    asyncio.run(run_case())


def test_non_capturing_drain_leaves_a_wedged_reader_unset() -> None:
    """A non-capturing drain still reports ``None`` for a reader with no text."""

    async def run_case() -> None:
        """Drain wedged readers for a run that captured nothing."""
        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_never_reaches_eof()),
        )

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            capture=False,
        )

        assert stdout_text is None, (
            f"a non-capturing drain must leave stdout unset, got {stdout_text!r}"
        )
        assert stderr_text is None, (
            f"a non-capturing drain must leave stderr unset, got {stderr_text!r}"
        )

    asyncio.run(run_case())


def test_a_cancelled_capturing_read_keeps_what_it_already_buffered() -> None:
    """A capturing reader cancelled mid-read reports the bytes it had.

    Output written before the deadline is output the run captured. Teardown
    cancels the reader while it waits on an EOF a wedged pipe may never
    deliver, and discarding the buffer at that point loses text the process
    really did produce.
    """

    async def run_case() -> None:
        """Cancel a capturing drain parked on a pipe that never ends."""
        config = _capture_config(capture=True)
        reader = _reader_with_partial_output(config)
        drain = asyncio.create_task(_drain(reader, config))
        # Let the drain consume the buffered bytes and park on the next read.
        for _ in range(4):
            await asyncio.sleep(0)
        assert not drain.done(), "the drain must still be waiting for EOF"

        drain.cancel()

        captured = await drain
        assert captured == _PARTIAL_TEXT, (
            f"a cancelled capturing read must keep its buffer, got {captured!r}"
        )

    asyncio.run(run_case())


def test_a_cancelled_non_capturing_read_still_propagates_the_cancellation() -> None:
    """A non-capturing reader cancelled mid-read raises as it always did.

    Only a capturing run has text worth salvaging, so the exemption stops
    there: elsewhere a cancellation must stay a cancellation.
    """

    async def run_case() -> None:
        """Cancel a non-capturing drain parked on a pipe that never ends."""
        config = _capture_config(capture=False)
        reader = _reader_with_partial_output(config)
        drain = asyncio.create_task(_drain(reader, config))
        for _ in range(4):
            await asyncio.sleep(0)
        assert not drain.done(), "the drain must still be waiting for EOF"

        drain.cancel()

        with pytest.raises(asyncio.CancelledError):
            await drain

    asyncio.run(run_case())


@pytest.fixture
def readers_that_never_reach_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace the stream consumers with readers that never observe EOF.

    Interpreters differ in whether a dead process's pipe EOF is visible by the
    time the drain runs. Withholding EOF outright reproduces the worst case on
    every interpreter, so the public contract can be asserted without depending
    on the host version.
    """

    async def consume_forever(
        _stream: asyncio.StreamReader | None,
        _config: _StreamConfig,
        *,
        on_line: cabc.Callable[[str], None] | None = None,
    ) -> str | None:
        """Stand in for a reader that is cancelled before EOF ever arrives."""
        del on_line  # Required by the _consume_stream callback interface.
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr(
        "cuprum._subprocess_execution._consume_stream",
        consume_forever,
    )


@pytest.mark.usefixtures("readers_that_never_reach_eof")
def test_timeout_reports_capture_as_text_when_no_reader_reached_eof(
    tmp_path: Path,
) -> None:
    """A capturing run's timeout reports text even when no reader saw EOF.

    Drives the public boundary rather than the drain helper, so the wiring that
    tells the drain a run is capturing is covered alongside the contract.
    """
    command = sh.make(Program(python_interpreter()), catalogue=python_catalogue()[0])(
        *child_argv(tmp_path / "ready")
    )

    with pytest.raises(TimeoutExpired) as expired:
        command.run_sync(timeout=0, output=RunOutputOptions(capture=True))

    detail = f"output={expired.value.output!r} stderr={expired.value.stderr!r}"
    assert expired.value.output == "", (
        f"a capturing run must report stdout as text on timeout, got {detail}"
    )
    assert expired.value.stderr == "", (
        f"a capturing run must report stderr as text on timeout, got {detail}"
    )


@pytest.fixture
def readers_with_partial_output_and_no_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace the stream consumers with real drains over never-ending pipes.

    Each stand-in runs the production :func:`cuprum._streams._drain` over a
    reader holding output but no EOF, which is what a process that wrote before
    its deadline and left a grandchild holding the pipe looks like from here.
    """

    async def consume_partial(
        _stream: asyncio.StreamReader | None,
        config: _StreamConfig,
        *,
        on_line: cabc.Callable[[str], None] | None = None,
    ) -> str | None:
        """Drain a pipe that delivered output and never reaches EOF."""
        del on_line  # Required by the _consume_stream callback interface.
        return await _drain(_reader_with_partial_output(config), config)

    monkeypatch.setattr(
        "cuprum._subprocess_execution._consume_stream",
        consume_partial,
    )


@pytest.mark.usefixtures("readers_with_partial_output_and_no_eof")
def test_timeout_reports_the_output_captured_before_the_deadline(
    tmp_path: Path,
) -> None:
    """A capturing run's timeout reports output written before EOF was due.

    Cancelling the readers is how teardown guarantees it leaves nothing behind,
    but it must not cost the run the text it had already captured.
    """
    command = sh.make(Program(python_interpreter()), catalogue=python_catalogue()[0])(
        *child_argv(tmp_path / "ready")
    )

    with pytest.raises(TimeoutExpired) as expired:
        command.run_sync(timeout=0, output=RunOutputOptions(capture=True))

    detail = f"output={expired.value.output!r} stderr={expired.value.stderr!r}"
    assert expired.value.output == _PARTIAL_TEXT, (
        f"a timed-out run must keep the stdout it captured, got {detail}"
    )
    assert expired.value.stderr == _PARTIAL_TEXT, (
        f"a timed-out run must keep the stderr it captured, got {detail}"
    )
