"""End-to-end bounds on retained observe-hook tasks for a flooding child.

The bug these tests cover needs a real child: a command that writes far more
lines than the retention ceiling, observed by an async hook, with capture
disabled. A spy on ``_StageObservation._emit_event`` records the high-water
mark of the run-wide pending-task list, which is what a line-flooding child
used to grow without limit.

The cases vary only in how the hook behaves — fast, slow, blocked, or failing
— because the bound must hold independently of hook speed. The blocked case
also proves the backpressure reaches the child, and the timeout case proves a
blocked observer holds final completion rather than being abandoned.
"""

from __future__ import annotations

import asyncio
import gc
import typing as typ

import pytest

from cuprum import sh
from cuprum._observability import _OBSERVE_TASK_CEILING
from cuprum._pipeline_types import _StageObservation
from cuprum.context import ScopeConfig, scoped
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent
    from cuprum.sh import Pipeline, SafeCmd

_LINE_COUNT = 20_000


class _ObserverFailureError(Exception):
    """Raised by a deliberately failing async observe hook."""

    def __init__(self, line: str) -> None:
        """Record the line whose observation failed."""
        super().__init__(line)
        self.line = line


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
    """Build a two-stage pipeline whose first stage floods stderr."""
    catalogue, program = python_catalogue()
    build = sh.make(program, catalogue=catalogue)
    producer = build(
        "-c",
        "import sys; "
        "sys.stderr.write(''.join(f'line-{i}\\n' for i in range("
        f"{line_count}))); "
        "sys.stdout.write('done\\n')",
    )
    consumer = build("-c", "import sys; sys.stdin.read()")
    return producer | consumer, catalogue


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

    def record(self, size: int) -> None:
        """Record one post-emission list size."""
        self.emissions += 1
        self.high_water = max(self.high_water, size)
        if size >= _OBSERVE_TASK_CEILING:
            self.saturated.set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the observation dispatcher to record after each emission."""
        original = _StageObservation._emit_event

        def spy(observation: _StageObservation, event: ExecEvent) -> None:
            """Record the list size after the real dispatcher runs."""
            original(observation, event)
            self.record(len(observation.pending_tasks))

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
    start: cabc.Callable[[], cabc.Awaitable[object]],
) -> object:
    """Run ``start`` under ``hook`` in the catalogue's scope."""
    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(typ.cast("typ.Any", hook)),
    ):
        return await start()


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


def test_fast_hook_retention_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flood of lines observed by a fast hook is delivered and bounded."""
    asyncio.run(_fast_hook_case(monkeypatch))


async def _fast_hook_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the flood under a fast observer and check the bound."""
    cmd, catalogue = _chatty_command()
    hook = _FastHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    await _run_observed(catalogue, hook, cmd.run)

    _assert_bounded_delivery(hook.seen, probe, line_count=_LINE_COUNT)


def test_slow_hook_retention_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook speed must not change the retention bound."""
    asyncio.run(_slow_hook_case(monkeypatch))


async def _slow_hook_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the flood under a slow observer and check the bound."""
    line_count = 2_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _SlowHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    await _run_observed(catalogue, hook, cmd.run)

    _assert_bounded_delivery(hook.seen, probe, line_count=line_count)


def test_blocked_hook_saturates_then_delivers_every_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked observer pauses the child, then delivery resumes losslessly."""
    asyncio.run(_blocked_hook_case(monkeypatch))


async def _blocked_hook_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saturate the list, prove the child pauses, then release."""
    line_count = 5_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        run_task = asyncio.create_task(cmd.run())
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)
        saturated_count = len(hook.seen)
        assert saturated_count == probe.high_water, (
            "one held task per dispatched line is what saturates the list"
        )

        await asyncio.sleep(0.05)
        assert len(hook.seen) == saturated_count, (
            "a blocked observer must pause the child rather than queue more lines"
        )

        hook.gate.set()
        await asyncio.wait_for(run_task, timeout=20.0)

    _assert_bounded_delivery(hook.seen, probe, line_count=line_count)


def test_failing_hook_reports_the_first_failure_without_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later failures are pruned, so only the first is reported and none leak."""
    asyncio.run(_failing_hook_case(monkeypatch))


async def _failing_hook_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the flood under a failing observer and inspect the report."""
    line_count = 600
    cmd, catalogue = _chatty_command(line_count)
    hook = _FailingHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    reports: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: reports.append(context)
    )

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
        pytest.raises(_ObserverFailureError) as exc_info,
    ):
        await cmd.run()

    gc.collect()
    await asyncio.sleep(0)

    assert exc_info.value.line == "line-0", (
        "finalization must report the earliest failure in list order"
    )
    # The retained early failure is a settled task, so the list may hold one
    # more entry than the unfinished ceiling it bounds.
    assert probe.high_water <= _OBSERVE_TASK_CEILING + 1, (
        f"retained tasks reached {probe.high_water}"
    )
    messages = [typ.cast("str", report.get("message", "")) for report in reports]
    assert not [message for message in messages if "never retrieved" in message], (
        f"pruned failures must have their exceptions retrieved, got {messages!r}"
    )


def test_blocked_hook_holds_a_timeout_until_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked observer delays final completion but still settles once."""
    asyncio.run(_blocked_timeout_case(monkeypatch))


async def _blocked_timeout_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Time out a blocked run and release the gate afterwards."""
    line_count = 5_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    settled: list[str] = []

    def observe(event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Record settlement, then delegate the line observation."""
        if event.phase == "settled":
            settled.append(typ.cast("str", event.phase))
        return hook(event)

    loop = asyncio.get_running_loop()
    loop.call_later(0.6, hook.gate.set)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(observe),
        pytest.raises(sh.TimeoutExpired),
    ):
        await cmd.run(timeout=0.2)

    assert settled == ["settled"], "a timed-out run must settle exactly once"
    assert probe.high_water <= _OBSERVE_TASK_CEILING
    assert len(hook.seen) < line_count, (
        "the blocked child must not have delivered every line before the timeout"
    )


def test_pipeline_stages_share_one_bounded_retention_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty pipeline stage is bounded by the run-wide list, not per stream."""
    asyncio.run(_pipeline_case(monkeypatch))


async def _pipeline_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a chatty pipeline and check the shared bound."""
    line_count = 5_000
    pipeline, catalogue = _chatty_pipeline(line_count)
    hook = _FastHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    await _run_observed(catalogue, hook, pipeline.run)

    _assert_bounded_delivery(hook.seen, probe, line_count=line_count)
