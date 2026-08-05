"""That a real pipeline run actually publishes the fail-fast event.

`test_pipeline_wait_fail_fast_event.py` drives the wait path directly, with
stage observations it builds itself. That proves the emission rule but says
nothing about whether a pipeline run supplies those observations at all: with
the wiring in `cuprum._pipeline_internals` removed, every one of those tests
still passes and no user ever sees an event. This module closes that gap by
running three real subprocesses and reading what the hooks received.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent

_READ_STDIN = "import sys; sys.stdout.write(sys.stdin.read())"


def _run_failing_pipeline() -> tuple[ExecEvent, ...]:
    """Run a three-stage pipeline whose first stage fails, capturing events."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    events: list[ExecEvent] = []

    pipeline = (
        python("-c", "import sys; sys.exit(3)")
        | python("-c", _READ_STDIN)
        | python("-c", _READ_STDIN)
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
    ):
        pipeline.run_sync(output=RunOutputOptions(capture=True, echo=False))

    return tuple(events)


@pytest.fixture(scope="module")
def fail_fast_events() -> tuple[ExecEvent, ...]:
    """Publish one run of the failing pipeline to every test in this module.

    The run costs three real subprocesses, and repeating it per test buys
    nothing: `scoped` is entered and left inside the helper, so no state
    survives it, and each test only reads the events it returned. The tuple is
    what keeps that sharing safe — no test can leave the next one a shortened
    or reordered sequence.
    """
    return _run_failing_pipeline()


def _phase(events: cabc.Sequence[ExecEvent], phase: str) -> list[ExecEvent]:
    """Return the events of one phase, in the order they were published."""
    return [event for event in events if event.phase == phase]


def _stage_of(event: ExecEvent) -> int:
    """Return the pipeline stage an event belongs to, from its tags."""
    return typ.cast("int", event.tags["pipeline_stage_index"])


def test_a_real_pipeline_publishes_one_fail_fast_event(
    fail_fast_events: tuple[ExecEvent, ...],
) -> None:
    """A failing non-final stage produces exactly one event, describing itself.

    The first stage exits 3 while two downstream stages are still running, so
    the run takes the fail-fast path. Whichever order the three completions
    land in, stage 0 is the earliest failing stage and is not the final one,
    so the decision is the same every time.
    """
    fail_fast = _phase(fail_fast_events, "pipeline_fail_fast")

    assert len(fail_fast) == 1, (
        f"a real fail-fast pipeline must publish exactly one event, "
        f"found {len(fail_fast)}"
    )
    (event,) = fail_fast
    assert (event.stage_index, event.stage_count, event.exit_code) == (0, 3, 3), (
        "the event must describe stage 0 of a three-stage pipeline exiting 3, "
        f"found {event!r}"
    )
    assert event.duration_s is not None, "the event must report an elapsed time"
    assert event.duration_s >= 0.0, (
        f"the elapsed time must not be negative, found {event.duration_s!r}"
    )


def test_the_event_joins_the_failing_stage_span(
    fail_fast_events: tuple[ExecEvent, ...],
) -> None:
    """Its ``exec_id`` is the token stage 0's own lifecycle events carry.

    This is the wiring the module exists to check: the token has to come from
    the failing stage's observation, so a tracing adapter finds the span that
    stage's ``start`` opened. A token from a different stage, or a fresh one,
    would leave the event correlated to nothing.
    """
    (event,) = _phase(fail_fast_events, "pipeline_fail_fast")
    tokens_by_stage = {
        _stage_of(start): start.exec_id for start in _phase(fail_fast_events, "start")
    }

    assert tokens_by_stage[0] == event.exec_id, (
        f"the event must carry stage 0's token {tokens_by_stage[0]!r}, "
        f"found {event.exec_id!r}"
    )
    others = {token for stage, token in tokens_by_stage.items() if stage != 0}
    assert event.exec_id not in others, "the event must not carry another stage's token"


def test_the_existing_lifecycle_events_are_unchanged(
    fail_fast_events: tuple[ExecEvent, ...],
) -> None:
    """Every stage still reports plan, start, and exit, in that order.

    The fail-fast event is additive. It must not displace, reorder, or suppress
    a stage's own lifecycle events — including those of the stages that the
    teardown stops, which still have to report their exit.
    """
    by_stage: dict[int, list[str]] = {}
    for event in fail_fast_events:
        if event.phase == "pipeline_fail_fast":
            continue
        by_stage.setdefault(_stage_of(event), []).append(event.phase)

    assert sorted(by_stage) == [0, 1, 2], (
        f"every stage must still report its own events, found {sorted(by_stage)}"
    )
    for stage, phases in by_stage.items():
        assert phases[0] == "plan", (
            f"stage {stage} must still begin with plan, found {phases!r}"
        )
        assert phases[1] == "start", (
            f"stage {stage} must still start before anything else, found {phases!r}"
        )
        assert phases[-1] == "exit", (
            f"stage {stage} must still end with exit, found {phases!r}"
        )
