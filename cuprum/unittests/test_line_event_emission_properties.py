"""Property tests for per-line event payload preservation.

Roadmap item 5.2.1 hoists invariant execution metadata out of the per-line
callback. The named characterization tests in ``test_line_event_emission`` pin
the payload for a handful of chosen cases; the properties here sample the
domain instead, so a hoist that quietly widens or narrows the field set, reuses
an event object, or drops a clock read cannot pass on luck.

The relation under test is: *substituting captured stable fields leaves each
event equal to the old constructor at the same clock value*. It is checked by
comparing every declared ``ExecEvent`` field against the generic
:meth:`_StageObservation.emit` oracle rather than against a handwritten
expectation, so the oracle and the implementation cannot drift together. The
clock is pinned to the same value for both calls, so the comparison is exact
rather than merely plausible.

Four invariants are asserted for every generated case:

* payload identity — line events equal the generic emission for the same state;
* one clock call per delivered line, in order;
* distinct object identities, so a later line cannot rewrite an earlier event;
* earlier payloads are unchanged after later deliveries.

Field order, constructor defaults, and ``repr`` are pinned by the named tests
instead: they are properties of the class, not of the hoist, and generating
inputs cannot say anything more about them.

Every generator here *guarantees* the shape its property is about, and every
property asserts that shape before asserting the behaviour. A generator that
stopped producing the interesting case therefore fails its property rather than
leaving it true and empty. The assertions are the anti-vacuity witnesses; the
``@example`` rows beside each property state the boundary cases by hand.

Timestamps are finite but *not* required to increase: the clock is the
operating system's, and a hoist that reordered events must not be able to hide
behind a monotonicity assumption.
"""

from __future__ import annotations

import dataclasses as dc
import math
import typing as typ

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum.events import ExecEvent, ExecId, new_exec_id

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineStreamName


class _NullProgram:
    """Program stand-in whose ``str`` form is the event's program name."""

    def __str__(self) -> str:
        """Return the program name."""
        return "prop-program"


class _NullProject:
    """Project stand-in carrying a name."""

    name = "prop-project"


class _NullCmd:
    """Command stand-in exposing only what observation emission reads."""

    program = _NullProgram()

    def __init__(self, argv: tuple[str, ...] = ()) -> None:
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


class _SetClock:
    """A clock pinned to an explicit reading, counting every read.

    A generator cannot be shared between the subject and the oracle, and index
    arithmetic over a scripted sequence silently desynchronizes the two. Pinning
    the reading makes "the same clock value" literal: the test sets the value,
    reads it through the line path, sets the same value again, and reads it
    through the oracle.
    """

    def __init__(self, value: float = 0.0) -> None:
        """Pin the clock to ``value``."""
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        """Return the pinned reading, counting the call."""
        self.calls += 1
        return self.value


@dc.dataclass(frozen=True, slots=True)
class _EmissionCase:
    """One generated per-line emission case.

    Attributes
    ----------
    lines:
        The line texts to deliver, in order.
    timestamps:
        One pinned clock reading per line, index-aligned with ``lines``.
    stream:
        The phase the emitter is composed for.
    pid:
        The process identifier the emitter binds, or ``None``.
    argv:
        The command argv the observation reports.

    """

    lines: tuple[str, ...]
    timestamps: tuple[float, ...]
    stream: LineStreamName
    pid: int | None
    argv: tuple[str, ...]


# Lines are Unicode and may repeat or be empty; the callback must not care.
_LINE = st.text(max_size=40)
# Finite and non-monotonic on purpose: a clock that goes backwards still
# produces a fresh event per line, and no ordering guarantee may be assumed.
# ``allow_nan=False`` keeps the comparison exact; ``allow_infinity=False``
# avoids a JSON round-trip difference that has nothing to do with the hoist.
_TIMESTAMP = st.floats(allow_nan=False, allow_infinity=False, width=32)
_PHASES = st.sampled_from(["stdout", "stderr"])
_PIDS = st.one_of(st.none(), st.integers(min_value=0, max_value=2**31 - 1))
_ARGS = st.lists(st.text(max_size=12), min_size=0, max_size=4).map(tuple)


@st.composite
def _emission_case(draw: st.DrawFn, *, min_lines: int = 1) -> _EmissionCase:
    """Draw one correlated line/timestamp/metadata case.

    ``lines`` and ``timestamps`` are drawn together and index-aligned: each
    property pins the clock per line, so a case whose two lists had unrelated
    lengths could not be read.

    ``min_lines`` defaults to 1 rather than 0 because these properties are
    about per-line behaviour: with no lines, every claim they make is true
    without anything having happened. The zero-line boundary is a named case in
    ``test_line_event_emission`` instead, where it asserts something concrete.

    Returns
    -------
    _EmissionCase
        A case of at least ``min_lines`` lines with one clock reading each.
    """
    lines = draw(st.lists(_LINE, min_size=min_lines, max_size=30))
    stamps = st.lists(_TIMESTAMP, min_size=len(lines), max_size=len(lines))
    return _EmissionCase(
        lines=tuple(lines),
        timestamps=tuple(draw(stamps)),
        stream=draw(_PHASES),
        pid=draw(_PIDS),
        argv=draw(_ARGS),
    )


@st.composite
def _interleaved_case(
    draw: st.DrawFn,
) -> tuple[tuple[str, ...], tuple[str, ...], int | None, int | None]:
    """Draw one non-empty interleaved two-stream case.

    Returns
    -------
    tuple[tuple[str, ...], tuple[str, ...], int | None, int | None]
        The stdout lines, stderr lines, stdout PID, and stderr PID.
    """
    line_lists = st.lists(_LINE, min_size=1, max_size=8).map(tuple)
    return (draw(line_lists), draw(line_lists), draw(_PIDS), draw(_PIDS))


# The boundary rows stated by hand. ``None`` is generated by ``_PIDS`` already
# but is spelled out because it is the case a coercion bug would break. Two
# streams sharing a PID is the collision a process-wide cache would cause.
_EXAMPLE_UNICODE = example(
    case=_EmissionCase(
        lines=("", "repeated", "repeated", "é中\U0001f600"),
        timestamps=(0.0, 0.0, 0.0, 0.0),
        stream="stdout",
        pid=None,
        argv=(),
    ),
)
_EXAMPLE_SINGLE = example(
    case=_EmissionCase(
        lines=("only",),
        timestamps=(1.0,),
        stream="stderr",
        pid=0,
        argv=("--flag",),
    ),
)
_EXAMPLE_BACKWARDS = example(
    case=_EmissionCase(
        lines=("a", "b"),
        timestamps=(0.0, -1.5),
        stream="stdout",
        pid=2**31 - 1,
        argv=("arg one", ""),
    ),
)
_EXAMPLE_TWO_STREAMS = example(case=(("only",), ("only",), 0, 2**31 - 1))


def _observation(
    cmd: _NullCmd,
    clock: cabc.Callable[[], float],
    captured: list[ExecEvent],
    *,
    exec_id: ExecId | None = None,
) -> _StageObservation:
    """Build an observation whose single observe hook retains events."""
    hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=(captured.append,),
    )
    kwargs: dict[str, object] = {}
    if exec_id is not None:
        kwargs["exec_id"] = exec_id
    return _StageObservation(
        cmd=typ.cast("typ.Any", cmd),
        hooks=hooks,
        tags={"project": "prop-project"},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=clock,
        **typ.cast("typ.Any", kwargs),
    )


def _fields(event: ExecEvent) -> dict[str, object]:
    """Return every declared field of ``event`` by name."""
    return {field.name: getattr(event, field.name) for field in dc.fields(ExecEvent)}


def _emitter(
    observation: _StageObservation,
    *,
    stream: LineStreamName,
    pid: int | None,
) -> cabc.Callable[[str], object]:
    """Compose a line callback, asserting the observing path was taken."""
    callback = _compose_line_callbacks(
        observation,
        _LineEmissionContext(stream=stream, pid=pid, on_line=None, started_at=0.0),
    )
    assert callback is not None, "an observe hook must produce a callback"
    return callback


def _assert_oracle_parity(captured: cabc.Sequence[ExecEvent]) -> None:
    """Each line event must pair with, and equal, the oracle emitted beside it.

    The captured order is line, oracle, line, oracle, ..., so the pairing is
    positional and a mismatch in either direction fails.
    """
    for index in range(0, len(captured), 2):
        line_fields = _fields(captured[index])
        oracle_fields = _fields(captured[index + 1])
        assert line_fields.keys() == oracle_fields.keys(), (
            "the hoist must not add or remove declared ExecEvent fields"
        )
        for name, oracle_value in oracle_fields.items():
            assert line_fields[name] == oracle_value, (
                f"line {index // 2}: field {name!r} diverged from the oracle"
            )


def _assert_streams_kept_apart(
    captured: cabc.Sequence[ExecEvent],
    out_lines: tuple[str, ...],
    err_lines: tuple[str, ...],
    out_pid: int | None,
    err_pid: int | None,
) -> None:
    """Assert neither stream's lines or PID crossed into the other's events."""
    assert len(captured) == len(out_lines) + len(err_lines), (
        "every generated line must reach exactly one emitter"
    )
    out_events = [event for event in captured if event.phase == "stdout"]
    err_events = [event for event in captured if event.phase == "stderr"]
    assert [event.line for event in out_events] == list(out_lines)
    assert [event.line for event in err_events] == list(err_lines)
    assert [event.pid for event in out_events] == [out_pid] * len(out_lines)
    assert [event.pid for event in err_events] == [err_pid] * len(err_lines)


class TestPayloadPreservationProperties:
    """Every generated line event equals generic emission at the same clock."""

    @_EXAMPLE_UNICODE
    @_EXAMPLE_SINGLE
    @given(case=_emission_case())
    def test_line_events_match_the_generic_emission_oracle(
        self,
        case: _EmissionCase,
    ) -> None:
        """Every declared field agrees with the oracle for each generated line.

        The oracle is driven from the *same* observation and the *same* pinned
        clock reading, so the execution ID, tags, argv, and timestamp are shared
        by construction and only the line-specific fields can differ.
        """
        clock = _SetClock()
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(case.argv), clock, captured)
        callback = _emitter(observation, stream=case.stream, pid=case.pid)

        for line, stamp in zip(case.lines, case.timestamps, strict=True):
            clock.value = stamp
            callback(line)
            clock.value = stamp
            observation.emit(case.stream, _EventDetails(pid=case.pid, line=line))

        # The anti-vacuity witness: the loop ran for every generated line, so
        # the pairing below is a real pairing and not an empty range.
        assert len(captured) == 2 * len(case.lines), (
            "each line must contribute one line event and one oracle event"
        )
        _assert_oracle_parity(captured)

    @_EXAMPLE_BACKWARDS
    @given(case=_emission_case())
    def test_exactly_one_clock_read_per_delivered_line(
        self, case: _EmissionCase
    ) -> None:
        """The bound clock is invoked once per line, in delivery order."""
        clock = _SetClock()
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(case.argv), clock, captured)
        callback = _emitter(observation, stream=case.stream, pid=case.pid)

        # Preparation binds the clock but must not read it: a preparation-time
        # reading would make the whole stream share one timestamp.
        prepared_calls = clock.calls
        expected: list[float] = []
        for line, stamp in zip(case.lines, case.timestamps, strict=True):
            clock.value = stamp
            expected.append(clock.value)
            callback(line)

        assert len(captured) == len(case.lines), (
            "every generated line must reach the observe hook"
        )
        assert clock.calls - prepared_calls == len(case.lines), (
            "each delivered line must read the clock exactly once"
        )
        assert [event.timestamp for event in captured] == expected, (
            "each event must carry its own reading, in order"
        )

    @_EXAMPLE_UNICODE
    @given(case=_emission_case())
    def test_events_are_distinct_objects_with_stable_history(
        self,
        case: _EmissionCase,
    ) -> None:
        """Each line yields a fresh frozen object; earlier ones never change.

        A reused or mutated event would corrupt any payload a hook retained
        across an asynchronous yield, which is exactly what V4 exercises end to
        end. Each snapshot is taken as its line is delivered, so a later
        rewrite is caught rather than masked.
        """
        clock = _SetClock()
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(case.argv), clock, captured)
        callback = _emitter(observation, stream=case.stream, pid=case.pid)

        snapshots: list[dict[str, object]] = []
        for index, (line, stamp) in enumerate(
            zip(case.lines, case.timestamps, strict=True)
        ):
            clock.value = stamp
            callback(line)
            snapshots.append(_fields(captured[index]))

        assert len(captured) == len(case.lines) == len(snapshots), (
            "every generated line must be delivered and snapshotted"
        )
        assert len({id(event) for event in captured}) == len(case.lines), (
            "each line must produce a distinct object"
        )
        for index, snapshot in enumerate(snapshots):
            assert _fields(captured[index]) == snapshot, (
                f"event {index} changed after later lines were delivered"
            )
        with pytest.raises(dc.FrozenInstanceError):
            captured[0].line = "mutated"  # ty: ignore[invalid-assignment]


class TestBoundaryExamples:
    """Explicit boundary cases beside the generated ones.

    Most line, timestamp, and PID boundaries are stated as ``@example`` rows on
    the properties above. What remains here are the cases whose *whole* payload
    shape is the point, kept as literals so a failure names the boundary rather
    than reporting a shrunk strategy.
    """

    @pytest.mark.parametrize(
        ("line", "description"),
        [
            ("", "an empty line is still a line"),
            ("a" * 4096, "a long line is not truncated"),
            ("é中\U0001f600", "Unicode survives unchanged"),
            ("no trailing newline", "the callback receives text, not framed lines"),
            ("\r\n", "a CRLF-only line is preserved verbatim"),
        ],
    )
    def test_boundary_lines_round_trip(self, line: str, description: str) -> None:
        """Boundary line text reaches the event unchanged."""
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(), _SetClock(1.0), captured)
        callback = _emitter(observation, stream="stdout", pid=1)
        callback(line)

        assert captured[0].line == line, description

    @pytest.mark.parametrize("timestamp", [0.0, -0.0, 1e-300, 1e300, -1.0])
    def test_boundary_timestamps_are_carried_exactly(self, timestamp: float) -> None:
        """A clock reading is stored as-is, including zero and negatives."""
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(), _SetClock(timestamp), captured)
        callback = _emitter(observation, stream="stdout", pid=1)
        callback("line")

        assert captured[0].timestamp == timestamp
        assert math.copysign(1.0, captured[0].timestamp) == math.copysign(
            1.0, timestamp
        ), "a signed zero must keep its sign"

    @pytest.mark.parametrize("pid", [None, 0, 1, 2**31 - 1])
    def test_boundary_pids_are_carried_exactly(self, pid: int | None) -> None:
        """Absent, zero, and large PIDs are all preserved without coercion."""
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(), _SetClock(1.0), captured)
        callback = _emitter(observation, stream="stdout", pid=pid)
        callback("line")

        assert captured[0].pid == pid
        assert captured[0].pid is pid, "an absent PID must not become 0"

    @pytest.mark.parametrize(
        ("argv", "description"),
        [
            ((), "an empty argv is carried as the program alone"),
            (("a b", ""), "an empty argument is not dropped"),
            (("☃",), "Unicode arguments survive unchanged"),
        ],
    )
    def test_boundary_argv_round_trips(
        self, argv: tuple[str, ...], description: str
    ) -> None:
        """The observation's argv reaches the event unchanged."""
        captured: list[ExecEvent] = []
        observation = _observation(_NullCmd(argv), _SetClock(1.0), captured)
        callback = _emitter(observation, stream="stdout", pid=1)
        callback("line")

        assert captured[0].argv == ("prop-program", *argv), description


class TestInterleavedEmitters:
    """Two emitters over one observation keep their own stream metadata.

    A single per-stream preparation step must not let one stream's bound phase
    or PID leak into the other's events, which is what a process-wide cache
    would do.
    """

    @_EXAMPLE_TWO_STREAMS
    @given(case=_interleaved_case())
    def test_two_streams_keep_distinct_phases_and_pids(
        self,
        case: tuple[tuple[str, ...], tuple[str, ...], int | None, int | None],
    ) -> None:
        """Interleaved stdout and stderr emitters never cross their metadata."""
        out_lines, err_lines, out_pid, err_pid = case
        captured: list[ExecEvent] = []
        token = new_exec_id()
        observation = _observation(_NullCmd(), _SetClock(5.0), captured, exec_id=token)
        stdout = _emitter(observation, stream="stdout", pid=out_pid)
        stderr = _emitter(observation, stream="stderr", pid=err_pid)

        for index in range(max(len(out_lines), len(err_lines))):
            if index < len(out_lines):
                stdout(out_lines[index])
            if index < len(err_lines):
                stderr(err_lines[index])

        _assert_streams_kept_apart(captured, out_lines, err_lines, out_pid, err_pid)
        assert {event.exec_id for event in captured} == {token}, (
            "one observation shares one execution token across both streams"
        )
