"""When the fail-fast decision reaches the observe hooks, and what it says.

The fail-fast log records are covered in `test_pipeline_wait_observability.py`;
this module covers the other channel the same decision is published on — a
single ``pipeline_fail_fast`` :class:`~cuprum.events.ExecEvent`. Its value to a
consumer rests on three things, so each is pinned here: that it fires exactly
once and only for the completion that actually triggers a teardown, that it
carries the failing stage's own ``exec_id`` so it can be joined to that stage's
span, and that it is published before termination is requested.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

import pytest

from cuprum import _pipeline_wait
from cuprum.unittests._pipeline_wait_support import (
    CompletionPlan,
    apply_completions,
    drive_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecEvent

_FAIL_FAST_PHASE = "pipeline_fail_fast"


@dc.dataclass(frozen=True, slots=True)
class _Driven:
    """One driven completion sequence and everything it published."""

    events: list[ExecEvent]
    observations: tuple[_StageObservation, ...]
    records: list[logging.LogRecord]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    stage_count: int,
    completions: list[tuple[int, int]],
) -> _Driven:
    """Drive completions through the async boundary with a collecting hook."""
    events: list[ExecEvent] = []
    observations = make_stage_observations(stage_count, (events.append,))
    records = drive_completions(
        monkeypatch,
        caplog,
        CompletionPlan(
            stage_count=stage_count,
            completions=completions,
            observations=observations,
        ),
    )
    return _Driven(events=events, observations=observations, records=records)


def _fail_fast_events(driven: _Driven) -> list[ExecEvent]:
    """Return only the fail-fast events from a driven sequence."""
    return [event for event in driven.events if event.phase == _FAIL_FAST_PHASE]


class TestFailFastEventEmission:
    """Which completions publish the decision, and which stay silent."""

    def test_a_non_final_first_failure_publishes_one_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The one completion that triggers a teardown publishes exactly once.

        Both halves matter. Publishing nothing would leave metrics and tracing
        integrations unable to see fail-fast at all, and publishing more than
        once would make the counter it feeds report pipelines that never
        happened.
        """
        driven = _drive(
            monkeypatch,
            caplog,
            stage_count=3,
            completions=[(0, 4)],
        )

        assert [event.phase for event in driven.events] == [_FAIL_FAST_PHASE], (
            f"expected exactly one fail-fast event, found {driven.events!r}"
        )

    def test_the_event_carries_the_decision(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Stage position, pipeline width, exit code, and elapsed time travel with it.

        Stage index alone cannot be read without the width — stage 1 of 2 is
        the final stage and stage 1 of 4 is not — and without the exit code a
        consumer cannot tell an ordinary failure from a signal.
        """
        driven = _drive(
            monkeypatch,
            caplog,
            stage_count=4,
            completions=[(1, 7)],
        )

        (event,) = _fail_fast_events(driven)

        assert (
            event.stage_index,
            event.stage_count,
            event.exit_code,
            event.duration_s,
        ) == (1, 4, 7, 12.5), (
            "the event must report the failing stage's position, the pipeline "
            f"width, its exit code, and its elapsed time, found {event!r}"
        )

    def test_the_event_reuses_the_failing_stage_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The token is the failing stage's own, not a fresh or neighbouring one.

        This is the whole correlation story: a tracing adapter finds the span
        to annotate by ``exec_id``, so a token minted for the occasion — or
        taken from a fixed position such as stage zero — would attach the
        decision to nothing, or to the wrong stage. The log records are checked
        against the same token, which is what proves the two channels agree.
        """
        driven = _drive(
            monkeypatch,
            caplog,
            stage_count=3,
            completions=[(1, 3)],
        )

        (event,) = _fail_fast_events(driven)
        expected = driven.observations[1].exec_id

        assert event.exec_id == expected, (
            f"the event must carry stage 1's own token {expected!r}, "
            f"found {event.exec_id!r}"
        )
        logged = {
            str(vars(record)["cuprum_exec_id"])
            for record in driven.records
            if "cuprum_exec_id" in vars(record)
        }
        assert logged == {str(expected)}, (
            f"the log records must carry the same token, found {logged!r}"
        )

    @pytest.mark.parametrize(
        ("stage_count", "completions", "reason"),
        [
            pytest.param(
                3,
                [(1, 0)],
                "a stage that succeeded triggers no teardown to report",
                id="successful_completion",
            ),
            pytest.param(
                3,
                [(2, 1)],
                "a failing final stage has nothing left to terminate",
                id="final_stage_failure",
            ),
            pytest.param(
                1,
                [(0, 9)],
                "a single-stage pipeline has no other stage to terminate",
                id="single_stage_failure",
            ),
        ],
    )
    def test_completions_that_publish_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stage_count: int,
        completions: list[tuple[int, int]],
        reason: str,
    ) -> None:
        """No event where there is no fail-fast teardown to announce."""
        driven = _drive(
            monkeypatch,
            caplog,
            stage_count=stage_count,
            completions=completions,
        )

        assert driven.events == [], f"{reason}, found {driven.events!r}"

    def test_a_later_failure_does_not_publish_a_second_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only the latched failure is announced, however many stages then fail.

        In a pipeline the upstream failure is what causes the downstream ones,
        so every stage exiting non-zero after the latch is a consequence of the
        teardown already under way, not a new incident.
        """
        driven = _drive(
            monkeypatch,
            caplog,
            stage_count=4,
            completions=[(0, 1), (1, 1), (2, 1)],
        )

        indices = [event.stage_index for event in _fail_fast_events(driven)]

        assert indices == [0], (
            f"only the latched stage 0 may be announced, found {indices!r}"
        )


class TestFailFastEventOrdering:
    """The event is published before the teardown it announces begins."""

    def test_the_event_precedes_the_termination_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A teardown that never returns must not swallow the announcement.

        Termination awaits every other stage, so a stage that ignores its
        signal blocks the fail-fast path for the whole cancel grace. Publishing
        first is what lets a consumer see the decision during that window
        rather than only after it.
        """
        order: list[str] = []
        pin_clock(monkeypatch, 12.5)

        async def noting_terminate(
            processes: object,
            wait_tasks: object,
            failure_index: int,
            *,
            cancel_grace: float,
        ) -> int:
            """Record that termination started instead of signalling anything."""
            del processes, wait_tasks, failure_index, cancel_grace
            # Yield like the real helper does, so the recorded order reflects
            # a genuine await point rather than a synchronous call.
            await asyncio.sleep(0)
            order.append("terminate")
            return 0

        monkeypatch.setattr(
            _pipeline_wait,
            "_terminate_pipeline_remaining_stages",
            noting_terminate,
        )

        def note_event(event: ExecEvent) -> None:
            """Record the arrival of the fail-fast event in sequence."""
            if event.phase == _FAIL_FAST_PHASE:
                order.append("event")

        observations = make_stage_observations(3, (note_event,))
        state = make_wait_state(3, observations=observations)

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            apply_completions(state, [(0, 4)])

        assert order == ["event", "terminate"], (
            f"the event must be published before termination starts, found {order!r}"
        )


class TestFailFastEventWithoutHooks:
    """A wait state with no observation context still fails fast."""

    def test_no_observations_still_terminates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Publishing is additive: with nothing to publish to, nothing changes.

        The transition-level tests and the symbolic model build states without
        observations, so the absence must degrade to silence rather than an
        attribute error taken on the fail-fast path.
        """
        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            records = drive_completions(
                monkeypatch,
                caplog,
                CompletionPlan(stage_count=3, completions=[(0, 4)]),
            )

        actions = [
            vars(record)["cuprum_action"]
            for record in records
            if "cuprum_action" in vars(record)
        ]

        assert "pipeline_fail_fast_termination" in actions, (
            f"the teardown must still be reported and requested, found {actions!r}"
        )


class _HookFailureError(RuntimeError):
    """Raised by a hook to prove emission failures are not swallowed."""


class TestFailFastHookFailure:
    """A hook that raises surfaces rather than being silently dropped."""

    def test_a_raising_hook_propagates_before_the_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Emission is fail-closed, and fails before termination is requested.

        Cuprum re-raises observe-hook failures rather than swallowing them, so
        a hook that cannot handle the new phase fails the run visibly. This is
        the exposure a third-party hook with a fail-closed phase match takes
        on, and it is pinned rather than left implicit. That no termination was
        requested shows the ordering from the other side: were the event
        published after the teardown began, the request would already have been
        recorded. Nothing leaks, because callers of ``_wait_for_pipeline`` tear
        the remaining stages down on the way out.
        """
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        def raising_hook(event: ExecEvent) -> None:
            """Fail on the fail-fast event, ignoring every other phase."""
            if event.phase == _FAIL_FAST_PHASE:
                raise _HookFailureError

        observations = make_stage_observations(3, (raising_hook,))
        state = make_wait_state(3, observations=observations)

        with (
            pytest.raises(_HookFailureError),
            caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__),
        ):
            apply_completions(state, [(0, 4)])

        assert terminations == [], (
            f"termination must not have been requested, found {terminations!r}"
        )
