"""Contract: no workflow reads sccache statistics without the fallback guard.

With no server, `sccache --show-stats` prints empty default statistics instead
of starting one, so a report that runs after a fallback publishes a table of
zeros for a job that never used the cache. The guard on the setup step's
`status` output keeps it out. It was first applied by step name, which missed a
differently named report in `loom-smoke`; scanning for the command itself
leaves no step to hide behind a name.

The guard must be one `&&`-separated term of a condition with no `||` in it.
GitHub's expression reference does not specify how `&&` and `||` mix, and a
disjunction leaves another arm that still runs against a dead server, so a
substring match would accept `guard || true`.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers import strict_yaml
from tests.helpers.ci_leg_gate import normalized
from tests.helpers.ci_runners import NOT_FALLEN_BACK, workflow_sources

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step


def has_fallback_conjunct(condition: str) -> bool:
    """Return whether the guard is a positive conjunction term of `condition`.

    Returns
    -------
    bool
        True when no `||` appears and one `&&`-separated term is the guard.

    Examples
    --------
    >>> has_fallback_conjunct("always() && steps.sccache.outputs.status != 'fallback'")
    True
    >>> has_fallback_conjunct("steps.sccache.outputs.status != 'fallback' || true")
    False
    """
    return "||" not in condition and NOT_FALLEN_BACK in {
        term.strip() for term in condition.split("&&")
    }


def _mapping(value: object, message: str) -> dict[str, object]:
    """Return `value` as a string-keyed mapping, failing the contract otherwise."""
    assert isinstance(value, dict), message
    return typ.cast("dict[str, object]", value)


def _statistics_steps() -> list[tuple[str, Step]]:
    """Return every workflow step that runs `--show-stats`, with its location.

    Each workflow is parsed once, from the text `workflow_sources` already read.
    Jobs that call a reusable workflow declare no steps and are skipped.

    Returns
    -------
    list[tuple[str, Step]]
        Each step with a ``workflow:job:step`` label for failure messages.
    """
    found: list[tuple[str, Step]] = []
    for workflow_name, source in workflow_sources():
        document = _mapping(
            strict_yaml.load(source, workflow_name),
            f"{workflow_name} must parse to a mapping",
        )
        jobs = _mapping(document.get("jobs", {}), f"{workflow_name} jobs must map")
        for job_name, job in jobs.items():
            declared = _mapping(job, f"{workflow_name}:{job_name} must map").get(
                "steps"
            )
            if not isinstance(declared, list):
                continue
            found.extend(
                (f"{workflow_name}:{job_name}:{step.get('name')}", step)
                for step in typ.cast("list[Step]", declared)
                if "--show-stats" in str(step.get("run", ""))
            )
    return found


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
        if not has_fallback_conjunct(normalized(step.get("if")))
    ]
    assert not unguarded, (
        f"these steps read sccache statistics without {NOT_FALLEN_BACK!r} as a "
        f"term of a conjunction, so they would publish empty statistics for an "
        f"uncached job: {unguarded}"
    )


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        pytest.param(NOT_FALLEN_BACK, True, id="the-guard-alone"),
        pytest.param(f"always() && {NOT_FALLEN_BACK}", True, id="always-and-guard"),
        pytest.param(
            f"always() && matrix.x && {NOT_FALLEN_BACK} && env.LEG_RUNS == 'true'",
            True,
            id="guard-among-other-terms",
        ),
        pytest.param("always()", False, id="omitted"),
        pytest.param("", False, id="no-condition"),
        pytest.param(f"{NOT_FALLEN_BACK} || true", False, id="guard-or-true"),
        pytest.param(f"always() || {NOT_FALLEN_BACK}", False, id="always-or-guard"),
        pytest.param(
            f"always() || matrix.x && {NOT_FALLEN_BACK}",
            False,
            id="a-disjunction-before-a-conjunction",
        ),
        pytest.param(
            "always() && steps.sccache.outputs.status == 'fallback'",
            False,
            id="inverted",
        ),
        pytest.param(f"!({NOT_FALLEN_BACK})", False, id="negated"),
    ],
)
def test_the_predicate_accepts_only_a_positive_conjunction_term(
    condition: str, *, expected: bool
) -> None:
    """The narrow half: an omitted, inverted, negated or `||` guard is refused."""
    assert has_fallback_conjunct(condition) is expected, (
        f"{condition!r} must be {'accepted' if expected else 'refused'}"
    )
