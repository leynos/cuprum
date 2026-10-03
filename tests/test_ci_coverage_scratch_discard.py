"""Behavioural tests for the coverage jobs' scratch-discard step.

The ordering contract proves the step runs before every cache save. It does not
prove the step removes anything, and the first version of it did not: run
33857764655 printed an identical `72G 64G 8.3G 89%` either side while removing
nothing, because `cargo llvm-cov` builds beside the manifest it is given and
this repository's is `rust/Cargo.toml`, not the root. These tests run the
step's own shell against a tree shaped like the runner's.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import steps
from tests.helpers.composite_actions import run_step

if typ.TYPE_CHECKING:
    from pathlib import Path

DISCARD_STEP = "Discard the instrumented build tree"
COVERAGE_JOBS: typ.Final = (
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)
#: Where `cargo llvm-cov` actually puts its trees for this repository, given
#: `cargo-manifest: rust/Cargo.toml`.
INSTRUMENTED_TREES: typ.Final = ("rust/target/llvm-cov-target", "rust/target/llvm-cov")
#: A tree the step must leave alone. The ordinary build output is what the next
#: job's sccache archive was seeded from; only the instrumented tree is scratch.
PRESERVED_TREE = "rust/target/debug"


def _discard_script(workflow_name: str, job_name: str) -> str:
    """Return the discard step's shell body from one workflow job."""
    for step in steps(workflow_name, job_name):
        if step.get("name") == DISCARD_STEP:
            script = step.get("run")
            assert isinstance(script, str), (
                f"{workflow_name}:{job_name} discard step must run a script"
            )
            return script
    message = f"{workflow_name}:{job_name} declares no {DISCARD_STEP!r} step"
    raise AssertionError(message)


def _populate(root: Path, relative: str) -> Path:
    """Create a directory with one file in it, so `du` reports a size."""
    tree = root / relative
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "object.o").write_bytes(b"\0" * 4096)
    return tree


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_the_discard_step_removes_the_instrumented_trees(
    workflow_name: str, job_name: str, tmp_path: Path
) -> None:
    """Reclaim the tree where `cargo llvm-cov` actually writes it."""
    trees = [_populate(tmp_path, relative) for relative in INSTRUMENTED_TREES]
    preserved = _populate(tmp_path, PRESERVED_TREE)

    result = run_step(_discard_script(workflow_name, job_name), workdir=tmp_path)

    assert result.returncode == 0, result.stderr
    for tree in trees:
        assert not tree.exists(), (
            f"{workflow_name}:{job_name} left {tree.relative_to(tmp_path)} behind; "
            "the archive would carry it and the sampler would misreport the peak"
        )
    assert preserved.exists(), (
        f"{workflow_name}:{job_name} removed {PRESERVED_TREE}, which is not scratch"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_the_discard_step_reports_what_it_reclaimed(
    workflow_name: str, job_name: str, tmp_path: Path
) -> None:
    """Print the disk either side and name the tree, so the reclaim is evidence."""
    _populate(tmp_path, INSTRUMENTED_TREES[0])

    result = run_step(_discard_script(workflow_name, job_name), workdir=tmp_path)

    assert result.stdout.count("Filesystem") == 2, (
        f"{workflow_name}:{job_name} must print `df -h` either side of the "
        f"deletion; got:\n{result.stdout}"
    )
    assert INSTRUMENTED_TREES[0] in result.stdout, (
        f"{workflow_name}:{job_name} must name the tree it removed, so a run "
        f"that reclaimed nothing is visible; got:\n{result.stdout}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_the_discard_step_says_when_there_is_nothing_to_reclaim(
    workflow_name: str, job_name: str, tmp_path: Path
) -> None:
    """Keep a job that had no tree distinguishable from one that missed it.

    This is the whole failure the step already had once: silent success reads
    identically to a correct no-op.
    """
    _populate(tmp_path, PRESERVED_TREE)

    result = run_step(_discard_script(workflow_name, job_name), workdir=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "No instrumented build tree found to discard" in result.stdout, (
        f"{workflow_name}:{job_name} must say so explicitly; got:\n{result.stdout}"
    )
