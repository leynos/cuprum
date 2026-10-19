"""Contract test that cache-writing jobs free disk before any cache step.

A Rust compiler-cache miss (every version bump causes one) grows the archive to
about 3 GiB, which does not fit beside the directory it packs on the 2 GB a
Linux job is left with. Pull-request runs never save, so only a push to `main`
shows the failure.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_runners import (
    CACHE_PLAIN,
    CACHE_RESTORE,
    CACHE_SAVE,
    CACHE_WRITERS,
    steps,
)

FREE_DISK_STEP = "Free preinstalled toolchains the job does not use"
FREED_DIRECTORIES = ("/usr/local/lib/android", "/usr/share/dotnet")
CACHE_USES = (CACHE_RESTORE, CACHE_SAVE, CACHE_PLAIN)


@pytest.mark.parametrize(
    ("workflow_name", "job_name"), sorted(CACHE_WRITERS["SCCACHE_CACHE_KEY"])
)
def test_compiler_cache_writers_free_disk_before_any_cache_step(
    workflow_name: str, job_name: str
) -> None:
    """Leave room for the archive: free unused toolchains before every cache."""
    job_steps = steps(workflow_name, job_name)
    freeing = [
        index
        for index, step in enumerate(job_steps)
        if step.get("name") == FREE_DISK_STEP
    ]
    where = f"{workflow_name}:{job_name}"
    assert len(freeing) == 1, f"{where} must declare {FREE_DISK_STEP!r} once"
    step = job_steps[freeing[0]]
    command = step.get("run")
    assert isinstance(command, str), f"{where} freeing step must run a command"
    missing = [path for path in FREED_DIRECTORIES if path not in command]
    assert not missing, f"{where} freeing step must remove {missing}"
    assert "runner.os == 'Linux'" in str(step.get("if", "")), (
        f"{where} freeing step must be guarded to Linux runners"
    )
    first_cache = min(
        index
        for index, other in enumerate(job_steps)
        if other.get("uses") in CACHE_USES
    )
    assert freeing[0] < first_cache, f"{where} must free disk before its first cache"
