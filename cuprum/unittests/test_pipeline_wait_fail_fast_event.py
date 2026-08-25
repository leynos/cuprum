"""When the fail-fast decision reaches the observe hooks, and what it says."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import types
import typing as typ
from pathlib import Path

import pytest

from cuprum import ECHO, _pipeline_wait, sh
from cuprum.unittests._pipeline_wait_support import (
    apply_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecEvent

_FAIL_FAST_PHASE = "pipeline_fail_fast"


@dc.dataclass(frozen=True, slots=True)
class _Driven:
    """One driven completion sequence and everything it published."""

    events: tuple[ExecEvent, ...]
    observations: tuple[_StageObservation, ...]
    terminations: tuple[tuple[int, float], ...]


@dc.dataclass(frozen=True, slots=True)
class _SilentCase:
    """One completion sequence that must announce nothing, and why."""

    stage_count: int
    completions: tuple[tuple[int, int], ...]
    reason: str


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage_count: int,
    completions: cabc.Sequence[tuple[int, int]],
) -> _Driven:
    """Drive completions through the async boundary with a collecting hook."""
    events: list[ExecEvent] = []
    observations = make_stage_observations(stage_count, (events.append,))
    terminations = record_terminations(monkeypatch)
    pin_clock(monkeypatch, 12.5)
    state = make_wait_state(stage_count, observations=observations)
    apply_completions(state, list(completions))
    # Snapshot both collectors: the result is shared with assertions that must
    # not be able to append to the sequence they are reading.
    return _Driven(
        events=tuple(events),
        observations=observations,
        terminations=tuple(terminations),
    )


def _fail_fast_events(driven: _Driven) -> tuple[ExecEvent, ...]:
    """Return only the fail-fast events from a driven sequence."""
    return tuple(event for event in driven.events if event.phase == _FAIL_FAST_PHASE)


class TestFailFastEventEmission:
    """Which completions publish the decision, and which stay silent."""

    def test_a_non_final_first_failure_publishes_one_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The one completion that triggers a teardown publishes exactly once.

        Both halves matter. Publishing nothing would leave metrics and tracing
        integrations unable to see fail-fast at all, and publishing more than
        once would make the counter it feeds report pipelines that never
        happened.
        """
        driven = _drive(
            monkeypatch,
            stage_count=3,
            completions=[(0, 4)],
        )

        assert [event.phase for event in driven.events] == [_FAIL_FAST_PHASE], (
            f"expected exactly one fail-fast event, found {driven.events!r}"
        )

    def test_the_event_carries_the_decision(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stage position, timing, pipeline width, and exit code travel with it.

        Stage index alone cannot be read without the width — stage 1 of 2 is
        the final stage and stage 1 of 4 is not — and without the exit code a
        consumer cannot tell an ordinary failure from a signal.
        """
        driven = _drive(
            monkeypatch,
            stage_count=4,
            completions=[(1, 7)],
        )

        (event,) = _fail_fast_events(driven)

        assert (
            event.stage_index,
            event.stage_count,
            event.exit_code,
            event.duration_s,
            event.timestamp,
            event.project,
        ) == (1, 4, 7, 12.5, 12.5, driven.observations[1].cmd.project.name), (
            "the event must report the failing stage's position, the pipeline "
            f"width, its exit code, timing, and trusted project, found {event!r}"
        )

    def test_the_event_reuses_the_failing_stage_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The token is the failing stage's own, not a fresh or neighbouring one.

        This is the whole correlation story: a tracing adapter finds the span
        to annotate by ``exec_id``, so a token minted for the occasion — or
        taken from a fixed position such as stage zero — would attach the
        decision to nothing, or to the wrong stage.
        """
        driven = _drive(
            monkeypatch,
            stage_count=3,
            completions=[(1, 3)],
        )

        (event,) = _fail_fast_events(driven)
        expected = driven.observations[1].exec_id

        assert event.exec_id == expected, (
            f"the event must carry stage 1's own token {expected!r}, "
            f"found {event.exec_id!r}"
        )

    def test_the_event_excludes_execution_secrets(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Decision telemetry cannot expose argv, environment, paths, or tags."""
        confidential_marker = "fail-fast-sensitive-value"
        events: list[ExecEvent] = []
        observations = make_stage_observations(
            3,
            (events.append,),
            tag_overrides={"token": confidential_marker},
        )
        observations = (
            dc.replace(
                observations[0],
                cmd=sh.make(ECHO)(f"--token={confidential_marker}"),
                cwd=Path(f"/private/{confidential_marker}"),
                env_overlay=types.MappingProxyType({"TOKEN": confidential_marker}),
            ),
            *observations[1:],
        )
        record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)
        state = make_wait_state(3, observations=observations)

        apply_completions(state, [(0, 4)])

        (event,) = _fail_fast_events(_Driven(tuple(events), observations, ()))
        for field, value, expected in (
            ("argv", event.argv, ()),
            ("cwd", event.cwd, None),
            ("env", event.env, None),
            ("tags", dict(event.tags), {}),
        ):
            assert value == expected, f"sanitized event leaked {field}: {event!r}"
        assert confidential_marker not in repr(event), (
            "the emitted fail-fast event must not retain execution secrets"
        )

    def test_a_backwards_clock_clamps_the_event_duration_to_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A completion earlier than its start cannot report a negative duration."""
        events: list[ExecEvent] = []
        observations = make_stage_observations(3, (events.append,))
        record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)
        state = make_wait_state(3, observations=observations)
        state.started_at[0] = 13.0

        apply_completions(state, [(0, 4)])

        (event,) = _fail_fast_events(_Driven(tuple(events), observations, ()))
        assert event.duration_s == 0.0, (
            f"backwards clock readings must clamp to zero, found {event.duration_s!r}"
        )

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _SilentCase(
                    stage_count=3,
                    completions=((1, 0),),
                    reason="a stage that succeeded triggers no teardown to report",
                ),
                id="successful_completion",
            ),
            pytest.param(
                _SilentCase(
                    stage_count=3,
                    completions=((2, 1),),
                    reason="a failing final stage has nothing left to terminate",
                ),
                id="final_stage_failure",
            ),
            pytest.param(
                _SilentCase(
                    stage_count=1,
                    completions=((0, 9),),
                    reason="a single-stage pipeline has no other stage to terminate",
                ),
                id="single_stage_failure",
            ),
        ],
    )
    def test_completions_that_publish_nothing(
        self,
        case: _SilentCase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No event where there is no fail-fast teardown to announce."""
        driven = _drive(
            monkeypatch,
            stage_count=case.stage_count,
            completions=case.completions,
        )

        assert driven.events == (), f"{case.reason}, found {driven.events!r}"

    def test_a_later_failure_does_not_publish_a_second_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the latched failure is announced, however many stages then fail.

        In a pipeline the upstream failure is what causes the downstream ones,
        so every stage exiting non-zero after the latch is a consequence of the
        teardown already under way, not a new incident.
        """
        driven = _drive(
            monkeypatch,
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
        ) -> tuple[bool, ...]:
            """Record that termination started instead of signalling anything."""
            del processes, wait_tasks, failure_index, cancel_grace
            # Yield like the real helper does, so the recorded order reflects
            # a genuine await point rather than a synchronous call.
            await asyncio.sleep(0)
            order.append("terminate")
            return ()

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

        apply_completions(state, [(0, 4)])

        assert order == ["event", "terminate"], (
            f"the event must be published before termination starts, found {order!r}"
        )


class TestFailFastEventWithoutHooks:
    """A wait state with no observation context still fails fast."""

    def test_no_observations_still_terminates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Publishing is additive: with nothing to publish to, nothing changes.

        The transition-level tests and the symbolic model build states without
        observations, so the absence must degrade to silence rather than an
        attribute error taken on the fail-fast path.
        """
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)
        state = make_wait_state(3)

        apply_completions(state, [(0, 4)])

        assert terminations == [(0, 0.25)], (
            f"the teardown must still be requested, found {terminations!r}"
        )


class _HookFailureError(RuntimeError):
    """Raised by a hook to prove emission failures are not swallowed."""


class TestFailFastHookFailure:
    """A hook that raises surfaces rather than being silently dropped."""

    def test_a_raising_hook_propagates_before_the_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
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

        with pytest.raises(_HookFailureError):
            apply_completions(state, [(0, 4)])

        assert terminations == [], (
            f"termination must not have been requested, found {terminations!r}"
        )
