"""Protect native-wheel warning suppression in both wheel-build entry points.

The tests parse the wheel workflow and composite action to verify that Linux
container builds and direct Maturin builds receive the suppression setting.
This records the intentional split between the declared ``uv_build`` backend
for pure-Python wheels and Maturin's direct native-wheel CLI invocation.
"""

from __future__ import annotations

import yaml

from cuprum.unittests._maturin_pin_support import read_text
from tests.helpers.ci_workflows import job, step_inputs, steps
from tests.helpers.docs import repo_root

_WARNING_SUPPRESSION = "MATURIN_NO_MISSING_BUILD_BACKEND_WARNING"
_WORKFLOW = "build-wheels.yml"
_NATIVE_WHEEL_JOB = "build-native-wheels"


def test_linux_maturin_action_forwards_backend_warning_suppression() -> None:
    """Both manylinux containers receive the warning suppression setting."""
    native_job = job(_WORKFLOW, _NATIVE_WHEEL_JOB)
    environment = native_job.get("env")
    assert isinstance(environment, dict)
    assert environment.get(_WARNING_SUPPRESSION) == "1"

    native_build_steps = [
        step
        for step in steps(_WORKFLOW, _NATIVE_WHEEL_JOB)
        if str(step.get("uses", "")).startswith("messense/maturin-action@")
    ]
    assert len(native_build_steps) == 2, (
        "the native-wheel workflow should have one maturin-action step for "
        f"each Linux architecture; found {len(native_build_steps)}"
    )

    for step in native_build_steps:
        inputs = step_inputs(step, "maturin-action build step must declare inputs")
        assert inputs.get("docker-options") == f"-e {_WARNING_SUPPRESSION}"


def test_composite_native_build_sets_backend_warning_suppression() -> None:
    """The direct maturin build receives the same suppression setting."""
    action = yaml.safe_load(
        read_text(repo_root(), ".github/actions/build-wheels/action.yml")
    )
    assert isinstance(action, dict)
    runs = action.get("runs")
    assert isinstance(runs, dict)
    action_steps = runs.get("steps")
    assert isinstance(action_steps, list)

    build_steps = [
        step
        for step in action_steps
        if isinstance(step, dict) and step.get("name") == "Build wheels with maturin"
    ]
    assert len(build_steps) == 1, (
        "the composite action should have one named maturin build step; "
        f"found {len(build_steps)}"
    )
    build_step = build_steps[0]
    assert "maturin build" in str(build_step.get("run", ""))
    environment = build_step.get("env")
    assert isinstance(environment, dict)
    assert environment.get(_WARNING_SUPPRESSION) == "1"
