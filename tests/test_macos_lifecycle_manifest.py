"""The macOS lifecycle manifest names real cases and stays fully routed.

``tests/helpers/macos_lifecycle.py`` is the selection the advisory macOS leg
runs. Two kinds of drift would leave it green while it proved less than it
claims: a target, deselection, or required node ID that no longer exists (pytest
silently ignores a stale ``--deselect``), and a root-level test module that the
default selector never collects. This module checks both against the collection
pytest actually produces on Linux, which is the host that can run it here.
"""

from __future__ import annotations

import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed pytest argv.
import sys

import pytest

from tests.helpers.macos_lifecycle import (
    DESELECTED,
    REQUIRED_PASSING,
    TARGETS,
)
from tests.helpers.makefile import variable_expansion
from tests.helpers.suite_selection import SELECTOR, selected_paths

ROOT = pth.Path(__file__).resolve().parents[1]
_ACCEPTANCE_MODULES = (
    "cuprum/unittests/test_process_exit_probe.py",
    "cuprum/unittests/test_cancelled_run_descendant_tree.py",
)


def _module_of(node_id: str) -> str:
    """Return the module path a node ID selects."""
    module, _, _rest = node_id.partition("::")
    return module


@pytest.fixture(scope="module")
def collected_node_ids() -> frozenset[str]:
    """Return every node ID the manifest's targets collect on this host."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed pytest argv.
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "--color=no",
            "-q",
            "-p",
            "no:cacheprovider",
            *TARGETS,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "the manifest targets must collect cleanly; "
        f"pytest said:\n{completed.stdout}\n{completed.stderr}"
    )
    return frozenset(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and line.strip().startswith("cuprum/")
    )


def test_every_target_is_a_real_module() -> None:
    """A target that names no file would be ignored by pytest."""
    assert len(TARGETS) == len(set(TARGETS)), f"duplicate targets: {TARGETS!r}"
    missing = [target for target in TARGETS if not (ROOT / target).is_file()]
    assert not missing, f"the manifest targets files that do not exist: {missing!r}"
    assert all(target.endswith(".py") for target in TARGETS), (
        f"every target must be a Python module: {TARGETS!r}"
    )


def test_the_acceptance_modules_are_targeted() -> None:
    """The issue's two named modules must stay in the leg."""
    missing = [module for module in _ACCEPTANCE_MODULES if module not in TARGETS]
    assert not missing, f"the acceptance criteria need these modules: {missing!r}"


def test_every_deselection_states_a_reason_and_names_a_target() -> None:
    """A removal is a claim with a reason, not a way to silence a skip."""
    node_ids = [entry.node_id for entry in DESELECTED]
    assert len(node_ids) == len(set(node_ids)), f"duplicate deselections: {node_ids!r}"
    for entry in DESELECTED:
        assert entry.reason.strip(), f"{entry.node_id} records no reason"
        assert _module_of(entry.node_id) in TARGETS, (
            f"{entry.node_id} is deselected outside the manifest's targets"
        )


def test_required_outcomes_are_unique_and_not_deselected() -> None:
    """A required case the manifest also removes could never pass."""
    assert REQUIRED_PASSING, "the leg must promise at least one outcome"
    assert len(REQUIRED_PASSING) == len(set(REQUIRED_PASSING)), (
        f"duplicate required outcomes: {REQUIRED_PASSING!r}"
    )
    removed = {entry.node_id for entry in DESELECTED}
    overlap = sorted(removed.intersection(REQUIRED_PASSING))
    assert not overlap, f"required outcomes are also deselected: {overlap!r}"
    outside = [
        node_id for node_id in REQUIRED_PASSING if _module_of(node_id) not in TARGETS
    ]
    assert not outside, f"required outcomes outside the targets: {outside!r}"


def test_required_outcomes_cover_the_acceptance_cases() -> None:
    """The ``ps`` route and all four cancellation cases are the promised ones."""
    probe = {
        node_id
        for node_id in REQUIRED_PASSING
        if "test_process_exit_probe.py" in node_id and node_id.endswith("[ps]")
    }
    assert len(probe) == 3, f"the three ps-route probe cases are required: {probe!r}"
    cancellation = {
        node_id
        for node_id in REQUIRED_PASSING
        if "test_cancelled_run_descendant_tree.py" in node_id
    }
    assert len(cancellation) == 4, (
        f"all four cancellation cases are required: {cancellation!r}"
    )


def test_every_predicted_node_id_is_collected(
    collected_node_ids: frozenset[str],
) -> None:
    """A node ID that no longer exists would silently weaken the leg."""
    predicted = {entry.node_id for entry in DESELECTED} | set(REQUIRED_PASSING)
    missing = sorted(predicted - collected_node_ids)
    assert not missing, (
        "these manifest node IDs no longer match a collected case, so pytest "
        f"would ignore the removal or promise a case that never runs: {missing!r}"
    )


def test_both_new_root_modules_are_routed_by_the_default_selector() -> None:
    """A root-level module outside the selector never runs in the suite."""
    selected = {str(path) for path in selected_paths(variable_expansion(SELECTOR))}
    expected = {
        "tests/test_macos_lifecycle_manifest.py",
        "tests/test_required_outcomes_guard.py",
        "tests/test_ci_macos_lifecycle_workflow_contract.py",
    }
    missing = sorted(expected - selected)
    assert not missing, (
        f"{SELECTOR} must collect every new contract module; missing: {missing!r}"
    )
