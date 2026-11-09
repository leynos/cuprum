"""Deferred line delivery once the observe-hook retention ceiling is reached.

A line that arrives at the ceiling is not dropped: the read loop suspends
until a slot frees, then emits the line and calls the caller's ``on_line``.
These cases pin both halves of that promise against a real flooding child —
the deferred line still reaches an asynchronous callback, and completion waits
for that callback to settle; and ``SafeCmd.lines()`` loses nothing while its
own queue drains behind the suspended driver.

The retention bound itself, and the hook-speed matrix, live in
``test_line_observe_backpressure.py``.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import sh
from cuprum._observe_task_retention import _OBSERVE_TASK_CEILING
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions
from cuprum.unittests.test_line_observe_support import (
    _chatty_command,
    _GatedHook,
    _output_options,
    _RetentionProbe,
)

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent, LineHook

_CAPTURE_MODES = [
    pytest.param(True, id="captured"),
    pytest.param(False, id="uncaptured"),
]

#: How long the stream may keep growing before the halt measurement gives up.
_SETTLE_TIMEOUT_SECONDS = 5.0

#: Flood size for these cases. Smaller than the shared ``_LINE_COUNT`` because
#: each case waits for a gate at the ceiling *and* for a callback, so the
#: shorter flood keeps the run time bounded without weakening the bound.
_DEFERRED_LINE_COUNT = 5_000


class _DeferredCallback:
    """Hold the first line delivered after the observer gate opens.

    The callback stays inert until the observer list is saturated, or it would
    block the very first line and the ceiling would never be reached.
    """

    def __init__(self) -> None:
        """Start inert, with no line recorded yet."""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.seen: list[str] = []
        self.is_armed = False

    async def __call__(self, event: LineEvent) -> None:
        """Record the line, then hold the first armed delivery."""
        self.seen.append(event.text)
        if self.is_armed:
            self.is_armed = False
            self.started.set()
            await self.release.wait()


def _capacity_wait_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the retention logger's capacity-wait records."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "cuprum._retention_telemetry"
        and record.getMessage().startswith("observe_task_capacity_wait")
    ]


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_deferred_async_on_line_runs_and_holds_completion(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capture: bool,
) -> None:
    """A line deferred by saturation must still await its async ``on_line``."""
    # The callback could also start late for an unrelated reason; the wait
    # record is what shows the line itself was deferred by admission.
    caplog.set_level(logging.INFO, logger="cuprum._retention_telemetry")

    asyncio.run(_async_on_line_case(monkeypatch, capture))

    assert _capacity_wait_messages(caplog), (
        "the callback must not start before the line reader was suspended for "
        "observer capacity"
    )


async def _async_on_line_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Saturate the observers, then hold the deferred line's async callback."""
    cmd, catalogue = _chatty_command(_DEFERRED_LINE_COUNT)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    callback = _DeferredCallback()
    # ``LineHook`` is the public synchronous contract, but the drain loop also
    # awaits a hook that returns an awaitable. Pinning that internal seam is
    # the point of this case, so the cast is deliberate.
    options = RunOutputOptions(
        capture=capture,
        on_line=typ.cast("LineHook", callback),
    )

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
    ):
        run_task = asyncio.create_task(cmd.run(output=options))
        await asyncio.wait_for(probe.saturated.wait(), timeout=10.0)
        callback.is_armed = True
        hook.gate.set()
        await asyncio.wait_for(callback.started.wait(), timeout=10.0)
        await asyncio.sleep(0.05)

        assert not run_task.done(), (
            "completion must wait for the deferred async callback to settle"
        )

        callback.release.set()
        await asyncio.wait_for(run_task, timeout=20.0)

    assert callback.seen[0] == "line-0", "the first line must still reach the callback"
    expected = [f"line-{index}" for index in range(_DEFERRED_LINE_COUNT)]
    assert callback.seen == expected, (
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


async def _settled_delivery_count(delivered: list[str]) -> int:
    """Return the delivered count once it has stopped growing.

    A fixed sleep would either sample the stream's own queue while it is still
    draining or wait needlessly long; polling for two unchanged samples
    measures the halt itself.

    Returns
    -------
    int
        The number of lines delivered once two consecutive samples agreed.

    Raises
    ------
    AssertionError
        If delivery is still growing when the settle deadline passes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_TIMEOUT_SECONDS
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
    cmd, catalogue = _chatty_command(_DEFERRED_LINE_COUNT)
    hook = _GatedHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)
    delivered: list[str] = []

    async def drain() -> None:
        """Iterate the stream to exhaustion, recording every delivered line."""
        async with cmd.lines(output=_output_options(capture)) as stream:
            async for event in stream:
                # Each line must be recorded as it arrives: an async
                # comprehension would buffer the whole stream and hide the
                # halt this case measures.
                delivered.append(event.text)  # ruff: ignore[manual-list-comprehension]

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

        assert stalled < _DEFERRED_LINE_COUNT, (
            "a saturated observer must pause the line stream short of the "
            f"whole flood, but {stalled} of {_DEFERRED_LINE_COUNT} lines arrived"
        )

        hook.gate.set()
        await asyncio.wait_for(drain_task, timeout=20.0)

    expected = [f"line-{index}" for index in range(_DEFERRED_LINE_COUNT)]
    assert delivered == expected, (
        "the line stream must deliver every line the saturation deferred"
    )
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
