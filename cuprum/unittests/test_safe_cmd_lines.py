"""Unit and behaviour tests for ``SafeCmd.lines()`` ordering and results."""

from __future__ import annotations

import asyncio
import io
import typing as typ
from collections import Counter

from cuprum import RunOutputOptions, ScopeConfig, before, observe, scoped
from cuprum.sh import ExecutionContext
from cuprum.unittests._safe_cmd_lines_support import (
    _assert_exec_event_order,
    _assert_line_event_order,
    _two_stream_script,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent
    from cuprum.sh import CommandResult, SafeCmd


def test_lines_preserve_per_stream_order_with_monotonic_at(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Each stream's lines arrive in order, and ``at`` never decreases."""
    command = python_builder("-c", _two_stream_script())

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate the stream, then return events and the result."""
        stream = command.lines()
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    stdout = [event.text for event in events if event.stream == "stdout"]
    stderr = [event.text for event in events if event.stream == "stderr"]
    assert stdout == ["o1", "o2", "o1"], f"stdout order broken: {stdout!r}"
    assert stderr == ["e1", "e2", "e1"], f"stderr order broken: {stderr!r}"
    stamps = [event.at for event in events]
    assert stamps == sorted(stamps), f"at must be non-decreasing: {stamps!r}"
    assert all(event.at >= 0.0 for event in events), (
        f"line timestamps must be non-negative, got {stamps!r}"
    )
    assert result is not None, "line iteration must return a command result"
    assert result.ok, f"line iteration command must succeed, got {result!r}"


def test_lines_publish_nonzero_result_with_captured_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A completed non-zero command exposes its lines and captured result."""
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
    )

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate to completion and retain the non-zero command result."""
        stream = command.lines()
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed non-zero line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    assert Counter((event.stream, event.text) for event in events) == {
        ("stdout", "out"): 1,
        ("stderr", "err"): 1,
    }, f"line iteration must retain both output streams, got {events!r}"
    assert result.exit_code == 7, f"non-zero exit code must be retained, got {result!r}"
    assert not result.ok, f"non-zero command result must not be ok, got {result!r}"
    assert result.stdout == "out\n", (
        f"stdout capture must survive non-zero completion, got {result.stdout!r}"
    )
    assert result.stderr == "err\n", (
        f"stderr capture must survive non-zero completion, got {result.stderr!r}"
    )


def test_lines_keep_capture_and_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Iterating lines does not disable capture or echo."""
    sink = io.StringIO()
    command = python_builder("-c", "print('captured line')")

    async def iterate() -> CommandResult:
        """Iterate with capture and echo on, then return the result."""
        stream = command.lines(
            output=RunOutputOptions(capture=True, echo=True),
            context=ExecutionContext(stdout_sink=typ.cast("typ.IO[str]", sink)),
        )
        async for _event in stream:
            pass
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return stream.result

    result = asyncio.run(iterate())

    assert result is not None, "line iteration must return a command result"
    assert result.ok, f"line iteration command must succeed, got {result!r}"
    assert result.stdout == "captured line\n", (
        f"capture must survive iteration, got {result.stdout!r}"
    )
    assert sink.getvalue() == "captured line\n", (
        f"echo must survive iteration, got {sink.getvalue()!r}"
    )


def test_lines_observe_without_capture_or_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Line iteration keeps both pipes open when it is their only consumer."""
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Collect line events and the completed stream result."""
        stream = command.lines(output=RunOutputOptions(capture=False, echo=False))
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    observed = {(event.stream, event.text) for event in events}
    assert observed == {("stdout", "out"), ("stderr", "err")}, (
        f"lines() must observe both streams with capture and echo off, got {observed!r}"
    )
    assert result.stdout is None, "capture=False must leave stdout unset"
    assert result.stderr is None, "capture=False must leave stderr unset"


def test_lines_defer_hooks_until_iteration(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Constructing or closing an unstarted stream runs no execution hooks."""
    command = python_builder("-c", "print('unused')")
    before_calls: list[SafeCmd] = []
    phases: list[str] = []

    def before_hook(cmd: SafeCmd) -> None:
        """Record a deferred before-hook invocation."""
        before_calls.append(cmd)

    def observe_hook(event: ExecEvent) -> None:
        """Record an observe-hook event phase."""
        phases.append(event.phase)

    with (
        scoped(ScopeConfig(allowlist=frozenset([command.program]))),
        before(before_hook),
        observe(observe_hook),
    ):
        stream = command.lines()
        assert not before_calls, "constructing lines() must not run before hooks"
        assert not phases, "constructing lines() must not emit a plan event"
        asyncio.run(stream.aclose())

    assert not before_calls, "closing an unstarted stream must not run before hooks"
    assert not phases, "closing an unstarted stream must not emit observe events"


def test_lines_started_stream_emits_lifecycle_and_output_events(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Starting iteration emits one correlated lifecycle with both outputs."""
    command = python_builder("-c", _two_stream_script())
    observed: list[ExecEvent] = []

    async def collect() -> list[LineEvent]:
        """Iterate the started stream while observation is active."""
        stream = command.lines()
        return [event async for event in stream]

    with observe(observed.append):
        lines = asyncio.run(collect())

    output_events = [event for event in observed if event.phase in {"stdout", "stderr"}]
    assert [event.phase for event in observed[:2]] == ["plan", "start"], (
        f"iteration must plan and start before output, got {observed!r}"
    )
    assert [event.phase for event in observed[-2:]] == ["exit", "settled"], (
        f"completion must follow all output, got {observed!r}"
    )
    assert observed.index(observed[-2]) > max(
        index
        for index, event in enumerate(observed)
        if event.phase in {"stdout", "stderr"}
    ), "exit must follow every output event"
    assert len({event.exec_id for event in observed}) == 1, (
        f"one line-stream run must share one exec_id, got {observed!r}"
    )
    _assert_exec_event_order(output_events, "stdout", ["o1", "o2", "o1"])
    _assert_exec_event_order(output_events, "stderr", ["e1", "e2", "e1"])
    _assert_line_event_order(lines, "stdout", ["o1", "o2", "o1"])
    _assert_line_event_order(lines, "stderr", ["e1", "e2", "e1"])


def test_lines_on_line_callback_receives_events(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Both output channels see ordered lines from one real subprocess."""
    line_events: list[LineEvent] = []
    observe_events: list[ExecEvent] = []
    command = python_builder("-c", _two_stream_script())

    with observe(observe_events.append):
        asyncio.run(
            command.run(
                output=RunOutputOptions(on_line=line_events.append),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", io.StringIO()),
                ),
            )
        )

    expected = Counter({
        ("stdout", "o1"): 2,
        ("stdout", "o2"): 1,
        ("stderr", "e1"): 2,
        ("stderr", "e2"): 1,
    })
    output_events = [
        event for event in observe_events if event.phase in {"stdout", "stderr"}
    ]
    assert Counter((event.stream, event.text) for event in line_events) == expected, (
        f"on_line must receive every decoded line, got {line_events!r}"
    )
    assert Counter((event.phase, event.line) for event in output_events) == expected, (
        f"observe must receive every output event, got {output_events!r}"
    )
    _assert_line_event_order(line_events, "stdout", ["o1", "o2", "o1"])
    _assert_line_event_order(line_events, "stderr", ["e1", "e2", "e1"])
    _assert_exec_event_order(output_events, "stdout", ["o1", "o2", "o1"])
    _assert_exec_event_order(output_events, "stderr", ["e1", "e2", "e1"])
    assert all(event.at >= 0.0 for event in line_events), (
        f"line callback timestamps must be non-negative, got {line_events!r}"
    )
    assert [event.at for event in line_events] == sorted(
        event.at for event in line_events
    ), f"line callback timestamps must be non-decreasing, got {line_events!r}"


def test_lines_behaviour_streams_tags_and_text(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Behaviour: the observed stream tags and text match the child's output."""
    command = python_builder("-c", _two_stream_script())

    async def follow() -> list[tuple[str, str]]:
        """Collect (stream, text) pairs from a full iteration."""
        stream = command.lines()
        return [(event.stream, event.text) async for event in stream]

    observed = asyncio.run(follow())

    assert ("stdout", "o1") in observed, (
        f"stdout line o1 must be observed, got {observed!r}"
    )
    assert ("stdout", "o2") in observed, (
        f"stdout line o2 must be observed, got {observed!r}"
    )
    assert ("stderr", "e1") in observed, (
        f"stderr line e1 must be observed, got {observed!r}"
    )
    assert ("stderr", "e2") in observed, (
        f"stderr line e2 must be observed, got {observed!r}"
    )
    assert len(observed) == 6, f"expected 6 line events, got {observed!r}"
