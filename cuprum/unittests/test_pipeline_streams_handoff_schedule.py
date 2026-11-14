"""Generated schedules of whole hand-offs that share one metrics collector.

The per-path tests pin one fault each. This module generates sequences of
complete hand-offs instead, so an arrangement no author wrote out — a success
after a guard fault, a cancellation after a refusal — must still settle its
hold, close the pair it acquired, and leave the collector's gauge balanced.

Every act drives the real coordinator over a private pipe; only the buffered
drain, the pump worker, and the fault being modelled are doubled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import typing as typ

import pytest
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.adapters.pump_metrics import RUST_PUMP_HELD_PAIRS, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._pump_stream_dispatch_support import clear_backend_caches
from cuprum.unittests._rust_pump_test_helpers import (
    RecordingCollector,
    allow_pause,
    fail_engage,
    install_fake_pump,
    owned_fds,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


async def _drain_completes(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Complete the buffered drain without moving bytes."""
    del reader, writer
    await asyncio.sleep(0)


async def _drain_fails(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Fail the buffered drain the way a broken transport does."""
    del reader, writer
    await asyncio.sleep(0)
    msg = "buffered drain failed"
    raise RuntimeError(msg)


def _fail_guard_construction(*_args: object, **_kwargs: object) -> typ.NoReturn:
    """Fail where the blocking-mode guard is built, after the real switch."""
    msg = "guard construction failed"
    raise RuntimeError(msg)


class _ScheduleRunner:
    """Run whole hand-offs over real descriptors and keep what they owe.

    Acts run one at a time against one collector, each through a fresh
    ``PumpMetricsHook``, so the gauge each act leaves behind is the shared
    collector's running total rather than a number owned by any one hook.
    """

    def __init__(self) -> None:
        """Start with one collector and no observations."""
        self.collector = RecordingCollector()
        self.acquired: list[int] = []
        self.observed: list[RustPumpHandoffOutcome] = []
        self._original_open = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )

    def record_open(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the real acquisition opened."""
        worker_fds = self._original_open(**kwargs)
        if worker_fds is not None:
            self.acquired.extend((worker_fds.reader_fd, worker_fds.writer_fd))
        return worker_fds

    def record_outcome(self, event: PumpEvent) -> None:
        """Record the routing outcome the hop published, if it published one."""
        if event.phase == "handoff" and isinstance(
            event.outcome,
            RustPumpHandoffOutcome,
        ):
            self.observed.append(event.outcome)

    async def drive(self, reader_fd: int, writer_fd: int) -> bool:
        """Drive the real coordinator over the descriptors a test owns."""
        return await _pipeline_streams._pump_over_raw_fds(
            reader=typ.cast("asyncio.StreamReader", object()),
            writer=None,
            handoff=_pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            ),
        )

    @contextlib.contextmanager
    def _patched(
        self,
        drain: cabc.Callable[..., cabc.Awaitable[None]],
        *,
        engage: cabc.Callable[..., object] | None = None,
        guard_init: cabc.Callable[..., typ.NoReturn] | None = None,
    ) -> cabc.Iterator[None]:
        """Apply the seams one act needs for the duration of the block."""
        with pytest.MonkeyPatch.context() as patch:
            self._install(patch, drain, engage=engage, guard_init=guard_init)
            yield

    def _install(
        self,
        patch: pytest.MonkeyPatch,
        drain: cabc.Callable[..., cabc.Awaitable[None]],
        *,
        engage: cabc.Callable[..., object] | None = None,
        guard_init: cabc.Callable[..., typ.NoReturn] | None = None,
    ) -> None:
        """Apply the seams one act needs to ``patch``."""
        allow_pause(patch, may_hand_off=True)
        patch.setattr(_pipeline_streams, "_drain_reader_buffer", drain)
        patch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.record_open,
        )
        install_fake_pump(patch, lambda _reader_fd, _writer_fd: 0)
        if engage is not None:
            patch.setattr(_pipeline_stream_fds._BlockingModeGuard, "engage", engage)
        if guard_init is not None:
            patch.setattr(
                _pipeline_stream_fds._BlockingModeGuard,
                "__init__",
                guard_init,
            )

    def run(
        self,
        drain: cabc.Callable[..., cabc.Awaitable[None]],
        *,
        engage: cabc.Callable[..., object] | None = None,
        guard_init: cabc.Callable[..., typ.NoReturn] | None = None,
    ) -> bool:
        """Run one hand-off to its settlement and report what it returned."""
        with (
            self._patched(drain, engage=engage, guard_init=guard_init),
            observe_pump(PumpMetricsHook(self.collector)),
            observe_pump(self.record_outcome),
            owned_fds() as (reader_fd, writer_fd),
        ):
            return asyncio.run(self.drive(reader_fd, writer_fd))

    def run_cancelled(self) -> None:
        """Cancel a hand-off inside its drain, before activation begins."""
        started = asyncio.Event()

        async def drain(
            _reader: asyncio.StreamReader,
            _writer: asyncio.StreamWriter | None,
        ) -> None:
            """Announce the drain, then wait to be cancelled."""
            started.set()
            await asyncio.Event().wait()

        with (
            self._patched(drain),
            observe_pump(PumpMetricsHook(self.collector)),
            observe_pump(self.record_outcome),
            owned_fds() as (reader_fd, writer_fd),
        ):
            asyncio.run(self._cancel_mid_drain(reader_fd, writer_fd, started))

    async def _cancel_mid_drain(
        self,
        reader_fd: int,
        writer_fd: int,
        started: asyncio.Event,
    ) -> None:
        """Cancel the coordinated hand-off once its drain is in flight."""
        task = asyncio.create_task(self.drive(reader_fd, writer_fd))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class HandoffSettlementMachine(RuleBasedStateMachine):
    """Interleave successful, failed, and cancelled hand-offs.

    After every act the shared gauge must hold exactly one balanced pair per
    act, every descriptor the act acquired must be closed, and the act must
    publish the one outcome its shape owes. Resume, refusal, and fault paths
    therefore cannot leak a hold or a descriptor into the next act.
    """

    _GAUGE_PAIR: typ.ClassVar[tuple[float, float]] = (1.0, 0.0)

    def __init__(self) -> None:
        """Start with one runner and no settled acts."""
        super().__init__()
        self.runner = _ScheduleRunner()
        self.acts = 0

    def _settled(self, expected: RustPumpHandoffOutcome | None) -> None:
        """Audit what the act that just finished owed, then clear its record."""
        runner = self.runner
        expected_events = [] if expected is None else [expected]
        assert runner.observed == expected_events, (
            f"the act must publish {expected_events}, found {runner.observed}"
        )
        assert len(runner.acquired) == 2, (
            f"each act must acquire one worker pair, found {runner.acquired}"
        )
        for fd in runner.acquired:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        self.acts += 1
        assert runner.collector.gauge_values(RUST_PUMP_HELD_PAIRS) == list(
            self._GAUGE_PAIR * self.acts
        ), "every act must raise and lower the shared gauge exactly once"
        runner.acquired.clear()
        runner.observed.clear()

    @rule()
    def successful_handoff(self) -> None:
        """Settle a completed hand-off after releasing its hold."""
        assert self.runner.run(_drain_completes), (
            "the native pump must carry a hand-off it can complete"
        )
        self._settled(RustPumpHandoffOutcome.SUBMITTED)

    @rule()
    def failed_drain(self) -> None:
        """Close an unactivated pair when its drain fails."""
        with pytest.raises(RuntimeError, match="buffered drain failed"):
            self.runner.run(_drain_fails)
        self._settled(RustPumpHandoffOutcome.DRAIN_FAILED)

    @rule()
    def cancelled_drain(self) -> None:
        """Settle a hold whose drain is cancelled before activation."""
        self.runner.run_cancelled()
        self._settled(RustPumpHandoffOutcome.DRAIN_CANCELLED)

    @rule()
    def refused_activation(self) -> None:
        """Fall back to Python when blocking mode is refused."""
        assert self.runner.run(_drain_completes, engage=fail_engage) is False, (
            "a blocking-mode refusal must let the Python pump carry the hop"
        )
        self._settled(RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED)

    @rule()
    def guard_construction_fault(self) -> None:
        """Re-raise an unexpected construction fault after settling the hold."""
        with pytest.raises(RuntimeError, match="guard construction failed"):
            self.runner.run(_drain_completes, guard_init=_fail_guard_construction)
        self._settled(None)

    @invariant()
    def the_gauge_never_reports_a_negative_hold(self) -> None:
        """Assert no settled act ever left the shared gauge below zero."""
        values = self.runner.collector.gauge_values(RUST_PUMP_HELD_PAIRS)
        assert all(value >= 0.0 for value in values), (
            f"a settled act must never leave a negative hold count, found {values}"
        )


TestHandoffSettlementMachine = HandoffSettlementMachine.TestCase
TestHandoffSettlementMachine.settings = settings(
    max_examples=25,
    stateful_step_count=12,
    deadline=None,
)
