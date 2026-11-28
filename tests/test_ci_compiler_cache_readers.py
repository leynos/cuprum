"""Join every compiler-cache restore to the job that publishes its family.

`tests/test_ci_cache_ownership.py` holds the writer side: one job publishes
each rendered family. `tests/test_ci_compiler_cache_backend.py` holds the lane
side: a job that binds a directory restores it. Neither can see the join. A
lane that restored a family no job publishes would satisfy both by restoring
*some* archive of the right shape, and would then compile cold on every run
with a green pipeline and a plausible-looking restore step.

That join is what this module asserts, over the same resolver the writer rule
uses, so the two halves cannot disagree about what a family *is*. It lives
beside those modules rather than inside one because the per-module line budget
in `AGENTS.md` is 400 lines and both are already close to it.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_cache_families import (
    CacheFamily,
    reader_families,
    writer_families,
)
from tests.helpers.ci_placement import placement
from tests.helpers.ci_runners import CACHED_JOBS, GITHUB_LABEL, expand

#: Every cached job that publishes its archives to the owned Ubicloud lane.
OWNED_CASES = tuple(
    (workflow_name, job_name)
    for workflow_name, job_name in expand(CACHED_JOBS)
    if placement(workflow_name, job_name).owned != GITHUB_LABEL
)


def _published_families() -> set[CacheFamily]:
    """Return every family the estate publishes, resolved from the saves."""
    return {
        family
        for workflow_name, job_name in expand(CACHED_JOBS)
        for family in writer_families(workflow_name, job_name)
    }


def test_the_owned_lane_has_cases() -> None:
    """Fail closed if the derived case list empties, which would assert nothing."""
    assert OWNED_CASES, (
        "no cached job resolves to the owned Ubicloud lane, so the join below "
        "would pass vacuously"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), OWNED_CASES)
def test_every_compiler_cache_restore_has_a_writer(
    workflow_name: str, job_name: str
) -> None:
    """Fail on a restore whose family no job publishes.

    The hosted lane is deliberately absent from the cases: a fork or
    Dependabot pull request renders ``github-hosted``, where no writer exists
    because every save is guarded on a push to ``refs/heads/main``. That arm
    is expected to compile cold.
    """
    published = _published_families()
    orphans = sorted(
        str(family)
        for family in reader_families(workflow_name, job_name)
        if family not in published
    )
    assert not orphans, (
        f"{workflow_name}:{job_name} restores compiler-cache families no job "
        f"publishes, so the cache is empty on every run: {orphans}"
    )
