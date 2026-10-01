"""Property tests for per-line event payload preservation.

Roadmap item 5.2.1 hoists invariant execution metadata out of the per-line
callback. The named characterization tests in ``test_line_event_emission`` pin
the payload for a handful of chosen cases; the properties here sample the
domain instead, so a hoist that quietly widens or narrows the field set, reuses
an event object, or drops a clock read cannot pass on luck.

Four invariants are asserted for every generated case:

* payload identity — line events equal the generic emission for the same state;
* one clock call per delivered line, in order;
* distinct object identities, so a later line cannot rewrite an earlier event;
* earlier payloads are unchanged after later deliveries.

Field order, constructor defaults, and ``repr`` are pinned by the named tests
instead: they are properties of the class, not of the hoist, and generating
inputs cannot say anything more about them.

The generators, the pinned clock, and the oracle-parity helpers live in
``test_line_event_emission_support_props``; the assertions live here.
"""

from __future__ import annotations

import dataclasses as dc
import math

import pytest
from hypothesis import given

from cuprum._pipeline_types import _EventDetails
from cuprum.events import ExecEvent, new_exec_id
from cuprum.unittests.test_line_event_emission_support import _fields
from cuprum.unittests.test_line_event_emission_support_props import (
    _EXAMPLE_BACKWARDS,
    _EXAMPLE_SINGLE,
    _EXAMPLE_TWO_STREAMS,
    _EXAMPLE_UNICODE,
    _assert_oracle_parity,
    _assert_streams_kept_apart,
    _emission_case,
    _EmissionCase,
    _emitter,
    _interleaved_case,
    _InterleavedCase,
    _observation,
    _PropCmd,
    _SetClock,
)


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
        observation = _observation(_PropCmd(case.argv), clock, captured)
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
        observation = _observation(_PropCmd(case.argv), clock, captured)
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
        observation = _observation(_PropCmd(case.argv), clock, captured)
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
        observation = _observation(_PropCmd(), _SetClock(1.0), captured)
        callback = _emitter(observation, stream="stdout", pid=1)
        callback(line)

        assert captured[0].line == line, description

    @pytest.mark.parametrize("timestamp", [0.0, -0.0, 1e-300, 1e300, -1.0])
    def test_boundary_timestamps_are_carried_exactly(self, timestamp: float) -> None:
        """A clock reading is stored as-is, including zero and negatives."""
        captured: list[ExecEvent] = []
        observation = _observation(_PropCmd(), _SetClock(timestamp), captured)
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
        observation = _observation(_PropCmd(), _SetClock(1.0), captured)
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
        observation = _observation(_PropCmd(argv), _SetClock(1.0), captured)
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
        case: _InterleavedCase,
    ) -> None:
        """Interleaved stdout and stderr emitters never cross their metadata."""
        captured: list[ExecEvent] = []
        token = new_exec_id()
        observation = _observation(_PropCmd(), _SetClock(5.0), captured, exec_id=token)
        stdout = _emitter(observation, stream="stdout", pid=case.out_pid)
        stderr = _emitter(observation, stream="stderr", pid=case.err_pid)

        for index in range(max(len(case.out_lines), len(case.err_lines))):
            if index < len(case.out_lines):
                stdout(case.out_lines[index])
            if index < len(case.err_lines):
                stderr(case.err_lines[index])

        _assert_streams_kept_apart(captured, case)
        assert {event.exec_id for event in captured} == {token}, (
            "one observation shares one execution token across both streams"
        )
