"""Payload parity between the line callback and generic emission.

Roadmap item 5.2.1 hoists invariant execution metadata out of the per-line
callback so a line no longer rebuilds it. These tests pin the payload the line
callback produces against the generic :meth:`_StageObservation.emit` oracle.

They pass before *and* after the hoist: payload characterization alone is the
contract the hoist must not break, not the red test. The red assertions live in
the preparation-cost module beside this one.

The comparison reads ``dataclasses.fields`` values directly rather than
``dataclasses.asdict``: the latter recurses into read-only mappings and can
fail on values it cannot copy.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
import uuid

import pytest

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._pipeline_types import _EventDetails
from cuprum.echo_events import EchoStream
from cuprum.events import ExecEvent, ExecId
from cuprum.unittests.test_line_event_emission_support import (
    _fields,
    _make_observation,
)

if typ.TYPE_CHECKING:
    from cuprum.lines import LineStreamName


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

        assert not captured, "preparation must not emit an event"

    def test_echo_stream_values_match_the_phase_literals(self) -> None:
        """The echo-stream members and the phase literals agree by value."""
        assert EchoStream.STDOUT == "stdout"
        assert EchoStream.STDERR == "stderr"
