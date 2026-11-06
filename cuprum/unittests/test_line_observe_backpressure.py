"""End-to-end bounds on retained observe-hook tasks for a flooding child.

The bug these tests cover needs a real child: a command that writes far more
lines than the retention ceiling, observed by an async hook. A spy on
``_StageObservation._emit_event`` records the high-water mark of the run-wide
pending-task list, which is what a line-flooding child used to grow without
limit.

Every case runs twice: once with capture on and once with capture off. The
uncaptured run is the regression configuration — the one where a child's
stream is read only because a line observer asked for it — and the bound must
hold there too.

The cases vary only in how the hook behaves — fast, slow, blocked, or failing
— because the bound must hold independently of hook speed. The blocked case
also proves delivery pauses at saturation, and the timeout case proves a
blocked observer holds final completion rather than being abandoned. The
stronger claim that the child itself stops writing lives in
``test_line_observe_child_backpressure.py``; the two-stage pipeline case lives
in ``test_line_observe_pipeline_backpressure.py``.
"""

from __future__ import annotations

import asyncio
import gc
import typing as typ

import pytest

from cuprum import sh
from cuprum._observe_task_retention import _OBSERVE_TASK_CEILING
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions
from cuprum.unittests.test_line_observe_support import (
    _LINE_COUNT,
    _assert_bounded_delivery,
    _chatty_command,
    _FailingHook,
    _FastHook,
    _GatedHook,
    _ObserverFailureError,
    _output_options,
    _RetentionProbe,
    _run_observed,
    _SlowHook,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent

_CAPTURE_MODES = [
    pytest.param(True, id="captured"),
    pytest.param(False, id="uncaptured"),
]


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
@pytest.mark.parametrize(
    ("hook_type", "line_count"),
    [(_FastHook, _LINE_COUNT), (_SlowHook, 2_000)],
    ids=["fast", "slow"],
)
def test_hook_speed_does_not_change_retention_bound(
    monkeypatch: pytest.MonkeyPatch,
    hook_type: type[_FastHook] | type[_SlowHook],
    line_count: int,
    capture: bool,
) -> None:
    """A flood observed by a fast or slow hook is delivered and bounded."""
    asyncio.run(_hook_speed_case(monkeypatch, hook_type(), line_count, capture))


async def _hook_speed_case(
    monkeypatch: pytest.MonkeyPatch,
    hook: _FastHook | _SlowHook,
    line_count: int,
    capture: bool,
) -> None:
    """Run the flood under ``hook`` and check the retention bound."""
    cmd, catalogue = _chatty_command(line_count)
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    await _run_observed(catalogue, hook, cmd.run, capture=capture)

    _assert_bounded_delivery(hook.seen, probe, line_count=line_count)


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_blocked_hook_saturates_then_delivers_every_line(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """A blocked observer pauses delivery, then resumes it losslessly."""
    asyncio.run(_blocked_hook_case(monkeypatch, capture))


async def _blocked_hook_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Saturate the list, prove delivery pauses, then release."""
    line_count = 5_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        run_task = asyncio.create_task(
            cmd.run(output=_output_options(capture)),
        )
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)
        saturated_count = len(hook.seen)
        assert saturated_count == probe.high_water, (
            "one held task per dispatched line is what saturates the list"
        )

        await asyncio.sleep(0.05)
        assert len(hook.seen) == saturated_count, (
            "a blocked observer must pause delivery rather than queue more lines"
        )

        hook.gate.set()
        await asyncio.wait_for(run_task, timeout=20.0)

    _assert_bounded_delivery(hook.seen, probe, line_count=line_count)


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_failing_hook_reports_the_first_failure_without_warnings(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Later failures are pruned, so only the first is reported and none leak."""
    asyncio.run(_failing_hook_case(monkeypatch, capture))


async def _failing_hook_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
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
        await cmd.run(output=_output_options(capture))

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


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_blocked_hook_holds_a_timeout_until_released(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """A blocked observer delays final completion but still settles once."""
    asyncio.run(_blocked_timeout_case(monkeypatch, capture))


async def _blocked_timeout_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
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
        await cmd.run(timeout=0.2, output=_output_options(capture))

    assert settled == ["settled"], "a timed-out run must settle exactly once"
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
    assert len(hook.seen) < line_count, (
        "the blocked child must not have delivered every line before the timeout"
    )


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_deferred_async_on_line_runs_and_holds_completion(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """A line deferred by saturation must still await its async ``on_line``."""
    asyncio.run(_async_on_line_case(monkeypatch, capture))


async def _async_on_line_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Saturate the observers, then hold the deferred line's async callback."""
    line_count = 5_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []
    # The callback must stay inert until the observer list is saturated, or it
    # would block the very first line and the ceiling would never be reached.
    is_armed = False

    async def on_line(event: LineEvent) -> None:
        """Record the line, then hold the first deferred delivery."""
        nonlocal is_armed
        seen.append(event.text)
        if is_armed:
            is_armed = False
            started.set()
            await release.wait()

    options = RunOutputOptions(capture=capture, on_line=on_line)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        run_task = asyncio.create_task(cmd.run(output=options))
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)
        is_armed = True
        hook.gate.set()
        await asyncio.wait_for(started.wait(), timeout=10.0)
        await asyncio.sleep(0.05)

        assert not run_task.done(), (
            "completion must wait for the deferred async callback to settle"
        )

        release.set()
        await asyncio.wait_for(run_task, timeout=20.0)

    assert seen[0] == "line-0", "the first line must still reach the callback"
    assert seen == [f"line-{index}" for index in range(line_count)], (
        "deferred delivery must reach the callback once per line, in order"
    )
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_lines_stream_delivers_every_line_deferred_by_saturation(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """``SafeCmd.lines`` must lose nothing while its observers are saturated."""
    asyncio.run(_deferred_lines_case(monkeypatch, capture))


async def _settled_delivery_count(
    delivered: list[str],
    *,
    timeout: float = 5.0,
) -> int:
    """Return the delivered count once it has stopped growing.

    A fixed sleep would either sample the stream's own queue while it is still
    draining or wait needlessly long; polling for two unchanged samples
    measures the halt itself.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    previous = len(delivered)
    stable = 0
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        current = len(delivered)
        stable = stable + 1 if current == previous else 0
        previous = current
        if stable >= 2:
            return current
    msg = f"line delivery never settled: {previous} lines delivered"
    raise AssertionError(msg)


async def _deferred_lines_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Iterate ``lines()`` across a saturation window and collect the result."""
    line_count = 5_000
    cmd, catalogue = _chatty_command(line_count)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    delivered: list[str] = []

    async def drain() -> None:
        """Iterate the stream to exhaustion, recording every delivered line."""
        async with cmd.lines(output=_output_options(capture)) as stream:
            async for event in stream:
                delivered.append(event.text)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        drain_task = asyncio.create_task(drain())
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)
        # The stream has its own queue, so lines already read are still
        # delivered after the driver blocks. Wait for that queue to drain
        # before measuring, or the sample catches the tail rather than the halt.
        stalled = await _settled_delivery_count(delivered)

        assert stalled < line_count, (
            "a saturated observer must pause the line stream short of the "
            f"whole flood, but {stalled} of {line_count} lines arrived"
        )

        hook.gate.set()
        await asyncio.wait_for(drain_task, timeout=20.0)

    assert delivered == [f"line-{index}" for index in range(line_count)], (
        "the line stream must deliver every line the saturation deferred"
    )
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
