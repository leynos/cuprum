"""Property tests for the pipeline's terminal-outcome mapping.

``cuprum._pipeline_sink`` maps a finished pipeline's stage results onto the
bounded outcome set the presentation sinks speak. A pipeline has no exit code
of its own, so the contract is relational: the reported outcome and code must
come from the first stage that failed, whatever the stages around it did.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from cuprum import Program
from cuprum._pipeline_sink import _pipeline_result_outcome
from cuprum.sh import CommandResult
from cuprum.sinks import TerminalOutcome

_STAGE_RESULTS = st.builds(
    CommandResult,
    program=st.just(Program("stage")),
    argv=st.just(("stage",)),
    exit_code=st.integers(min_value=-1, max_value=255),
    pid=st.integers(min_value=-1, max_value=2**31 - 1),
    stdout=st.one_of(st.none(), st.text()),
    stderr=st.one_of(st.none(), st.text()),
)


@given(stages=st.lists(_STAGE_RESULTS, max_size=8))
def test_pipeline_result_outcome_reports_the_first_failing_stage(
    *,
    stages: list[CommandResult],
) -> None:
    """The outcome is zero exactly when no stage failed.

    The oracle is the first non-zero exit code in document order — the stage
    the pipeline stopped at — so a later failure must not displace it.
    """
    outcome = _pipeline_result_outcome(stages)
    first_failure = next(
        (stage.exit_code for stage in stages if stage.exit_code != 0),
        None,
    )

    if first_failure is None:
        assert outcome.outcome == TerminalOutcome.EXIT_ZERO
        assert outcome.exit_code == 0
    else:
        assert outcome.outcome == TerminalOutcome.EXIT_NONZERO
        assert outcome.exit_code == first_failure
