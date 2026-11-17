"""Keep formal boundary validation hosted, bounded, and free of target archives."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers.ci_workflows import (
    cache_paths,
    cache_steps,
    job,
    step_inputs,
    steps,
    workflow_document,
)
from tests.helpers.makefile import recipe_of, recipe_tokens

WORKFLOW = "rust-boundaries.yml"


def test_native_boundary_matrix_is_hosted_and_bounded() -> None:
    """Native resource effects need each supported kernel within the CI budget."""
    native = job(WORKFLOW, "native")
    assert native["runs-on"] == "${{ matrix.os }}", "native checks lost their OS matrix"
    strategy = native.get("strategy")
    assert isinstance(strategy, dict), "native strategy must be a mapping"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "native matrix must be a mapping"
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-2022", "macos-latest"}, (
        "native boundary validation must retain all supported hosted platforms"
    )
    assert strategy["max-parallel"] == 2, "native jobs exceeded the reviewed fan-out"
    assert native.get("timeout-minutes") == 20, "native validation lost its timeout"


def test_expensive_boundary_checks_are_scheduled_or_manual() -> None:
    """Kani/Miri and fault sensitivity must not add an unbounded PR workload."""
    extended = job(WORKFLOW, "extended")
    assert extended.get("if") == "github.event_name != 'pull_request'", (
        "heavy proofs entered every PR"
    )
    assert extended.get("timeout-minutes") == 45, (
        "extended verification lost its time limit"
    )
    strategy = extended.get("strategy")
    assert isinstance(strategy, dict), "extended strategy must be a mapping"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "extended matrix must be a mapping"
    assert strategy["max-parallel"] == 1, "heavy proof jobs must run serially"
    assert set(matrix["check"]) == {"kani", "miri"}, "a required verifier disappeared"


@pytest.mark.parametrize("name", ["native", "verus", "extended"])
def test_boundary_checkouts_do_not_persist_credentials(name: str) -> None:
    """Keep the checkout token out of .git/config where built code could read it."""
    checkouts = [
        step
        for step in steps(WORKFLOW, name)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, f"{name} must check the repository out for its boundary checks"
    for checkout in checkouts:
        inputs = step_inputs(
            checkout, f"{WORKFLOW}:{name} checkout must declare inputs"
        )
        assert inputs.get("persist-credentials") is False, (
            f"{name} must set persist-credentials: false"
        )


@pytest.mark.parametrize("name", ["native", "verus", "extended"])
def test_boundary_cache_ownership_is_limited(name: str) -> None:
    """Only scheduled main runs publish tool/object caches, never target trees."""
    for step in cache_steps(WORKFLOW, name):
        for path in cache_paths(step, f"{name} cache"):
            assert "target" not in PurePosixPath(path).parts, (
                "Cargo target archive bypassed sccache"
            )
    saves = [
        step
        for step in steps(WORKFLOW, name)
        if "actions/cache/save@" in str(step.get("uses", ""))
    ]
    assert saves, "the scheduled tool/compiler cache has no owner"
    for save in saves:
        condition = str(save.get("if", ""))
        assert "github.event_name == 'schedule'" in condition, (
            "a PR can publish the trusted cache"
        )
        assert "github.ref == 'refs/heads/main'" in condition, (
            "a branch can publish the trusted cache"
        )


def test_verus_archives_the_negative_compiler_evidence() -> None:
    """The boundary contract keeps its compiler logs under `.cache`, so archive them.

    `make boundary-contract` writes each attempt beneath
    `.cache/boundary-contract/runs`, not under the Cargo target directory the
    proof log uses. Without that path the job archives only the Verus output and
    the negative compiler evidence — the artefact a failure investigation needs
    most — is discarded with the runner.
    """
    archive = next(
        (
            step
            for step in steps(WORKFLOW, "verus")
            if step.get("name") == "Archive proof and negative compiler evidence"
        ),
        None,
    )

    assert isinstance(archive, dict), "the verus job must archive its evidence"
    inputs = step_inputs(archive, "the verus archive must declare its inputs")
    assert inputs.get("name") == (
        "verus-and-compiler-contracts-${{ github.run_id }}-${{ github.run_attempt }}"
    ), (
        "each attempt must upload under its own name; the uploader rejects a "
        "duplicate name within one run, which would replace the original failure"
    )
    paths = {
        line.strip()
        for line in str(inputs.get("path", "")).splitlines()
        if line.strip()
    }

    assert paths == {
        "rust/target/boundary-verification",
        ".cache/boundary-contract/runs",
    }, "the archive must carry the proof log and the boundary contract's run logs"
    assert inputs.get("include-hidden-files") is True, (
        "the uploader must include logs beneath the hidden .cache directory"
    )
    assert inputs.get("if-no-files-found") == "ignore", (
        "a failure before the harness creates a run must not mask its original error"
    )


def _logical_assignment(makefile: str, name: str) -> str:
    """Return the value of a Makefile assignment with continuations joined."""
    lines = makefile.splitlines()
    index = next(
        position for position, line in enumerate(lines) if line.startswith(f"{name} =")
    )
    value = lines[index].partition("=")[2]
    while value.rstrip().endswith("\\"):
        index += 1
        value = f"{value.rstrip()[:-1]} {lines[index].strip()}"
    return value


def test_boundary_kani_finds_the_installed_binaries() -> None:
    """The local target must run the Kani the shared action placed in `.cache`.

    `cargo kani` resolves `cargo-kani` on `PATH`. CI appends the cache's `bin`
    directory to `$GITHUB_PATH`, but a local `make boundary-kani` sees only the
    target's own environment, so the target must export that directory itself.
    """
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(
        encoding="utf-8"
    )
    cached_bin = _logical_assignment(makefile, "KANI_CACHED_BIN").strip()
    assert cached_bin == "$(CURDIR)/.cache/boundary-kani/bin", (
        "the cached frontend must be the one the shared install action fills"
    )
    assert "boundary-kani: export PATH := $(KANI_CACHED_BIN):$(PATH)" in makefile, (
        "make boundary-kani must resolve the Kani installed into .cache"
    )


#: Each job, its cache restore step, and the inputs its key must hash. Both
#: jobs archive the Verus install and its solver, so a new shared-installer
#: revision must invalidate both entries.
_TOOL_CACHE_INPUTS = [
    (
        "verus",
        "Restore pinned verifier, solver, and compiler objects",
        ["tools/verus/*", "tools/prover-tools/VERSION"],
    ),
    (
        "extended",
        "Restore proof tools and Miri sysroots",
        ["tools/kani/*", "tools/prover-tools/VERSION", "Makefile"],
    ),
]


def _restore_inputs(job_name: str, step_name: str) -> dict[str, object]:
    """Return the declared inputs of one job's cache restore step."""
    restore = next(
        step for step in steps(WORKFLOW, job_name) if step.get("name") == step_name
    )
    return step_inputs(restore, f"the {job_name} restore must declare its inputs")


@pytest.mark.parametrize(("job_name", "step_name", "expected"), _TOOL_CACHE_INPUTS)
def test_tool_caches_track_the_shared_installer_pin(
    job_name: str, step_name: str, expected: list[str]
) -> None:
    """A new shared-installer revision must invalidate every archived verifier.

    The retired `scripts/install_boundary_z3.py` used to name the Verus key. The
    pin now lives in `tools/prover-tools/VERSION`, so both jobs hash that file:
    otherwise the archived solver and verifier could outlive the revision that
    produced them. Hashing only the pin also restores a warm cache after an
    unrelated Makefile edit, which the extended job keeps as an input.
    """
    key = str(_restore_inputs(job_name, step_name).get("key", ""))
    assert re.findall(r"'([^']*)'", key) == expected, (
        f"the {job_name} cache key must hash only the inputs that pick the binaries"
    )


def test_the_verus_restore_prefix_hashes_the_same_inputs() -> None:
    """The verus restore prefix must reject the same revisions as its key."""
    job_name, step_name, expected = _TOOL_CACHE_INPUTS[0]
    prefix = "boundary-verus-${{ hashFiles(" + ", ".join(
        f"'{path}'" for path in expected
    )
    inputs = _restore_inputs(job_name, step_name)
    assert str(inputs.get("restore-keys", "")).startswith(prefix), (
        "the restore prefix must hash the same inputs as the key"
    )


def test_boundary_workflows_do_not_reference_the_retired_installers() -> None:
    """Kani and Z3 downloads must stay delegated to the shared dependency."""
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows" / WORKFLOW
    source = workflow.read_text(encoding="utf-8")
    assert "install_boundary_kani" not in source, (
        "the workflow must not call the retired Kani installer"
    )
    assert "install_boundary_z3" not in source, (
        "the workflow must not call the retired Z3 installer"
    )


def test_the_download_integration_target_supplies_its_opt_in() -> None:
    """The opt-in suite needs its switch."""
    tokens = recipe_tokens(recipe_of("boundary-download-integration"))

    assert tokens == (
        "CUPRUM_RUN_DOWNLOAD_INTEGRATION=1",
        "$(PYTEST)",
        "-v",
        "scripts/tests/test_boundary_pypy_https.py",
    ), "the target must switch the suite on and name its module"


def test_the_verus_job_runs_the_bounded_download_contracts() -> None:
    """The controlled HTTPS scenarios run where the pinned revision is installed."""
    commands = [str(step.get("run", "")) for step in steps(WORKFLOW, "verus")]

    assert any(
        "make boundary-download-integration" in command for command in commands
    ), "the verus job must exercise the bounded download contracts"


def test_the_boundary_workflow_watches_the_download_harness() -> None:
    """Editing a harness module must re-run the boundary workflow."""
    triggers = workflow_document(WORKFLOW)[True]
    assert isinstance(triggers, dict), "the boundary workflow must declare triggers"
    pull_request = triggers["pull_request"]
    assert isinstance(pull_request, dict), "pull_request must declare its filters"
    paths = pull_request["paths"]

    assert "scripts/tests/boundary_download_*.py" in paths, (
        "the download harness modules must trigger the boundary workflow"
    )
    assert {
        "scripts/pypy_download.py",
        "scripts/pypy_download_deadline.py",
        "scripts/pypy_download_metrics.py",
        "scripts/install_pypy312.py",
    } <= set(paths), (
        "editing any module of the downloader, or its installer, must not skip "
        "the controlled HTTPS coverage that only this workflow runs"
    )
