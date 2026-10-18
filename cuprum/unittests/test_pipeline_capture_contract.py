"""Tests for capture guarantees at pipeline stage boundaries."""

from __future__ import annotations

import asyncio

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import RunOutputOptions
from cuprum.unittests.test_pipeline_output_options import _identity_pipeline


@pytest.mark.parametrize("run_sync", [False, True], ids=["run()", "run_sync()"])
def test_captured_pipeline_final_stage_can_be_narrowed(
    run_sync: bool,
) -> None:
    """Only the captured final stage has both text streams available."""
    pipeline, allowed_programs = _identity_pipeline()

    with scoped(ScopeConfig(allowlist=allowed_programs)):
        output = RunOutputOptions(capture=True)
        if run_sync:
            result = pipeline.run_sync(output=output)
        else:
            result = asyncio.run(pipeline.run(output=output))

    captured_final = result.final.require_captured()
    assert isinstance(captured_final, sh.CapturedResult), (
        "the final stage should narrow to CapturedResult"
    )
    assert captured_final.stdout == "echoed", (
        "the final-stage capture must preserve pipeline output"
    )
    assert result.stages[0].stdout is None, (
        "an interior stage's stdout is consumed by the next stage"
    )
    with pytest.raises(ValueError, match="stdout"):
        result.stages[0].require_captured()
