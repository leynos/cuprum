"""Preparation-cost tests for per-line observe-event emission.

These are roadmap item 5.2.1's assertions that preparing to observe a stream
does not rebuild stable metadata per line. They began as a red test carrying
strict ``xfail`` markers, which flipped to ``XPASS`` and were removed when the
hoist landed; ``test_no_event_details_construction_per_line`` and
``test_argv_is_not_rebuilt_per_line`` are now ordinary passing tests, and the
strictness is what forced the markers out rather than letting them linger.

A zero count is a weak assertion on its own, because a recorder that had gone
blind would report the same zero. Every case that asserts a zero calls
``_prove_recorder_is_live`` first, so the recorder has to demonstrate it can
still see a construction before its silence is treated as evidence.

The cases drive the *existing production callback factories* —
``_create_stream_callback`` for single commands and
``_create_stage_capture_tasks`` for pipelines — rather than a proposed factory.
A missing-method error would not be evidence of the performance bug, so the
tests must reach the bug through code that already exists. The single-command
factory carries the per-line assertions, since it is the shared seam's direct
consumer; the pipeline factory's cases pin that it composes at most one stdout
callback per stage and constructs nothing while preparing.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum._streams import _StreamConfig
from cuprum.echo_events import EchoStream
from cuprum.unittests.test_line_event_emission_support import (
    _CountingCmd,
    _deliver,
    _ExecutionStub,
    _make_observation,
    _prove_recorder_is_live,
    _record_event_details,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_callbacks import _LineEmissionContext
    from cuprum._pipeline_types import _StageObservation
    from cuprum.lines import LineStreamName, _LineHookOutcome


class _PipelineProcessStub:
    """The two attributes ``_create_stage_capture_tasks`` reads off a process."""

    def __init__(self, pid: int | None = 77) -> None:
        """Bind the PID the stage's events should carry."""
        self.pid = pid
        self.stdout = None
        self.stderr = None


class _StageRequestStub:
    """A capture request wired for the pipeline factory under test.

    ``consumes_stdout`` and ``consumes_stderr`` must both be true for the
    factory to create the consumers that own the composed callbacks; the
    config stub below makes them so without an execution context.
    """

    def __init__(self, observation: _StageObservation, *, is_last_stage: bool) -> None:
        """Bind the observation and stage position."""
        self.process = _PipelineProcessStub()
        self.config = _PipelineConfigStub()
        self.observation = observation
        self.is_last_stage = is_last_stage
        self.started_at = 0.0


def _minimal_stream_config(stream: EchoStream = EchoStream.STDOUT) -> _StreamConfig:
    """Build a minimal captured stream config for one output stream."""
    return _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="strict",
        stream=stream,
    )


class _PipelineConfigStub:
    """The config surface the pipeline capture task builder touches."""

    on_line = None
    capture = True
    consumes_stdout = True
    consumes_stderr = True

    @property
    def stream_config(self) -> _StreamConfig:
        """A minimal stdout stream config for the consumer."""
        return _minimal_stream_config()

    @property
    def stderr_stream_config(self) -> _StreamConfig:
        """A minimal stderr stream config for the consumer."""
        return _minimal_stream_config(EchoStream.STDERR)


class _StageRig:
    """Records what one pipeline stage composed and what its consumers got.

    Attributes
    ----------
    composed:
        The callbacks ``_compose_line_callbacks`` returned, per stream, in
        composition order.
    consumed:
        The ``on_line`` each consumer was handed, per stream, in the same
        order, so the rig can prove the consumer uses the composed callable
        rather than an equivalent one.

    """

    def __init__(self) -> None:
        """Start with no recorded streams."""
        self.composed: list[tuple[LineStreamName, object]] = []
        self.consumed: list[tuple[LineStreamName, object]] = []


class TestPipelinePreparationCost:
    """The pipeline path must reach the shared seam, not re-implement it.

    ``_create_stage_capture_tasks`` composes its callbacks through
    ``_compose_line_callbacks`` and hands the result straight to the stream
    consumer, so the hoist reaches the pipeline by construction. That routing
    is what these cases pin: a future refactor that gave the pipeline its own
    inline closure would leave the single-command red test passing while the
    pipeline kept paying the per-line cost. The per-line *red* assertion lives
    in :class:`TestSingleCommandPreparationCost`, because both factories
    return the same closure object and a second copy of that assertion would
    only restate it.
    """

    def _drive_stage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rig: _StageRig,
        *,
        is_last_stage: bool,
    ) -> None:
        """Create one stage's capture tasks, recording composition and wiring.

        The consumer stand-in replaces ``_consume_stream`` so no real stream
        machinery is needed; it is an async function, so ``create_task``
        accepts it and the stage's tasks settle by ordinary await.
        """
        from cuprum import _pipeline_stage_streams as stage_streams

        real_compose = stage_streams._compose_line_callbacks

        def compose_spy(
            observation: _StageObservation,
            context: _LineEmissionContext,
        ) -> cabc.Callable[[str], _LineHookOutcome] | None:
            """Record the composed callback, then build the real one."""
            callback = real_compose(observation, context)
            rig.composed.append((context.stream, callback))
            return callback

        async def consume_spy(
            stream: asyncio.StreamReader | None,
            config: _StreamConfig,
            *,
            on_line: object = None,
            relay_diagnostics: object = None,
        ) -> None:
            """Record the ``on_line`` the consumer would use, then drain nothing."""
            _ = (stream, relay_diagnostics)
            rig.consumed.append((config.stream.value, on_line))
            # The real consumer suspends on its first read; yielding once keeps
            # this stand-in a genuine coroutine and lets the stage's tasks
            # settle through the ordinary await below.
            await asyncio.sleep(0)

        monkeypatch.setattr(stage_streams, "_compose_line_callbacks", compose_spy)
        monkeypatch.setattr(stage_streams, "_consume_stream", consume_spy)

        async def drive() -> None:
            """Build the stage's tasks and let them finish."""
            request = _StageRequestStub(
                _make_observation((lambda event: None,)),
                is_last_stage=is_last_stage,
            )
            created = stage_streams._create_stage_capture_tasks(
                typ.cast("typ.Any", request)
            )
            live = [task for task in (created[0], created[1]) if task is not None]
            await asyncio.gather(*live)

        asyncio.run(drive())

    def test_pipeline_consumer_runs_the_composed_callback_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consumer's ``on_line`` *is* the callback the factory composed.

        Identity, not equality: an equal-but-separate callable would mean the
        pipeline built its own per-line path, which is precisely the
        divergence the shared seam exists to prevent.
        """
        rig = _StageRig()
        self._drive_stage(monkeypatch, rig, is_last_stage=True)

        composed = dict(rig.composed)
        consumed = dict(rig.consumed)
        assert set(composed) == {"stdout", "stderr"}, (
            "the final stage composes one callback per consumed stream"
        )
        assert composed == consumed, (
            "each consumer must be handed the factory's own composed callback"
        )
        assert all(callback is not None for callback in composed.values()), (
            "an observe hook must produce a callback for every stream"
        )

    def test_pipeline_preparation_constructs_no_event_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Composing a pipeline stage's callbacks constructs no payload.

        Preparation is constant in the number of lines: this pins the zero
        end, and the delivery end is the single-command test's job.
        """
        constructed = _record_event_details(monkeypatch)
        _prove_recorder_is_live(constructed)
        self._drive_stage(monkeypatch, _StageRig(), is_last_stage=True)

        assert constructed == [], (
            "preparing a pipeline stage must not construct an event payload"
        )

    def test_interior_stage_composes_no_stdout_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interior stage observes stderr only; its stdout belongs to the next.

        A regression that gave every stage a stdout emitter would double-count
        line construction, so the per-pipeline stdout binding is stated here.
        """
        rig = _StageRig()
        self._drive_stage(monkeypatch, rig, is_last_stage=False)

        assert [stream for stream, _ in rig.composed] == ["stderr"], (
            "an interior stage must compose no stdout lines"
        )
        assert [stream for stream, _ in rig.consumed] == ["stderr"]


class TestSingleCommandPreparationCost:
    """The single-command factory must not rebuild metadata per line.

    Exercises the real ``_create_stream_callback``, which is what
    ``SafeCmd.run()`` and ``SafeCmd.lines()`` reach through the stream
    consumers.
    """

    @pytest.mark.parametrize("line_count", [1, 100])
    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_no_event_details_construction_per_line(
        self,
        line_count: int,
        stream: LineStreamName,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Line delivery constructs no transient ``_EventDetails`` payload."""
        from cuprum._subprocess_streams import _create_stream_callback

        constructed = _record_event_details(monkeypatch)
        _prove_recorder_is_live(constructed)
        execution = _ExecutionStub(_make_observation((lambda event: None,)))

        callback = _create_stream_callback(typ.cast("typ.Any", execution), stream, 99)
        assert callback is not None

        before = len(constructed)
        _deliver(callback, line_count)
        per_line = len(constructed) - before

        assert per_line == 0, (
            f"{per_line} _EventDetails were built while delivering "
            f"{line_count} {stream} lines; the hoist must bind this once"
        )

    def test_zero_delivered_lines_construct_no_payloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delivering nothing constructs nothing, hoisted or not.

        This is the zero end of V1's 0/1/100 parametrization. It holds
        before and after the hoist, so it never carried an expected-failure
        marker; it pins that the cost is proportional to delivered lines
        rather than to preparation alone.
        """
        from cuprum._subprocess_streams import _create_stream_callback

        constructed = _record_event_details(monkeypatch)
        _prove_recorder_is_live(constructed)
        execution = _ExecutionStub(_make_observation((lambda event: None,)))

        callback = _create_stream_callback(typ.cast("typ.Any", execution), "stdout", 99)
        assert callback is not None
        _deliver(callback, 0)

        assert constructed == [], (
            "preparing and delivering nothing must construct nothing"
        )

    @pytest.mark.parametrize("line_count", [1, 100])
    def test_argv_is_not_rebuilt_per_line(self, line_count: int) -> None:
        """The full argv tuple is resolved during preparation, not per line."""
        from cuprum._subprocess_streams import _create_stream_callback

        _CountingCmd.accesses = 0
        observation = _make_observation((lambda event: None,), cmd=_CountingCmd())
        execution = _ExecutionStub(observation)

        callback = _create_stream_callback(typ.cast("typ.Any", execution), "stdout", 5)
        assert callback is not None
        during_preparation = _CountingCmd.accesses
        _deliver(callback, line_count)
        during_delivery = _CountingCmd.accesses - during_preparation

        assert during_preparation >= 1, (
            "preparation must resolve argv once so lines can reuse it"
        )
        assert during_delivery == 0, (
            f"argv_with_program was rebuilt {during_delivery} times while "
            f"delivering {line_count} lines"
        )

    def test_zero_observe_hooks_never_reaches_the_clock(self) -> None:
        """With no observe hooks and no on_line the factory returns ``None``."""
        from cuprum._subprocess_streams import _create_stream_callback

        clock_calls = 0

        def clock() -> float:
            """Count the read, then answer a fixed reading."""
            nonlocal clock_calls
            clock_calls += 1
            return 0.0

        execution = _ExecutionStub(_make_observation(clock=clock))
        callback = _create_stream_callback(typ.cast("typ.Any", execution), "stdout", 1)

        assert callback is None
        assert clock_calls == 0, "the no-observer path must not read the clock"
