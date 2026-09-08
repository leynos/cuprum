"""Keep formal boundary validation hosted, bounded, and free of target archives."""

import shlex
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers.ci_workflows import cache_paths, cache_steps, job, steps

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
