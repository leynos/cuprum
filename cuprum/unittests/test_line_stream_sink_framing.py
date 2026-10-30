"""Presentation-sink framing on the ``SafeCmd.lines()`` path.

``lines()`` is lazy, so its sink session cannot be opened when the stream is
built: a stream that is never pulled would leave a GitHub Actions group and its
stop-commands lease open, silencing every workflow command the runner processes
afterwards. It must instead open on the first pull and close on every terminal
path, including an early close and a cancellation.

These are real-child tests because the property under test is ordering between
bytes the child wrote and control lines the adapter wrote around them.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.sh import CommandResult, RunOutputOptions
from cuprum.unittests.test_sinks_end_to_end import (
    _ON_CI,
    _flags_output,
    _forced_sink,
    _python_command,
    _stop_token,
)
from tests.helpers.timeouts import started_pids, wait_for_process_death

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

_LEASE_PREFIX = "::stop-commands::"

# The child announces itself and then blocks, so an early close or a
# cancellation is the only way the run can end.
_READY_THEN_BLOCK = "import time; print('ready', flush=True); time.sleep(300)"

# Every workflow command a child could use to escape its own group or forge an
# annotation. All three must reach the log before the lease is released.
_SPOOFING_CHILD = (
    "print('inside the group'); "
    "print('::endgroup::'); "
    "print('::error title=child::spoofed')"
)


async def _drain(command: sh.SafeCmd, output: RunOutputOptions) -> list[str]:
    """Consume one streamed run to completion and return the line texts."""
    async with command.lines(output=output) as stream:
        texts = [event.text async for event in stream]
    result = stream.result
    assert isinstance(result, CommandResult), (
        f"a completed line stream must report its result; got {result!r}"
    )
    return texts


def _assert_lease_frames(
    value: str,
    command: sh.SafeCmd,
    *child_outputs: str,
) -> None:
    """Assert the group opens first, the lease wraps the child, and both close.

    Parameters
    ----------
    value : str
        The sink buffer or captured stderr after the stream finished.
    command : sh.SafeCmd
        The streamed command; its bounded run label titles the group.
    *child_outputs : str
        Child lines that must fall inside the session's framing.
    """
    assert value.count("::group::") == 1, (
        f"one line stream must open one group; got {value!r}"
    )
    assert value.count(_LEASE_PREFIX) == 1, (
        f"one line stream must open one stop-commands lease; got {value!r}"
    )
    assert value.startswith(f"::group::{_run_label(command)}\n"), (
        f"the group must open before any echoed output; got {value!r}"
    )
    release = f"::{_stop_token(value)}::\n"
    assert value.index(_LEASE_PREFIX) < value.index(release), (
        f"the lease must be released after it was opened; got {value!r}"
    )
    for line in child_outputs:
        assert line in value, f"the child's {line!r} must be echoed; got {value!r}"
        assert value.index(line) < value.index(release), (
            f"{line!r} must precede the lease release so the runner ignores it; "
            f"got {value!r}"
        )
    assert value.rindex("::endgroup::\n") > value.index(release), (
        f"the session's own endgroup must follow the lease release; got {value!r}"
    )


def test_grouped_lines_lease_wraps_live_child_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``group=True`` shields child output on the line-iteration path too.

    The runner only ignores workflow commands between the lease and its
    release, so every line the child writes must land inside that window. This
    is the regression the issue reports: the line path used to leave the sink
    unopened, letting the child's ``::endgroup::`` and forged annotation reach
    the job log as commands.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(_SPOOFING_CHILD)

    with scoped(ScopeConfig(allowlist=allowlist)):
        texts = asyncio.run(_drain(command, _flags_output(group=True)))

    assert texts == [
        "inside the group",
        "::endgroup::",
        "::error title=child::spoofed",
    ], f"line events must keep their content and order under framing; got {texts!r}"
    _assert_lease_frames(
        capsys.readouterr().err,
        command,
        "inside the group",
        "::endgroup::",
        "::error title=child::spoofed",
    )


def test_stream_built_but_not_pulled_frames_nothing() -> None:
    """Closing an unpulled stream must not emit a group or a lease."""
    command, allowlist = _python_command("print('never started')")
    sink, buffer = _forced_sink()

    async def abandon() -> None:
        """Build a stream and close it without requesting an event."""
        stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
        await stream.aclose()

    with scoped(ScopeConfig(allowlist=allowlist)):
        asyncio.run(abandon())

    framed = buffer.getvalue()
    assert not framed, f"a stream that never ran must frame nothing; got {framed!r}"


def test_early_close_releases_lease_and_terminates_child() -> None:
    """An explicit close releases the lease and reaps the still-running child."""
    command, allowlist = _python_command(_READY_THEN_BLOCK)
    sink, buffer = _forced_sink()
    events: list[ExecEvent] = []

    async def close_after_first_line() -> None:
        """Stop consuming after one line, then close the stream."""
        with (
            scoped(ScopeConfig(allowlist=allowlist)),
            sh.observe(events.append),
        ):
            stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
            try:
                first = await anext(stream)
                assert first.text == "ready", (
                    f"the child must start before the close; got {first!r}"
                )
            finally:
                await stream.aclose()

    asyncio.run(close_after_first_line())

    (pid,) = started_pids(events)
    wait_for_process_death(pid, seconds=5.0, context="an early-closed line stream")
    _assert_lease_frames(buffer.getvalue(), command, "ready")


def test_cancelled_consumer_releases_lease_and_terminates_child() -> None:
    """Cancelling an awaiting consumer tears down child and framing alike."""
    command, allowlist = _python_command(_READY_THEN_BLOCK)
    sink, buffer = _forced_sink()
    events: list[ExecEvent] = []

    async def cancel_while_waiting() -> None:
        """Park a second read on the live child, then cancel it."""
        with (
            scoped(ScopeConfig(allowlist=allowlist)),
            sh.observe(events.append),
        ):
            stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
            first = await anext(stream)
            assert first.text == "ready", (
                f"the child must start before the cancellation; got {first!r}"
            )
            waiter = asyncio.ensure_future(anext(stream))
            # One turn lets the waiter reach the queue read; the child emits
            # nothing further, so it stays parked there.
            await asyncio.sleep(0)
            assert not waiter.done(), (
                "the waiter must be parked on the live child before it is cancelled"
            )
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await stream.aclose()

    asyncio.run(cancel_while_waiting())

    (pid,) = started_pids(events)
    wait_for_process_death(pid, seconds=5.0, context="a cancelled line stream")
    _assert_lease_frames(buffer.getvalue(), command, "ready")
