"""Parity and preparation-cost tests for per-line observe-event emission.

Roadmap item 5.2.1 hoists invariant execution metadata out of the per-line
callback so a line no longer rebuilds it. Two classes of test guard that:

* :class:`TestLineEventPayloadParity` pins the payload the line callback
  produces against the generic :meth:`_StageObservation.emit` oracle. It
  passes before and after the hoist and is the contract the hoist must not
  break — payload characterization alone is expected to pass either way.
* The preparation-cost tests are the red test. They assert that preparing to
  observe a stream does not rebuild stable metadata per line, which fails
  against the un-hoisted implementation. They carry a strict ``xfail`` so the
  committed suite stays green while the failure stays recorded; the strictness
  makes the marker impossible to leave behind once the hoist lands.

The cost tests drive the *existing production callback factories* —
``_create_stream_callback`` for single commands and
``_create_stage_capture_tasks`` for pipelines — rather than the proposed
factory. A missing-method error would not be evidence of the performance bug,
so the red test must reach the bug through code that already exists. The
single-command factory carries the per-line red assertions, since it is the
shared seam's direct consumer; the pipeline factory's cases pin that it
composes at most one stdout callback per stage and constructs nothing while
preparing.

The parity test compares ``dataclasses.fields`` values directly rather than
``dataclasses.asdict``: the latter recurses into read-only mappings and can
fail on values it cannot copy.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import io
import typing as typ
import uuid

import pytest

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum.echo_events import EchoStream
from cuprum.events import ExecEvent, ExecId

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineStreamName
    from cuprum.sh import SafeCmd

# Removed in EP-M2, when the hoist makes these assertions true. ``strict=True``
# turns the leftover marker into a failure the moment they start passing.
RED_REASON = (
    "5.2.1 red test: the un-hoisted callback rebuilds argv and _EventDetails "
    "on every line; EP-M2 removes this marker"
)


class _NullProject:
    """Project stand-in carrying a name."""

    name = "test-project"


class _NullProgram:
    """Program stand-in whose ``str`` form is the event's program name."""

    def __str__(self) -> str:
        """Return the program name."""
        return "test-program"


class _NullCmd:
    """Command stand-in exposing only what observation emission reads."""

    program = _NullProgram()

    def __init__(self, argv: tuple[str, ...] = ("arg",)) -> None:
        """Store the argv this stand-in reports."""
        self.argv = argv

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """The full argv, program name first."""
        return (str(self.program), *self.argv)

    @property
    def project(self) -> _NullProject:
        """The project stand-in."""
        return _NullProject()


class _CountingCmd(_NullCmd):
    """Command stand-in counting accesses to its derived argv property."""

    accesses = 0

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """Count the access, then return the tuple."""
        type(self).accesses += 1
        return (str(self.program), *self.argv)


class _ExecutionStub:
    """The three attributes ``_create_stream_callback`` reads off a real one.

    Standing in for ``_SubprocessExecution`` keeps this test off the spawn
    machinery while still driving the real factory function.
    """

    def __init__(
        self,
        observation: _StageObservation,
        on_line: cabc.Callable[[typ.Any], object] | None = None,
    ) -> None:
        """Bind the observation the callback composes against."""
        self.observation = observation
        self.on_line = on_line
        self.started_at = 0.0


def _make_observation(
    observe_hooks: tuple[cabc.Callable[[ExecEvent], None], ...] = (),
    *,
    cmd: object | None = None,
    clock: cabc.Callable[[], float] | None = None,
    exec_id: ExecId | None = None,
) -> _StageObservation:
    """Build a minimal observation with the requested observe hooks."""
    hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=observe_hooks,
    )
    kwargs: dict[str, object] = {}
    if exec_id is not None:
        kwargs["exec_id"] = exec_id
    return _StageObservation(
        cmd=typ.cast("SafeCmd", _NullCmd() if cmd is None else cmd),
        hooks=hooks,
        tags={"project": "test-project"},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=(lambda: 1234.5) if clock is None else clock,
        **typ.cast("typ.Any", kwargs),
    )


def _fields(event: ExecEvent) -> dict[str, object]:
    """Return every declared field of ``event`` by name."""
    return {field.name: getattr(event, field.name) for field in dc.fields(ExecEvent)}


def _record_event_details(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every ``_EventDetails`` construction for the test's duration.

    ``_line_callbacks`` defers its import of ``_EventDetails`` to call time, so
    patching the defining module's attribute catches the production path
    without the test reaching into the callback's internals.

    Returns
    -------
    list[dict[str, object]]
        The keyword arguments of each recorded construction, in call order.
    """
    constructed: list[dict[str, object]] = []
    import cuprum._pipeline_types as pipeline_types

    real = pipeline_types._EventDetails

    def spy(**kwargs: object) -> object:
        """Record one construction's keywords, then build the real payload."""
        constructed.append(kwargs)
        return real(**typ.cast("typ.Any", kwargs))

    monkeypatch.setattr(pipeline_types, "_EventDetails", spy)
    return constructed


class TestLineEventPayloadParity:
    """A line event equals what generic emission builds for the same state."""

    def test_line_event_matches_generic_emit_field_for_field(self) -> None:
        """Every declared field agrees with the generic emission oracle.

        Both events come from one observation under a fixed clock, so the
        comparison is exact — including ``timestamp``, which must equal the
        single clock reading rather than merely being plausible.
        """
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,))

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(
                stream="stdout", pid=4242, on_line=None, started_at=0.0
            ),
        )
        assert callback is not None
        callback("a line")
        observation.emit("stdout", _EventDetails(pid=4242, line="a line"))

        assert len(captured) == 2
        line_event, generic_event = captured
        line_fields = _fields(line_event)
        generic_fields = _fields(generic_event)

        assert line_fields.keys() == generic_fields.keys(), (
            "the hoist must not add or remove declared ExecEvent fields"
        )
        for name, generic_value in generic_fields.items():
            assert line_fields[name] == generic_value, (
                f"field {name!r} diverged from the generic emission oracle"
            )

    def test_line_event_carries_the_pinned_clock_reading(self) -> None:
        """The clock is read exactly once per line, and the reading is kept."""
        readings = iter([10.0, 20.0, 30.0])
        captured: list[ExecEvent] = []
        observation = _make_observation(
            (captured.append,), clock=lambda: next(readings)
        )

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stderr", pid=7, on_line=None, started_at=0.0),
        )
        assert callback is not None
        callback("one")
        callback("two")

        assert [event.timestamp for event in captured] == [10.0, 20.0]

    def test_each_line_event_is_a_distinct_frozen_object(self) -> None:
        """Per-line events are separate objects, and mutation is refused."""
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,))

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stdout", pid=1, on_line=None, started_at=0.0),
        )
        assert callback is not None
        callback("first")
        callback("second")

        assert len(captured) == 2
        assert captured[0] is not captured[1], (
            "a reused event object would corrupt retained asynchronous payloads"
        )
        assert captured[0].line == "first"
        assert captured[1].line == "second", (
            "emitting a later line must not rewrite an earlier event"
        )
        with pytest.raises(dc.FrozenInstanceError):
            captured[0].line = "mutated"  # ty: ignore[invalid-assignment]

    def test_absent_pid_is_preserved_not_coerced(self) -> None:
        """A ``None`` PID stays ``None`` rather than being asserted or coerced."""
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,))

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(
                stream="stdout", pid=None, on_line=None, started_at=0.0
            ),
        )
        assert callback is not None
        callback("a line")

        assert captured[0].pid is None

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_phase_follows_the_stream(self, stream: LineStreamName) -> None:
        """Each stream's events carry that stream's phase."""
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,))
        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream=stream, pid=1, on_line=None, started_at=0.0),
        )
        assert callback is not None
        callback("line")

        assert captured[0].phase == stream

    def test_exec_id_is_shared_across_lines_of_one_stream(self) -> None:
        """Every line of one observation shares that execution's token."""
        token = ExecId(uuid.uuid4())
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,), exec_id=token)

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stdout", pid=1, on_line=None, started_at=0.0),
        )
        assert callback is not None
        callback("one")
        callback("two")

        assert [event.exec_id for event in captured] == [token, token]

    def test_on_line_only_path_still_delivers_every_line(self) -> None:
        """With no observe hooks, the caller's hook still sees every line."""
        seen: list[str] = []
        observation = _make_observation()

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(
                stream="stdout",
                pid=1,
                on_line=lambda event: seen.append(event.text),
                started_at=0.0,
            ),
        )
        assert callback is not None
        callback("hello")

        assert seen == ["hello"], "the caller's on_line must still see every line"

    def test_no_hooks_and_no_on_line_stays_callback_free(self) -> None:
        """The zero-observer path allocates no callback and reads no clock."""
        clock_calls = 0

        def clock() -> float:
            """Count the read, then answer a fixed reading."""
            nonlocal clock_calls
            clock_calls += 1
            return 0.0

        observation = _make_observation(clock=clock)
        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stdout", pid=1, on_line=None, started_at=0.0),
        )

        assert callback is None, (
            "no observe hooks and no on_line must keep the no-callback path"
        )
        assert clock_calls == 0, "preparation must not read the clock"

    def test_preparation_does_not_emit(self) -> None:
        """Preparing a callback emits nothing; only lines do."""
        captured: list[ExecEvent] = []
        observation = _make_observation((captured.append,))

        callback = _compose_line_callbacks(
            observation,
            _LineEmissionContext(stream="stdout", pid=1, on_line=None, started_at=0.0),
        )
        assert callback is not None

        assert captured == [], "preparation must not emit an event"

    def test_echo_stream_values_match_the_phase_literals(self) -> None:
        """The echo-stream members and the phase literals agree by value."""
        assert EchoStream.STDOUT == "stdout"
        assert EchoStream.STDERR == "stderr"


def _deliver(callback: cabc.Callable[[str], object], line_count: int) -> None:
    """Deliver ``line_count`` lines through ``callback``."""
    for index in range(line_count):
        callback(f"line-{index}")


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


class _PipelineConfigStub:
    """The config surface the pipeline capture task builder touches."""

    on_line = None
    capture = True
    consumes_stdout = True
    consumes_stderr = True

    @property
    def stream_config(self) -> object:
        """A minimal stdout stream config for the consumer."""
        from cuprum._streams import _StreamConfig

        return _StreamConfig(
            capture_output=True,
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="strict",
        )

    @property
    def stderr_stream_config(self) -> object:
        """A minimal stderr stream config for the consumer."""
        from cuprum._streams import _StreamConfig

        return _StreamConfig(
            capture_output=True,
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="strict",
            stream=EchoStream.STDERR,
        )


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

        def compose_spy(observation: object, context: object) -> object:
            """Record the composed callback, then build the real one."""
            callback = real_compose(observation, context)
            rig.composed.append((context.stream, callback))
            return callback

        async def consume_spy(
            stream: object,
            config: object,
            *,
            on_line: object = None,
            relay_diagnostics: object = None,
        ) -> None:
            """Record the ``on_line`` the consumer would use, then drain nothing."""
            _ = (stream, relay_diagnostics)
            rig.consumed.append((typ.cast("LineStreamName", config.stream), on_line))
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
        end, and the delivery end is the single-command red test's job.
        """
        constructed = _record_event_details(monkeypatch)
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

    @pytest.mark.xfail(strict=True, reason=RED_REASON)
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
        before and after the hoist, so it carries no expected-failure
        marker; it pins that the cost is proportional to delivered lines
        rather than to preparation alone.
        """
        from cuprum._subprocess_streams import _create_stream_callback

        constructed = _record_event_details(monkeypatch)
        execution = _ExecutionStub(_make_observation((lambda event: None,)))

        callback = _create_stream_callback(typ.cast("typ.Any", execution), "stdout", 99)
        assert callback is not None
        _deliver(callback, 0)

        assert constructed == [], (
            "preparing and delivering nothing must construct nothing"
        )

    @pytest.mark.xfail(strict=True, reason=RED_REASON)
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
