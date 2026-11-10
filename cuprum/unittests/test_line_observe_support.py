"""Shared scaffolding for the real-child observe-retention tests.

The retention bound is exercised against a real child in three modules: the
hook-speed matrix, the two-stage pipeline, and the child-write backpressure
case. Everything they share lives here — the chatty child builders, the
observe hooks, the retention probe, and the delivery assertions — so each test
module can stay focused on the behaviour it owns.

The shared delivery assertion lives here because all three modules check the
same lossless-delivery and retention-bound contract; ``*_support.py`` carries
the same per-file lint exemption for ``assert`` as ``test_*.py``.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import sh
from cuprum._observe_task_retention import _OBSERVE_TASK_CEILING
from cuprum._pipeline_types import _StageObservation
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent
    from cuprum.sh import Pipeline, SafeCmd

_LINE_COUNT = 20_000


class _ObserverFailureError(Exception):
    """Raised by a deliberately failing async observe hook."""

    def __init__(self, line: str) -> None:
        """Record the line whose observation failed."""
        super().__init__(line)
        self.line = line


def _keep_stream_piped(_event: LineEvent) -> None:
    """Consume each line so an uncaptured stream is still read."""


def _output_options(capture: bool) -> RunOutputOptions:
    """Return options that read lines, capturing them only when asked.

    With capture and echo both off, cuprum folds an unread stream to DEVNULL.
    Registering a line observer is what keeps the stream piped, so the flood
    reaches the observe hook in both modes and the two runs differ only in
    whether the lines are also retained on the result.

    Returns
    -------
    RunOutputOptions
        Options that capture only when ``capture`` is true and always keep the
        stream piped for line observation.
    """
    return RunOutputOptions(capture=capture, on_line=_keep_stream_piped)


def _chatty_command(
    line_count: int = _LINE_COUNT,
) -> tuple[SafeCmd, ProgramCatalogue]:
    """Build a command that writes ``line_count`` distinct stdout lines."""
    catalogue, program = python_catalogue()
    build = sh.make(program, catalogue=catalogue)
    script = (
        "import sys; "
        "sys.stdout.write(''.join(f'line-{i}\\n' for i in range("
        f"{line_count})))"
    )
    return build("-c", script), catalogue


def _chatty_pipeline(line_count: int) -> tuple[Pipeline, ProgramCatalogue]:
    """Build a two-stage pipeline whose stages both flood stderr.

    The second stage writes its own lines before reading stdin, so both
    stderr floods start as soon as the two children spawn. That overlap is
    what makes the shared retention list contend: each stage dispatches line
    events while the other stage's tasks are still unfinished.

    Returns
    -------
    tuple[Pipeline, ProgramCatalogue]
        The two-stage pipeline and the catalogue that authorizes its terms.
    """
    catalogue, program = python_catalogue()
    build = sh.make(program, catalogue=catalogue)
    first = build(
        "-c",
        "import sys; "
        "sys.stderr.write(''.join(f'a-{i}\\n' for i in range("
        f"{line_count}))); "
        "sys.stdout.write('done\\n')",
    )
    second = build(
        "-c",
        "import sys; "
        "sys.stderr.write(''.join(f'b-{i}\\n' for i in range("
        f"{line_count}))); "
        "sys.stdin.read()",
    )
    return first | second, catalogue


def _expected_lines(line_count: int) -> list[str]:
    """Return the exact line sequence the chatty child writes."""
    return [f"line-{index}" for index in range(line_count)]


class _RetentionProbe:
    """Record the pending-task high-water mark from inside event emission."""

    def __init__(self) -> None:
        """Create the saturation signal the blocked case waits on."""
        self.saturated = asyncio.Event()
        self.high_water = 0
        self.emissions = 0
        self.list_ids: set[int] = set()

    def record(self, observation: _StageObservation) -> None:
        """Record one post-emission list size and the list it was read from."""
        size = len(observation.pending_tasks)
        self.emissions += 1
        self.list_ids.add(id(observation.pending_tasks))
        self.high_water = max(self.high_water, size)
        if size >= _OBSERVE_TASK_CEILING:
            self.saturated.set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the observation dispatcher to record after each emission."""
        original = _StageObservation._emit_event

        def spy(observation: _StageObservation, event: ExecEvent) -> None:
            """Record the list size after the real dispatcher runs."""
            original(observation, event)
            self.record(observation)

        monkeypatch.setattr(_StageObservation, "_emit_event", spy)


def _is_line(event: ExecEvent) -> bool:
    """Return whether ``event`` carries an output line."""
    return event.phase in {"stdout", "stderr"}


class _FastHook:
    """Async hook that yields once for every line."""

    def __init__(self) -> None:
        """Start with no lines recorded."""
        self.seen: list[str] = []

    def __call__(self, event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Record the line and schedule one cheap settlement."""
        if not _is_line(event):
            return None
        self.seen.append(typ.cast("str", event.line))
        return self._settle()

    async def _settle(self) -> None:
        """Settle after one cooperative yield."""
        await asyncio.sleep(0)


class _SlowHook:
    """Async hook that takes measurable time for every line."""

    def __init__(self) -> None:
        """Start with no lines recorded."""
        self.seen: list[str] = []

    def __call__(self, event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Record the line and schedule a slow settlement."""
        if not _is_line(event):
            return None
        self.seen.append(typ.cast("str", event.line))
        return self._settle()

    async def _settle(self) -> None:
        """Settle after a measurable delay."""
        await asyncio.sleep(0.001)


class _GatedHook:
    """Async hook whose tasks stay unfinished until the gate opens."""

    def __init__(self) -> None:
        """Create the gate and the recorded-lines list."""
        self.gate = asyncio.Event()
        self.seen: list[str] = []

    def __call__(self, event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Record the line and defer its settlement to the gate."""
        if not _is_line(event):
            return None
        self.seen.append(typ.cast("str", event.line))
        return self._settle()

    async def _settle(self) -> None:
        """Complete when the test releases the gate."""
        await self.gate.wait()


class _FailingHook:
    """Async hook that fails on every line."""

    def __init__(self) -> None:
        """Start with no lines attempted."""
        self.attempted = 0

    def __call__(self, event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Schedule a failing settlement for the line."""
        if not _is_line(event):
            return None
        self.attempted += 1
        return self._fail(typ.cast("str", event.line))

    async def _fail(self, line: str) -> None:
        """Fail after a yield so the task is scheduled before it raises."""
        await asyncio.sleep(0)
        raise _ObserverFailureError(line)


async def _run_observed(
    catalogue: ProgramCatalogue,
    hook: object,
    start: cabc.Callable[..., cabc.Awaitable[object]],
    *,
    capture: bool,
) -> object:
    """Run ``start`` under ``hook`` in the catalogue's scope."""
    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(typ.cast("typ.Any", hook)),
    ):
        return await start(output=_output_options(capture))


def _assert_bounded_delivery(
    seen: list[str],
    probe: _RetentionProbe,
    *,
    line_count: int,
    ceiling: int = _OBSERVE_TASK_CEILING,
) -> None:
    """Assert lossless delivery and a retained list bounded by ``ceiling``."""
    assert seen == _expected_lines(line_count), "every line must arrive in order"
    assert probe.emissions >= line_count, "every line event must have been emitted"
    assert probe.high_water <= ceiling, (
        f"retained tasks reached {probe.high_water}, above the ceiling {ceiling}"
    )
    assert probe.high_water < line_count // 4, (
        "the retained list must stay far below the line count; "
        f"got {probe.high_water} for {line_count} lines"
    )
