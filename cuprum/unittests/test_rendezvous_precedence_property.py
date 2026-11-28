"""The exit rendezvous selects the same failure whatever settled first.

The rendezvous reports one failure when several of its monitored slots have
failed, and the choice must not depend on which task the event loop happened to
settle first: a run that reports stdout's error on one attempt and stderr's on
the next names neither defect reliably. ``cuprum._subprocess_rendezvous`` fixes
the order — a settled exit, then the stdin writer, then the consumers in
stdout-then-stderr order — and these properties pin it over every combination
of slot states, including the writer's published failure while its task is
still unwinding.

Settlement *order* is what the properties vary. Each slot settles after its own
number of loop steps, so the tasks finish in every relative order while the set
of settled outcomes stays fixed. That separates the rule from the schedule: a
selection driven by completion order would still pass a table of named cases
built in a fixed sequence, and would fail here.

Four invariants are checked:

- The pure selection, the full race and the stated precedence agree on which
  failure the run reports, and identity distinguishes it from a lookalike.
- A settled exit outranks every failed slot, whichever slot failed.
- A run whose writer is quiet and whose consumers succeed, are absent or are
  still running selects nothing and returns the exit's own result. A cancelled
  consumer is not quiet: it is a stream-labelled failure.
- Every case leaves no task behind: each task it created is retrieved.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import enum
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._stream_consumer_errors import ConsumerCancelledError
from cuprum._subprocess_rendezvous import (
    _await_exit_or_writer_failure,
    _first_early_failure,
    _is_early_failure,
    _StdinWriterLog,
)
from cuprum.unittests._rendezvous_support import CallbackError, ExitProbe

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _State(enum.Enum):
    """How one monitored slot behaves in a generated case."""

    ABSENT = "absent"
    SUCCEEDS = "succeeds"
    CANCELLED = "cancelled"
    FAILS = "fails"
    RUNNING = "running"
    PUBLISHED = "published"


#: Where a slot settles, in loop steps. The value is what varies completion
#: order, so it must span more than one step to be meaningful.
_STEPS = st.integers(min_value=0, max_value=3)

#: The slots a consumer can be. ``PUBLISHED`` is the writer's alone: it models a
#: writer whose failure is recorded before its task starts unwinding.
_CONSUMER_STATES = (
    _State.ABSENT,
    _State.SUCCEEDS,
    _State.CANCELLED,
    _State.FAILS,
    _State.RUNNING,
)
_WRITER_STATES = (*_CONSUMER_STATES, _State.PUBLISHED)

#: The states that contribute no failure, for the no-failure property. A writer
#: may fail or publish, so those two are excluded here rather than reused. A
#: cancelled *writer* is quiet teardown, while a cancelled *consumer* is a
#: labelled early failure, which is why the two slot kinds have separate quiet
#: sets.
_WRITER_QUIET_STATES = (
    _State.ABSENT,
    _State.SUCCEEDS,
    _State.CANCELLED,
    _State.RUNNING,
)
_CONSUMER_QUIET_STATES = (
    _State.ABSENT,
    _State.SUCCEEDS,
    _State.RUNNING,
)

_SlotSpec = tuple[_State, int]
_SlotsSpec = tuple[_SlotSpec, _SlotSpec, _SlotSpec]

#: The slots in precedence order, and the sentinel index each one owns.
_RANKS = (0, 1, 2)

#: Enough examples to cover the state combinations with room for orderings,
#: without turning a per-example event loop into a slow suite.
_SETTINGS = settings(max_examples=50)


@dc.dataclass(slots=True)
class _Slots:
    """The tasks one case races, and the sentinels its slots would raise."""

    stdin_task: asyncio.Task[object] | None
    consumers: tuple[asyncio.Task[object] | None, ...]
    stdin_log: _StdinWriterLog | None
    sentinels: tuple[CallbackError, CallbackError, CallbackError]

    def tasks(self) -> tuple[asyncio.Task[object], ...]:
        """Return every task this case created, absent slots omitted.

        Returns
        -------
        tuple[asyncio.Task[object], ...]
            The tasks that exist, in slot order.
        """
        return tuple(
            task for task in (self.stdin_task, *self.consumers) if task is not None
        )


async def _settle_after(ticks: int, sentinel: CallbackError | None) -> object:
    """Fail with *sentinel*, or succeed, after *ticks* loop steps.

    Returns
    -------
    object
        A stand-in result, which only a slot that was given no sentinel reaches.
    """
    for _ in range(ticks):
        await asyncio.sleep(0)
    if sentinel is not None:
        raise sentinel
    return object()


async def _build_slot(
    state: _State,
    ticks: int,
    sentinel: CallbackError,
) -> asyncio.Task[object] | None:
    """Build the task one slot races with, in the state the case gave it.

    ``PUBLISHED`` builds a task that *succeeds*, which is what a writer still
    unwinding looks like to anything that only asks its task: the failure it
    has already published is not on the task yet. It must still settle, because
    that settlement is what wakes the race to read the publication.

    Returns
    -------
    asyncio.Task[object] | None
        The slot's task, or ``None`` for a stream cuprum holds no pipe for.
    """
    if state is _State.ABSENT:
        return None
    if state is _State.RUNNING:
        # PARKED: this one never settles, so the case must cancel and retrieve it.
        return asyncio.create_task(asyncio.Event().wait())
    task = asyncio.create_task(
        _settle_after(ticks, sentinel if state is _State.FAILS else None),
    )
    if state is _State.CANCELLED:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return task


async def _let_settle(specs: cabc.Sequence[_SlotSpec]) -> None:
    """Give every slot enough loop steps to reach the state it was given."""
    for _ in range(max(ticks for _, ticks in specs) + 2):
        await asyncio.sleep(0)


async def _build_slots(writer: _SlotSpec, consumers: tuple[_SlotSpec, ...]) -> _Slots:
    """Build the slots of one case, with every settleable slot already settled.

    Settling before the race is what keeps the property deterministic. Each
    slot still takes its own number of loop steps, so the tasks finish in
    different relative orders; what is fixed is the set of outcomes the race
    then reads, which is exactly the input the selection rule is defined over.

    Returns
    -------
    _Slots
        The tasks, the writer's publication, and each slot's sentinel.
    """
    sentinels = typ.cast(
        "tuple[CallbackError, CallbackError, CallbackError]",
        tuple(CallbackError(f"slot {rank} failed") for rank in _RANKS),
    )
    specs = (writer, *consumers)
    built = [
        await _build_slot(state, ticks, sentinels[rank])
        for rank, (state, ticks) in enumerate(specs)
    ]
    await _let_settle(specs)
    stdin_task, *consumer_tasks = built
    return _Slots(
        stdin_task=stdin_task,
        consumers=tuple(consumer_tasks),
        stdin_log=_published(writer, sentinels[0]),
        sentinels=sentinels,
    )


def _published(writer: _SlotSpec, sentinel: CallbackError) -> _StdinWriterLog | None:
    """Return the writer's publication, present only for the unwinding state.

    Returns
    -------
    _StdinWriterLog | None
        The log carrying *sentinel*, or ``None`` for a writer that publishes
        nothing.
    """
    return _StdinWriterLog(error=sentinel) if writer[0] is _State.PUBLISHED else None


@dc.dataclass(frozen=True, slots=True)
class _ExpectedFailure:
    """What the fixed precedence must select for one generated case.

    A failing writer or consumer contributes its own sentinel, compared by
    identity. A cancelled consumer contributes a synthesized
    ``ConsumerCancelledError`` instead, so it is compared by type and stream:
    the query builds a new object for every reading, and the requirement is
    that both readings name the same stream.
    """

    sentinel: CallbackError | None = None
    stream: str | None = None

    def matches(self, error: BaseException) -> bool:
        """Return whether *error* is the failure this case selected.

        Returns
        -------
        bool
            Whether *error* is the expected sentinel, or the expected
            stream-labelled cancellation.
        """
        if self.sentinel is not None:
            return error is self.sentinel
        return isinstance(error, ConsumerCancelledError) and error.stream == self.stream


def _expected_failure(
    slots: _Slots,
    specs: _SlotsSpec,
) -> _ExpectedFailure | None:
    """Name the failure the fixed precedence must select.

    The order is stated here rather than read off the implementation: a settled
    writer outranks stdout, which outranks stderr. A slot that is absent,
    succeeded, or is still running contributes nothing, and a cancelled writer
    is quiet teardown. A writer that has *published* its failure contributes it
    even while its task runs, because that publication is the window it exists
    for. A cancelled consumer contributes a stream-labelled
    ``ConsumerCancelledError``.

    Returns
    -------
    _ExpectedFailure | None
        The highest-precedence failure, or ``None`` when no slot contributes
        one.
    """
    streams = ("stdout", "stderr")
    for rank, (state, _ticks) in enumerate(specs):
        if state is _State.FAILS:
            return _ExpectedFailure(sentinel=slots.sentinels[rank])
        if rank == 0 and state is _State.PUBLISHED:
            return _ExpectedFailure(sentinel=slots.sentinels[0])
        if rank > 0 and state is _State.CANCELLED:
            return _ExpectedFailure(stream=streams[rank - 1])
    return None


async def _settle_every_task(slots: _Slots) -> None:
    """Cancel every task the case created and retrieve each outcome."""
    tasks = slots.tasks()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@st.composite
def _cases_with_a_failure(draw: st.DrawFn) -> _SlotsSpec:
    """Generate three slots with one named rank forced to fail.

    Forcing a failure is what keeps every case meaningful: the property is
    about *which* failure is reported, and a case with nothing failed would
    only restate the separate no-failure property.

    Returns
    -------
    _SlotsSpec
        The writer's slot and both consumers', in precedence order.
    """
    forced = draw(st.sampled_from(_RANKS))
    specs = []
    for rank in _RANKS:
        states = _WRITER_STATES if rank == 0 else _CONSUMER_STATES
        if rank == forced:
            state = draw(
                st.sampled_from([_State.FAILS, _State.PUBLISHED])
                if rank == 0
                else st.just(_State.FAILS),
            )
        else:
            state = draw(st.sampled_from(states))
        specs.append((state, draw(_STEPS)))
    return typ.cast("_SlotsSpec", tuple(specs))


@st.composite
def _cases_without_a_failure(draw: st.DrawFn) -> _SlotsSpec:
    """Generate three slots none of which fails.

    Consumer cancellation is excluded here because it is a failure now; the
    writer's cancel remains quiet and is drawn for the writer alone.

    Returns
    -------
    _SlotsSpec
        The writer's slot and both consumers', in precedence order.
    """
    writer = (draw(st.sampled_from(_WRITER_QUIET_STATES)), draw(_STEPS))
    consumers = [
        (draw(st.sampled_from(_CONSUMER_QUIET_STATES)), draw(_STEPS))
        for _ in _RANKS[1:]
    ]
    return typ.cast("_SlotsSpec", (writer, *consumers))


@given(specs=_cases_with_a_failure())
@_SETTINGS
def test_the_race_reports_the_highest_precedence_failure(specs: _SlotsSpec) -> None:
    """Selection, the race and the stated precedence agree, in every ordering.

    The pure selection and the full race are both checked against an order
    stated independently of either, so a rule applied in one place and not the
    other fails here rather than passing on one route. Identity is checked too:
    the race must raise the very object the selected slot carries, and a fresh
    exception with the same message must not be mistaken for it.
    """
    asyncio.run(_check_precedence(specs))


async def _check_precedence(specs: _SlotsSpec) -> None:
    """Race one generated case and assert every reading of the precedence."""
    writer, *consumers = specs
    slots = await _build_slots(writer, tuple(consumers))
    probe = ExitProbe()
    try:
        expected = _expected_failure(slots, specs)
        assert expected is not None, (
            f"a generated case with a forced failure must select one, for {specs!r}"
        )
        selected = _first_early_failure(
            slots.stdin_task,
            slots.consumers,
            stdin_log=slots.stdin_log,
        )
        assert selected is not None, (
            f"a generated case with a forced failure must select one, for {specs!r}"
        )
        assert expected.matches(selected), (
            "the pure selection must name the highest-precedence failed slot, "
            f"got {selected!r} rather than {expected!r} for {specs!r}"
        )
        with pytest.raises((CallbackError, ConsumerCancelledError)) as caught:
            await _await_exit_or_writer_failure(
                probe.wait(),
                slots.stdin_task,
                consumers=slots.consumers,
                stdin_log=slots.stdin_log,
            )
        assert expected.matches(caught.value), (
            "the race must raise the selected failure itself, got "
            f"{caught.value!r} rather than {expected!r} for {specs!r}"
        )
        assert probe.cancelled.is_set(), (
            "a reported failure must cancel the exit wait, which is what "
            f"terminates the child, for {specs!r}"
        )
        if expected.sentinel is None:
            # The synthesized cancellation error is not a task's own exception,
            # so the identity-based provenance predicate must not claim it. It
            # never reaches that predicate: it is not a ``TimeoutError``.
            assert not _is_early_failure(
                caught.value,
                slots.stdin_task,
                slots.consumers,
                stdin_log=slots.stdin_log,
            ), f"a synthesized cancellation is not a task's own error, for {specs!r}"
        else:
            assert _is_early_failure(
                expected.sentinel,
                slots.stdin_task,
                slots.consumers,
                stdin_log=slots.stdin_log,
            ), f"the selected failure must be recognized as monitored, for {specs!r}"
            lookalike = CallbackError(str(expected.sentinel))
            assert not _is_early_failure(
                lookalike,
                slots.stdin_task,
                slots.consumers,
                stdin_log=slots.stdin_log,
            ), (
                "an equal-looking exception from elsewhere must not be taken for "
                f"a monitored failure, for {specs!r}"
            )
    finally:
        await _settle_every_task(slots)


@given(specs=_cases_with_a_failure())
@_SETTINGS
def test_a_settled_exit_outranks_every_failed_slot(specs: _SlotsSpec) -> None:
    """An exit that settled first is the run's outcome, whatever also failed.

    The race reports the exit's result and leaves the failed tasks to the
    caller's post-exit await, so no cancellation is forwarded to the exit wait
    and no failure is raised from the race itself.
    """
    asyncio.run(_check_settled_exit_wins(specs))


async def _check_settled_exit_wins(specs: _SlotsSpec) -> None:
    """Race a settled exit against one generated set of slots."""
    writer, *consumers = specs
    slots = await _build_slots(writer, tuple(consumers))
    probe = ExitProbe(exit_code=5)
    probe.release.set()
    try:
        result = await _await_exit_or_writer_failure(
            probe.wait(),
            slots.stdin_task,
            consumers=slots.consumers,
            stdin_log=slots.stdin_log,
        )
        assert result == (5, probe.exited_at), (
            f"a settled exit must be the run's result, got {result!r} for {specs!r}"
        )
        assert not probe.cancelled.is_set(), (
            "a settled exit must not be cancelled by a sibling's failure, for "
            f"{specs!r}"
        )
    finally:
        await _settle_every_task(slots)


@given(specs=_cases_without_a_failure())
@_SETTINGS
def test_a_run_with_nothing_failed_returns_the_exit(specs: _SlotsSpec) -> None:
    """Slots that settle without failing leave the run on the exit's result.

    This is the other half of the rule the precedence covers: finishing is the
    ordinary way a stream ends, so the race must re-arm and wait for the child
    rather than treat the first settlement as an outcome. The exit settles
    after a few loop steps, so the race is exercised through that re-arm.
    """
    writer, *consumers = specs
    assert asyncio.run(_nothing_failed(writer, tuple(consumers))) == (5, 1.5), (
        "a run whose slots all settled without failing must return the exit's "
        f"result, for {specs!r}"
    )


async def _nothing_failed(
    writer: _SlotSpec,
    consumers: tuple[_SlotSpec, ...],
) -> tuple[int, float]:
    """Race a multi-step exit against slots that never fail.

    Returns
    -------
    tuple[int, float]
        The exit's result, and nothing else.
    """
    slots = await _build_slots(writer, consumers)
    probe = ExitProbe(exit_code=5, ticks=3)
    try:
        selected = _first_early_failure(
            slots.stdin_task,
            slots.consumers,
            stdin_log=slots.stdin_log,
        )
        assert selected is None, (
            "no slot failed, so nothing may be selected, got "
            f"{selected!r} for {consumers!r}"
        )
        return await _await_exit_or_writer_failure(
            probe.wait(),
            slots.stdin_task,
            consumers=slots.consumers,
            stdin_log=slots.stdin_log,
        )
    finally:
        await _settle_every_task(slots)
