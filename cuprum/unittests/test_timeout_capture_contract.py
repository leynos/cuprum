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
from cuprum._subprocess_wait import (
    _CAPTURE_EOF_GRACE_S,
    _drain_stream_consumers,
    _DrainContext,
)
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    CHILD_STDERR,
    CHILD_STDOUT,
    child_argv,
    python_interpreter,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


async def _never_reaches_eof() -> str | None:
    """Block as a reader does on a pipe whose EOF never arrives."""
    await asyncio.Event().wait()


async def _reaches_eof_late(text: str, turns: int) -> str | None:
    """Return ``text`` after ``turns`` scheduling turns, as a late EOF would."""
    for _ in range(turns):
        await asyncio.sleep(0)
    return text


async def _wait_for_marker(marker: Path, *, seconds: float = 10.0) -> None:
    """Fail unless the child writes its readiness marker within ``seconds``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        # ASYNC240: a child process writes this file, so no asyncio primitive
        # can observe it directly.
        if marker.exists():  # noqa: ASYNC240
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"the child did not write {marker} within {seconds}s")


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

        async with asyncio.timeout(_CAPTURE_EOF_GRACE_S * 2):
            stdout_text, stderr_text = await _drain_stream_consumers(
                consumers,
                _DrainContext(capture=True),
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
    briefly.
    Cancelling them straight away would discard output the run did capture.
    """

    async def run_case() -> None:
        """Drain readers that settle a few turns after the process died."""
        consumers = (
            asyncio.create_task(_reaches_eof_late("out", turns=3)),
            asyncio.create_task(_reaches_eof_late("err", turns=3)),
        )

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            _DrainContext(capture=True),
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
        grace_started = asyncio.Event()
        grace_release = asyncio.Event()

        async def wait_at_grace(
            _consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
        ) -> None:
            """Expose the exact grace boundary without relying on elapsed time."""
            grace_started.set()
            await grace_release.wait()

        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_never_reaches_eof()),
        )
        drain = asyncio.create_task(
            _drain_stream_consumers(
                consumers,
                _DrainContext(capture=True, eof_grace_waiter=wait_at_grace),
            ),
        )
        await grace_started.wait()
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
            _DrainContext(capture=False),
        )

        assert stdout_text is None, (
            f"a non-capturing drain must leave stdout unset, got {stdout_text!r}"
        )
        assert stderr_text is None, (
            f"a non-capturing drain must leave stderr unset, got {stderr_text!r}"
        )

    asyncio.run(run_case())


def test_non_capturing_drain_discards_a_buffered_consumer_when_signalled() -> None:
    """Non-capturing cleanup cancels a buffered reader without decoding it."""

    async def run_case() -> None:
        """Set the shared discard event before consumer settlement."""
        discard_event = asyncio.Event()
        reader = asyncio.StreamReader()
        reader.feed_data(b"buffered output")
        config = _StreamConfig(
            capture_output=True,
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="strict",
            discard_on_cancel=discard_event,
        )
        consumers = (
            asyncio.create_task(_drain(reader, config)),
            asyncio.create_task(_never_reaches_eof()),
        )
        await asyncio.sleep(0)
        discard_event.set()

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            _DrainContext(capture=False, discard_on_cancel=discard_event),
        )

        assert stdout_text is None
        assert stderr_text is None
        assert all(task.cancelled() for task in consumers), (
            "non-capturing cleanup must settle both readers by cancellation"
        )

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
        return

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


def test_timeout_keeps_flushed_output_after_the_child_is_ready(tmp_path: Path) -> None:
    """A public timeout retains each stream flushed before its deadline."""

    async def run_case() -> TimeoutExpired:
        """Wait for the child readiness marker before its timeout fires."""
        marker = tmp_path / "ready"
        command = sh.make(
            Program(python_interpreter()),
            catalogue=python_catalogue()[0],
        )(*child_argv(marker))
        run = asyncio.create_task(
            command.run(timeout=1.0, output=RunOutputOptions(capture=True)),
        )

        await _wait_for_marker(marker)

        with pytest.raises(TimeoutExpired) as expired:
            await run
        return expired.value

    expired = asyncio.run(run_case())

    assert isinstance(expired.output, str)
    assert CHILD_STDOUT in expired.output
    assert isinstance(expired.stderr, str)
    assert CHILD_STDERR in expired.stderr
