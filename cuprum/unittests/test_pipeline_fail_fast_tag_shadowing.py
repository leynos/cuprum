"""That the fail-fast event reports the coordinator's stage, not the caller's.

``ExecutionContext.tags`` are merged *last* when a pipeline builds its per-stage
observations (``cuprum._pipeline_internals._build_pipeline_observations``), so a
caller is free to supply a ``pipeline_stage_index`` tag that overwrites the
coordinator's own. Shadowing is allowed — the tags are the caller's metadata
namespace — which is precisely why the fail-fast decision travels on the typed
``stage_index`` and ``stage_count`` fields of the event instead.

Every other module in this suite keeps tag and typed field in lock-step, so an
implementation that read the tag would pass all of them. These tests pull the
two apart: the typed fields carry the coordinator's reckoning while the
sanitized fail-fast event deliberately drops caller metadata.
"""

from __future__ import annotations

import types
import typing as typ

import pytest

from cuprum.sh import ExecutionContext
from cuprum.unittests._fail_fast_pipeline_support import phase, run_failing_pipeline
from cuprum.unittests._pipeline_wait_support import (
    apply_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

_FAIL_FAST_PHASE = "pipeline_fail_fast"

# Deliberately impossible values: no pipeline in these tests has a stage 99 or
# 99 stages, so a reported 99 can only have come from the caller's tags.
_SHADOWED_INDEX = 99
_SHADOWING_TAGS: typ.Final = types.MappingProxyType(
    {
        "pipeline_stage_index": _SHADOWED_INDEX,
        "pipeline_stages": _SHADOWED_INDEX,
    },
)


@pytest.fixture(scope="module")
def shadowed_fail_fast_event() -> ExecEvent:
    """Run the failing pipeline once with shadowing tags, returning its event.

    Module-scoped for the reason `test_pipeline_fail_fast_wiring` shares its
    own run: three real subprocesses and a settling delay are not worth paying
    twice for a frozen event that no test can mutate.
    """
    events = run_failing_pipeline(ExecutionContext(tags=_SHADOWING_TAGS))
    fail_fast = phase(events, _FAIL_FAST_PHASE)

    assert len(fail_fast) == 1, (
        f"the shadowed run must still reach fail-fast once, found {len(fail_fast)}"
    )
    return fail_fast[0]


def test_a_real_run_reports_the_coordinators_stage_not_the_tag(
    shadowed_fail_fast_event: ExecEvent,
) -> None:
    """The typed fields describe stage 0 of 3 however the tags are set.

    This is the contract the typed fields exist for. An implementation that
    read ``tags["pipeline_stage_index"]`` would report stage 99 of 99 here,
    telling a metrics or tracing consumer that a stage which never ran had
    failed — while every test that leaves the tags alone stayed green.
    """
    event = shadowed_fail_fast_event

    assert (event.stage_index, event.stage_count) == (0, 3), (
        "the event must report the stage the coordinator acted on, "
        f"found {(event.stage_index, event.stage_count)!r}"
    )


def test_the_fail_fast_event_drops_caller_tags(
    shadowed_fail_fast_event: ExecEvent,
) -> None:
    """Caller metadata cannot enter the decision event's hook payload."""
    assert shadowed_fail_fast_event.tags == {}, (
        "the sanitized fail-fast event must omit caller tags, found "
        f"{shadowed_fail_fast_event.tags!r}"
    )


def test_the_emission_seam_ignores_a_shadowing_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving one completion directly pins the same rule without subprocesses.

    The end-to-end run can only exercise the stage the pipeline happens to
    fail at. Here the failing stage is chosen — stage 1 of 4 — so a reported
    index that merely *looked* right at zero has nowhere to hide.
    """
    events: list[ExecEvent] = []
    observations = make_stage_observations(
        4,
        (events.append,),
        tag_overrides=_SHADOWING_TAGS,
    )

    record_terminations(monkeypatch)
    pin_clock(monkeypatch, 12.5)
    state = make_wait_state(4, observations=observations)
    apply_completions(state, [(1, 7)])

    (event,) = [item for item in events if item.phase == _FAIL_FAST_PHASE]

    assert (event.stage_index, event.stage_count) == (1, 4), (
        "the event must report the completed stage and the real pipeline width, "
        f"found {(event.stage_index, event.stage_count)!r}"
    )
    assert event.tags == {}, (
        f"the sanitized event must omit the shadowing tag, found {event.tags!r}"
    )
