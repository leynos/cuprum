"""Contract: no workflow reads sccache statistics without the fallback guard.

With no server, `sccache --show-stats` prints empty default statistics instead
of starting one, so a report that runs after a fallback publishes a table of
zeros for a job that never used the cache. The guard on the setup step's
`status` output keeps it out. It was first applied by step name, which missed a
differently named report in `loom-smoke`; scanning for the command itself
leaves no step to hide behind a name.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_leg_gate import normalized
from tests.helpers.ci_runners import NOT_FALLEN_BACK, steps, workflow_sources
from tests.helpers.ci_workflows import workflow_document

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step


def _statistics_steps() -> list[tuple[str, Step]]:
    """Return every workflow step that runs `--show-stats`, with its location.

    Jobs that call a reusable workflow declare no steps and are skipped.

    Returns
    -------
    list[tuple[str, Step]]
        Each step with a ``workflow:job:step`` label for failure messages.
    """
    job_ids = [
        (workflow_name, job_name)
        for workflow_name, _ in workflow_sources()
        for job_name, job in typ.cast(
            "dict[str, dict[str, object]]",
            workflow_document(workflow_name).get("jobs", {}),
        ).items()
        if "steps" in job
    ]
    return [
        (f"{workflow_name}:{job_name}:{step.get('name')}", step)
        for workflow_name, job_name in job_ids
        for step in steps(workflow_name, job_name)
        if "--show-stats" in str(step.get("run", ""))
    ]


def test_no_workflow_step_reads_statistics_without_the_fallback_guard() -> None:
    """Find every statistics step by what it runs, not by what it is called.

    The guard was first applied by step name, which missed a differently named
    report in `loom-smoke`. Scanning for the command itself leaves no step to
    hide behind a name.
    """
    found = _statistics_steps()
    assert found, "no workflow step reads sccache statistics; the scan is empty"
    unguarded = [
        label
        for label, step in found
        if NOT_FALLEN_BACK not in normalized(step.get("if"))
    ]
    assert not unguarded, (
        f"these steps read sccache statistics without {NOT_FALLEN_BACK!r}, so "
        f"they would publish empty statistics for an uncached job: {unguarded}"
    )
