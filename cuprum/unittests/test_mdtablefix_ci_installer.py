"""Declarative contract for CI's shared mdtablefix installer."""

from __future__ import annotations

import pytest

from tests.helpers.ci_leg_gate import ungated
from tests.helpers.ci_runners import job, steps, workflow_env

_SHARED_ACTION_REVISION = "c5a54701c8603a0fa756a6b34c49bc2af75a6c11"
_INSTALL_MDTABLEFIX = (
    "leynos/shared-actions/.github/actions/install-mdtablefix@"
    f"{_SHARED_ACTION_REVISION}"
)
_FULL_PYTHON_TEST_STEPS = [
    ("ci.yml", "lint-test", "Markdown formatter checker tests"),
    ("ci.yml", "typecheck-test", "Run tests"),
    ("ci.yml", "coverage", "Generate coverage"),
    ("coverage-main.yml", "coverage-upload", "Generate coverage"),
]


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "test_step_name"), _FULL_PYTHON_TEST_STEPS
)
def test_full_python_test_jobs_install_mdtablefix_before_running_tests(
    workflow_name: str,
    job_name: str,
    test_step_name: str,
) -> None:
    """Every full Python suite receives mdtablefix before its tests start."""
    declared_steps = steps(workflow_name, job_name)
    installers = [
        step for step in declared_steps if step.get("name") == "Install mdtablefix"
    ]
    assert len(installers) == 1, (
        f"{workflow_name}:{job_name} must declare one mdtablefix installer"
    )
    installer = installers[0]

    assert installer.get("uses") == _INSTALL_MDTABLEFIX, (
        f"{workflow_name}:{job_name} must use the pinned shared mdtablefix installer"
    )
    inputs = installer.get("with")
    assert isinstance(inputs, dict), (
        f"{workflow_name}:{job_name} must pass inputs to the shared "
        "mdtablefix installer"
    )
    assert inputs.get("version") == "${{ env.MDTABLEFIX_VERSION }}", (
        f"{workflow_name}:{job_name} must pass its pinned mdtablefix version "
        "to the installer"
    )
    assert "run" not in installer, (
        f"{workflow_name}:{job_name} must not retain a local mdtablefix "
        "source-build fallback"
    )
    job_environment = job(workflow_name, job_name).get("env")
    environment = (
        job_environment
        if isinstance(job_environment, dict) and "MDTABLEFIX_VERSION" in job_environment
        else workflow_env(workflow_name)
    )
    assert environment.get("MDTABLEFIX_VERSION") == "0.6.0", (
        f"{workflow_name}:{job_name} must pin MDTABLEFIX_VERSION to 0.6.0"
    )
    assert declared_steps.index(installer) < declared_steps.index(
        next(step for step in declared_steps if step.get("name") == test_step_name)
    ), f"{workflow_name}:{job_name} must install mdtablefix before {test_step_name}"


def test_typecheck_python_suite_installs_mdtablefix_on_every_leg() -> None:
    """Every matrix leg runs the Python suite, so every leg needs the formatter."""
    installer = next(
        step
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Install mdtablefix"
    )

    guard = ungated("ci.yml", "typecheck-test", installer.get("if"))
    assert guard == "", (
        "typecheck-test must install mdtablefix on every leg, gated only by "
        f"the leg flag, got if: {installer.get('if')!r}"
    )
