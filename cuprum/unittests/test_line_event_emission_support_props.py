"""Generators and helpers shared by the per-line emission property tests.

Roadmap item 5.2.1 hoists invariant execution metadata out of the per-line
callback. The relation under test is: *substituting captured stable fields
leaves each event equal to the old constructor at the same clock value*. It is
checked by comparing every declared ``ExecEvent`` field against the generic
:meth:`_StageObservation.emit` oracle rather than against a handwritten
expectation, so the oracle and the implementation cannot drift together. The
clock is pinned to the same value for both calls, so the comparison is exact
rather than merely plausible.

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
import typing as typ

from hypothesis import example
from hypothesis import strategies as st

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum.unittests.test_line_event_emission_support import (
    _fields,
    _NullCmd,
    _NullProgram,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecId
    from cuprum.lines import LineStreamName


class _PropCmd(_NullCmd):
    """Command stand-in whose sentinels are distinct from the shared ones.

    The property tests assert against the emitted strings, so their stand-in
    must not be interchangeable with ``_NullCmd``'s: substituting one for the
    other would change what these tests assert without failing them.
    """

    program = _NullProgram("prop-program")
    _project_name = "prop-project"
    _default_argv: tuple[str, ...] = ()


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
_PHASES = st.sampled_from(typ.cast("tuple[LineStreamName, ...]", ("stdout", "stderr")))
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


@dc.dataclass(frozen=True, slots=True)
class _InterleavedCase:
    """One generated two-stream interleaving.

    Attributes
    ----------
    out_lines:
        The line texts to deliver to the stdout emitter, in order.
    err_lines:
        The line texts to deliver to the stderr emitter, in order.
    out_pid:
        The process identifier the stdout emitter binds, or ``None``.
    err_pid:
        The process identifier the stderr emitter binds, or ``None``.

    """

    out_lines: tuple[str, ...]
    err_lines: tuple[str, ...]
    out_pid: int | None
    err_pid: int | None


@st.composite
def _interleaved_case(draw: st.DrawFn) -> _InterleavedCase:
    """Draw one non-empty interleaved two-stream case.

    Returns
    -------
    _InterleavedCase
        The stdout lines, stderr lines, stdout PID, and stderr PID.
    """
    line_lists = st.lists(_LINE, min_size=1, max_size=8).map(tuple)
    return _InterleavedCase(
        out_lines=draw(line_lists),
        err_lines=draw(line_lists),
        out_pid=draw(_PIDS),
        err_pid=draw(_PIDS),
    )


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
_EXAMPLE_TWO_STREAMS = example(
    case=_InterleavedCase(
        out_lines=("only",),
        err_lines=("only",),
        out_pid=0,
        err_pid=2**31 - 1,
    ),
)


def _observation(
    cmd: _PropCmd,
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
    case: _InterleavedCase,
) -> None:
    """Assert neither stream's lines or PID crossed into the other's events."""
    assert len(captured) == len(case.out_lines) + len(case.err_lines), (
        "every generated line must reach exactly one emitter"
    )
    out_events = [event for event in captured if event.phase == "stdout"]
    err_events = [event for event in captured if event.phase == "stderr"]
    assert [event.line for event in out_events] == list(case.out_lines)
    assert [event.line for event in err_events] == list(case.err_lines)
    assert [event.pid for event in out_events] == [case.out_pid] * len(case.out_lines)
    assert [event.pid for event in err_events] == [case.err_pid] * len(case.err_lines)
