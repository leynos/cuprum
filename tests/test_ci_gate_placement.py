"""Contracts for the placement of jobs that a paid lane waits on.

A job on the Ubicloud lane that `needs` a GitHub-hosted job inherits the
hosted queue. `changes` is a five-second paths filter, but `benchmark-ratchet`,
a required check, waits on it, and on 2026-09-23 it sat queued on
`ubuntu-latest` for 46 minutes (run 35904789287) while later runs' Ubicloud
jobs started. The rule is therefore structural rather than a list of names:
every job an Ubicloud job needs is itself on the Ubicloud manifest.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_runners import UBICLOUD_JOBS, expand, job

UBICLOUD_CASES = expand(UBICLOUD_JOBS)

#: Needs that cannot move, reviewed. `verify-wheel-install` installs every
#: native wheel, and the native legs build on macOS, Windows and two
#: `ubuntu-latest` legs whose check names are required contexts spelling the
#: label; moving them is the repository owner's decision, not a placement fix.
PLATFORM_NEEDS = frozenset({("build-wheels.yml", "build-native-wheels")})


def _needs(workflow_name: str, job_name: str) -> list[str]:
    """Return the jobs one job waits on, whatever form `needs` takes."""
    declared = job(workflow_name, job_name).get("needs")
    if declared is None:
        return []
    if isinstance(declared, str):
        return [declared]
    message = f"{workflow_name}:{job_name} declares an unreadable needs: {declared!r}"
    assert isinstance(declared, list), message
    names = [name for name in declared if isinstance(name, str)]
    assert len(names) == len(declared), message
    return names


@pytest.mark.parametrize(("workflow_name", "job_name"), UBICLOUD_CASES)
def test_no_ubicloud_job_waits_on_a_hosted_job(
    workflow_name: str, job_name: str
) -> None:
    """A paid lane must not queue behind the hosted lane."""
    hosted = [
        name
        for name in _needs(workflow_name, job_name)
        if (workflow_name, name) not in UBICLOUD_CASES
        and (workflow_name, name) not in PLATFORM_NEEDS
    ]
    assert not hosted, (
        f"{workflow_name}:{job_name} needs {hosted}, which run outside the "
        "Ubicloud manifest, so the paid job queues behind the hosted lane"
    )


def test_the_paths_filter_gates_a_ubicloud_job() -> None:
    """The presence half: the rule above must have a real edge to hold.

    Without it, dropping every `needs` would satisfy the rule vacuously.
    """
    assert "changes" in _needs("ci.yml", "benchmark-ratchet"), (
        "benchmark-ratchet must still wait on the paths filter"
    )
