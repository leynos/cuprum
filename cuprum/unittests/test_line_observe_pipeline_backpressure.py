"""Shared-capacity retention across concurrent pipeline producers.

Both stages of the pipeline flood their own stderr, so the two stage
observations dispatch line events at the same time. The run-wide retention
list must bound their combined unfinished tasks, and both stages must retain
into the same list object rather than one list per stage. A per-stage bound
would let two producers hold twice the ceiling between them, which is exactly
what the shared list exists to prevent.
"""

from __future__ import annotations

import asyncio

import pytest

from cuprum._observe_task_retention import _OBSERVE_TASK_CEILING
from cuprum.unittests.test_line_observe_support import (
    _chatty_pipeline,
    _RetentionProbe,
    _run_observed,
    _SlowHook,
)

_CAPTURE_MODES = [
    pytest.param(True, id="captured"),
    pytest.param(False, id="uncaptured"),
]


@pytest.mark.parametrize("capture", _CAPTURE_MODES)
def test_pipeline_stages_share_one_bounded_retention_list(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Two concurrent producers are bounded by one run-wide list."""
    asyncio.run(_pipeline_case(monkeypatch, capture))


async def _pipeline_case(
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """Run a two-producer pipeline and check the shared bound."""
    per_stage = 1_500
    pipeline, catalogue = _chatty_pipeline(per_stage)
    hook = _SlowHook()
    probe = _RetentionProbe()
    probe.install(monkeypatch)

    await _run_observed(catalogue, hook, pipeline.run, capture=capture)

    first = [line for line in hook.seen if line.startswith("a-")]
    second = [line for line in hook.seen if line.startswith("b-")]
    assert first == [f"a-{index}" for index in range(per_stage)], (
        "the first stage's lines must arrive complete and in order"
    )
    assert second == [f"b-{index}" for index in range(per_stage)], (
        "the second stage's lines must arrive complete and in order"
    )
    assert len(hook.seen) == 2 * per_stage, "both stages must deliver every line"
    assert len(probe.list_ids) == 1, (
        "both stages must retain into one run-wide list; "
        f"observed {len(probe.list_ids)} lists"
    )
    assert probe.high_water <= _OBSERVE_TASK_CEILING, (
        f"retained tasks reached {probe.high_water}, above the shared ceiling "
        f"{_OBSERVE_TASK_CEILING}"
    )
    assert probe.high_water < (2 * per_stage) // 4, (
        "the shared list must stay far below the combined line count; "
        f"got {probe.high_water} for {2 * per_stage} lines"
    )
