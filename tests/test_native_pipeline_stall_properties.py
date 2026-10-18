"""Properties of the stall classifier over generated snapshots.

The example tests in :mod:`tests.test_native_pipeline_hand_off_support` name
the shapes a reviewer thought of. These generate the space those shapes come
from, and assert the two invariants that only a generator can reach:

- a hand-off rule that holds for one child holds for the whole snapshot,
  whatever unrelated children are added beside it;
- a child's verdict contribution depends on the evidence, never on the order
  the evidence happens to be captured in.

Both are preservation properties: they say a positive verdict cannot be
*destroyed* by evidence that does not bear on it. That is the direction that
matters here, because every ``HUNG_HANDOFF`` rule is a positive claim and the
starvation fallback is what a missing claim falls back to. The converse —
starvation is reported only when no rule matched — is the classifier's own
control flow, and is pinned by the examples instead.

Only the classifier is exercised. The liveness policy's numeric invariants are
restatements of its own ``max``/``min`` expressions, so a property test over
them would assert the implementation against itself; the example tests name the
boundaries that carry meaning.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.behaviour._native_pipeline_hand_off import _StallSnapshot
from tests.behaviour._native_pipeline_stall import (
    HandOffVerdict,
    _ChildStallSnapshot,
    _classify_stall,
    _exited_children_left_output,
    _ParentOutput,
)
from tests.helpers.process_state import ChildPipe, ProcessState

#: The verdicts a single child can contribute, as the rule that produces them.

#: Scheduler states a live child can report, plus the two that mean no live
#: child at all: an exited record and an unobservable one.
_LIVE_STATES = st.sampled_from(["R", "D", "S", "T", "Z"])
_WCHAN = st.sampled_from([None, "pipe_read", "pipe_write", "do_wait", "do_exit"])
_STAGE = st.integers(min_value=0, max_value=6)
_PENDING = st.one_of(st.none(), st.integers(min_value=0, max_value=64))

#: Constant strategies for the two booleans a process record carries. Each
#: polarity is named, so a generated record reads as the branch it stands for
#: rather than as an anonymous ``st.just(False)`` at the call site.
_HAS_EXITED = st.sampled_from([True])
_HAS_NOT_EXITED = st.sampled_from([False])
_OBSERVABLE = st.sampled_from([True])
_UNOBSERVABLE = st.sampled_from([False])


def _process(pid: int) -> st.SearchStrategy[ProcessState]:
    """Generate a process record with any observable combination of fields.

    Returns
    -------
    SearchStrategy[ProcessState]
        A record drawn from the live, zombie, and unobservable branches.
    """
    return st.one_of(
        # A live child: a state, a wait channel, and procfs available.
        st.builds(
            ProcessState,
            pid=st.just(pid),
            state=_LIVE_STATES,
            exited=_HAS_NOT_EXITED,
            wchan=_WCHAN,
            available=_OBSERVABLE,
        ),
        # An exited child, still observable as a zombie.
        st.builds(
            ProcessState,
            pid=st.just(pid),
            state=st.none(),
            exited=_HAS_EXITED,
            wchan=st.just(None),
            available=_OBSERVABLE,
        ),
        # A host that cannot answer for this pid at all.
        st.builds(
            ProcessState,
            pid=st.just(pid),
            state=st.none(),
            exited=_HAS_NOT_EXITED,
            wchan=st.just(None),
            available=_UNOBSERVABLE,
        ),
    )


def _pipes() -> st.SearchStrategy[tuple[ChildPipe, ...]]:
    """Generate a child's descriptor set: none, or one pipe of either kind."""
    return st.one_of(
        st.just(()),
        st.builds(
            ChildPipe,
            fd=st.integers(min_value=0, max_value=2),
            target=st.sampled_from(["pipe:[42]", "pipe:[43]"]),
            pending_bytes=_PENDING,
        ).map(lambda pipe: (pipe,)),
    )


def _stdout_parent() -> st.SearchStrategy[_ParentOutput | None]:
    """Generate a parent read end, present or absent, with any byte count."""
    return st.one_of(
        st.none(),
        st.builds(
            _ParentOutput,
            fd=st.just(9),
            target=st.just("pipe:[43]"),
            pending_bytes=_PENDING,
        ),
    )


def _child(index: int) -> st.SearchStrategy[_ChildStallSnapshot]:
    """Generate a child snapshot in any state the classifier can read.

    Every field the classifier consults is generated independently, including
    the ones that would be inconsistent in a real capture: the properties under
    test must hold for any snapshot the classifier accepts, not only for the
    ones a live pipeline can produce.

    Returns
    -------
    SearchStrategy[_ChildStallSnapshot]
        The generated snapshot.
    """
    return st.builds(
        _ChildStallSnapshot,
        process=_process(1000 + index),
        pipes=_pipes(),
        stdin_parent_writers=st.one_of(
            st.just(()),
            st.tuples(st.integers(min_value=0, max_value=64)),
        ),
        stage_index=st.one_of(st.none(), _STAGE),
        stdout_parent=_stdout_parent(),
    )


def _unexited_child(index: int) -> st.SearchStrategy[_ChildStallSnapshot]:
    """Generate a child whose record does not report it as exited.

    Both branches are genuinely not-exited: one is a live process with a
    scheduler state, the other an unobservable record. ``_child`` generates a
    third branch — an exited record — which is why it cannot stand in for
    "live" in a test whose whole subject is the exited/not-exited distinction.

    Returns
    -------
    SearchStrategy[_ChildStallSnapshot]
        A snapshot whose process record reports ``exited=False``.
    """
    return st.builds(
        _ChildStallSnapshot,
        process=st.one_of(
            st.builds(
                ProcessState,
                pid=st.just(1000 + index),
                state=_LIVE_STATES,
                exited=_HAS_NOT_EXITED,
                wchan=_WCHAN,
                available=_OBSERVABLE,
            ),
            st.builds(
                ProcessState,
                pid=st.just(1000 + index),
                state=st.none(),
                exited=_HAS_NOT_EXITED,
                wchan=st.just(None),
                available=_UNOBSERVABLE,
            ),
        ),
        pipes=_pipes(),
        stdin_parent_writers=st.one_of(
            st.just(()),
            st.tuples(st.integers(min_value=0, max_value=64)),
        ),
        stage_index=st.one_of(st.none(), _STAGE),
        stdout_parent=_stdout_parent(),
    )


def _exited_with_output() -> st.SearchStrategy[_ChildStallSnapshot]:
    """Generate an exited child whose parent read end holds unread bytes.

    Returns
    -------
    SearchStrategy[_ChildStallSnapshot]
        A snapshot that is uncollected output by construction.
    """
    return st.builds(
        _ChildStallSnapshot,
        process=st.builds(
            ProcessState,
            pid=st.integers(min_value=1000, max_value=1999),
            state=st.none(),
            exited=_HAS_EXITED,
            wchan=st.just(None),
            available=_OBSERVABLE,
        ),
        pipes=st.just(()),
        stdin_parent_writers=st.just(()),
        stage_index=st.one_of(st.none(), _STAGE),
        stdout_parent=st.builds(
            _ParentOutput,
            fd=st.just(9),
            target=st.just("pipe:[43]"),
            pending_bytes=st.integers(min_value=1, max_value=64),
        ),
    )


def _snapshot(
    children: list[_ChildStallSnapshot],
    task_names: tuple[str, ...],
) -> _StallSnapshot:
    """Build a stall snapshot from generated children and task names."""
    return _StallSnapshot(tuple(children), task_names, "generated stall")


@settings(max_examples=75, deadline=None)
@given(extra=st.lists(_child(10), max_size=4))
def test_a_parent_writer_verdict_survives_unrelated_children(
    extra: list[_ChildStallSnapshot],
) -> None:
    """Adding children that witness nothing cannot destroy a missed hand-off.

    The parent-writer rule consults only the edge a child reads from: the
    blocked child's own writers and wait channel, and whether the stage
    immediately upstream of it has exited. None of those can be removed by
    adding a child elsewhere, so the verdict must survive any addition. This is
    the property the classifier's docstring claims when it says an unrelated
    runnable stage must not hide a genuine hand-off failure.

    The core pair is fixed rather than generated, because a generated one is
    usually not a missed hand-off at all, and a preservation property whose
    premise is usually false asserts very little.
    """
    core = _blocked_and_exited(1)

    assert _classify_stall(_snapshot(core, ())) is HandOffVerdict.HUNG_HANDOFF, (
        "the fixed pair must be a missed hand-off before it is disturbed"
    )

    widened = _classify_stall(_snapshot([*core, *extra], ()))

    assert widened is HandOffVerdict.HUNG_HANDOFF, (
        "a missed hand-off must survive bystanders that witness nothing, got "
        f"{widened!r} for extras of {[c.process for c in extra]!r}"
    )


@settings(max_examples=75, deadline=None)
@given(
    child=_child(0),
    siblings=st.lists(_child(1), max_size=4),
    task_names=st.one_of(st.just(()), st.just(("t:wait",))),
)
def test_reordering_children_never_changes_the_verdict(
    child: _ChildStallSnapshot,
    siblings: list[_ChildStallSnapshot],
    task_names: tuple[str, ...],
) -> None:
    """The verdict is a property of the evidence, not of its order.

    Stage adjacency is what makes the rules meaningful, so a rewrite that
    compared children positionally rather than by stage would still pass every
    example test and would change its answer whenever the capture happened to
    order the same evidence differently.
    """
    forward = _snapshot([child, *siblings], task_names)
    reversed_ = _snapshot([*siblings, child], task_names)

    assert _classify_stall(forward) is _classify_stall(reversed_), (
        "the same evidence must classify the same way however it is ordered, "
        f"got {_classify_stall(forward)!r} and {_classify_stall(reversed_)!r}"
    )


@settings(max_examples=75, deadline=None)
@given(
    children=st.lists(_child(0), min_size=1, max_size=5),
    task_names=st.one_of(st.just(()), st.just(("t:wait",))),
)
def test_every_snapshot_classifies_without_raising(
    children: list[_ChildStallSnapshot],
    task_names: tuple[str, ...],
) -> None:
    """No combination of readable evidence makes the classifier blow up.

    The classifier runs while a pipeline is already stalled, so an exception
    here would replace the diagnostic with a traceback at exactly the moment
    it is needed. This is the weakest property in the module and it is kept
    because it is the only one that ranges over every field at once.
    """
    verdict = _classify_stall(_snapshot(children, task_names))

    assert verdict in {HandOffVerdict.HUNG_HANDOFF, HandOffVerdict.HOST_STARVATION}, (
        f"the classifier must return a verdict, got {verdict!r}"
    )


@settings(max_examples=75, deadline=None)
@given(
    exited=st.lists(_exited_with_output(), min_size=1, max_size=3),
    live=_unexited_child(10),
)
def test_uncollected_output_requires_every_child_to_have_exited(
    exited: list[_ChildStallSnapshot],
    live: _ChildStallSnapshot,
) -> None:
    """A live child disables the uncollected-output rule, whatever it is doing.

    The rule reads "every tracked child has exited, and some parent read end
    still holds bytes", and the universal half is load-bearing: a child that is
    still running may be about to drain the very bytes the rule would call
    uncollected, so the count is not yet evidence of a missed close. Hypothesis
    found this while this module was asserting the opposite — that a hand-off
    verdict survives any added child — which is why the property is pinned here
    in the direction that actually holds.

    A ``S`` or ``T`` child is included among the generated ones deliberately:
    not runnable is not the same as exited, and only exited disables the rule.

    The subject is the rule, not the composite verdict. A child that has not
    exited may still be a missed hand-off in its own right — a live reader
    blocked in ``pipe_read`` under a parent-held write end whose upstream stage
    has exited is exactly that — and ``_classify_stall`` tests that rule before
    this one. Asserting ``HOST_STARVATION`` here would fold two independent
    rules together and fail whenever the live child generated evidence for the
    other one, which is a false alarm about a correct classifier.
    """
    assert _classify_stall(_snapshot(exited, ())) is HandOffVerdict.HUNG_HANDOFF, (
        "the generated exited children must be uncollected output"
    )

    widened = _snapshot([*exited, live], ())

    assert _exited_children_left_output(widened) is False, (
        "a child that has not exited must defeat the uncollected-output rule, "
        f"got a match for a live child {live.process!r}"
    )


def _blocked_and_exited(stage: int) -> list[_ChildStallSnapshot]:
    """Build the two children a missed hand-off needs at ``stage``."""
    return [
        _ChildStallSnapshot(
            ProcessState(42, "S", exited=False, wchan="pipe_read", available=True),
            (),
            (10,),
            stage,
            None,
        ),
        _ChildStallSnapshot(
            ProcessState(43, None, exited=True, wchan=None, available=True),
            (),
            (),
            stage - 1,
            None,
        ),
    ]


def test_the_properties_are_not_vacuous() -> None:
    """The generators can actually reach a HUNG_HANDOFF snapshot.

    A preservation property over a generator that never produces the evidence
    it claims to preserve would pass forever while asserting nothing, so the
    reachable case is pinned here rather than assumed. Stages start at one so
    that a stage-zero child has no upstream to consult.
    """
    reached = [
        _classify_stall(_snapshot(_blocked_and_exited(stage), ()))
        for stage in range(1, 5)
    ]

    assert HandOffVerdict.HUNG_HANDOFF in reached, (
        f"the fixture must be able to produce a missed hand-off, got {reached!r}"
    )
