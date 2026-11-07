"""Keep formal boundary validation hosted, bounded, and free of target archives."""

from __future__ import annotations

import re
import shlex
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


def test_prover_tools_selects_its_required_python() -> None:
    """The installer must override CI's Python 3.13 with its required Python 3.14."""
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(
        encoding="utf-8"
    )
    assignment = next(
        line for line in makefile.splitlines() if line.startswith("PROVER_TOOLS =")
    )
    arguments = shlex.split(assignment.partition("=")[2])
    assert "--python" in arguments, (
        "ambient UV_PYTHON can select an incompatible runtime"
    )
    runtime = arguments[arguments.index("--python") + 1]
    assert runtime == "3.14", "the pinned prover-tools package requires Python 3.14"


def test_verus_cache_tracks_the_shared_installer_pin() -> None:
    """A new shared-installer revision must invalidate the cached Verus and Z3 trees.

    The retired `scripts/install_boundary_z3.py` used to name this key. The pin
    now lives in `tools/prover-tools/VERSION`, so hashing that file keeps the
    archived solver and verifier from outliving the revision that produced
    them, while an unrelated Makefile edit still restores a warm cache.
    """
    restore = next(
        step
        for step in steps(WORKFLOW, "verus")
        if step.get("name") == "Restore pinned verifier, solver, and compiler objects"
    )
    inputs = step_inputs(restore, "the verus restore must declare its inputs")
    expected = ["tools/verus/*", "tools/prover-tools/VERSION"]
    key = str(inputs.get("key", ""))
    hashed = re.findall(r"'([^']*)'", key)
    assert hashed == expected, (
        "the verifier cache key must hash only the inputs that pick the binaries"
    )
    prefix = "boundary-verus-${{ hashFiles(" + ", ".join(
        f"'{path}'" for path in expected
    )
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
    """The opt-in suite needs both the switch and the pinned revision."""
    tokens = recipe_tokens(recipe_of("boundary-download-integration"))

    assert tokens == (
        "CUPRUM_RUN_DOWNLOAD_INTEGRATION=1",
        "CUPRUM_PROVER_TOOLS_SOURCE=$(PROVER_TOOLS_SOURCE)",
        "$(PYTEST)",
        "-v",
        "scripts/tests/test_boundary_download_integration.py",
    ), "the target must switch the suite on, pass the pin, and name the module"


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
