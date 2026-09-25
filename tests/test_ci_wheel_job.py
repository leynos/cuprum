"""Contracts for the single job that builds the pure wheel and verifies both.

`build-pure-wheel` and `verify-wheel-install` were separate Ubicloud jobs doing
0.2 and 0.3 minutes of work and billing a minute each on every run. They are
one job now, and the merge is only safe while four things hold: the pure wheel
is built nowhere else, it is built before the artefacts are downloaded (the
download is how the verification reads it back, and `release.yml` reads the
same artefact), the job still waits for the native legs, and the verification
installs into a fresh virtual environment rather than one the build touched.
"""

from __future__ import annotations

from tests.helpers.ci_runners import (
    jobs,
    single_step_position_using,
    step_inputs,
    steps,
)

WORKFLOW = "build-wheels.yml"
WHEEL_JOB = "verify-wheel-install"
PURE_WHEEL_ACTION = "./.github/actions/pure-python-wheel"
DOWNLOAD_ACTION = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
PURE_ARTEFACT = "wheels-pure"
INSTALL_STEP = "Install pure Python wheel, then native wheel"


def _step_position(name: str) -> int:
    """Return the position of the uniquely named step in the wheel job."""
    names = [str(step.get("name", "")) for step in steps(WORKFLOW, WHEEL_JOB)]
    assert names.count(name) == 1, f"{WHEEL_JOB} must have one {name!r} step"
    return names.index(name)


def test_the_pure_wheel_is_built_only_in_the_verifying_job() -> None:
    """No other job in the workflow builds the pure wheel.

    A second builder would bring back the job this merge removed, and two
    uploads of `wheels-pure` in one run collide.
    """
    builders = [
        job_name
        for job_name in jobs(WORKFLOW)
        if any(
            step.get("uses") == PURE_WHEEL_ACTION for step in steps(WORKFLOW, job_name)
        )
    ]
    assert builders == [WHEEL_JOB], (
        f"only {WHEEL_JOB} may build the pure wheel, found {builders}"
    )


def test_the_pure_wheel_is_built_before_the_artefacts_are_downloaded() -> None:
    """The download is how the verification reads the pure wheel back."""
    build = single_step_position_using(WORKFLOW, WHEEL_JOB, uses=PURE_WHEEL_ACTION)
    artefact = step_inputs(
        steps(WORKFLOW, WHEEL_JOB)[build], "the pure-wheel build must declare inputs"
    ).get("artifact-name")
    assert artefact == PURE_ARTEFACT, (
        f"the pure wheel must be uploaded as {PURE_ARTEFACT!r}, which release.yml "
        f"publishes, not {artefact!r}"
    )
    download = single_step_position_using(WORKFLOW, WHEEL_JOB, uses=DOWNLOAD_ACTION)
    install = _step_position(INSTALL_STEP)
    assert build < download < install, (
        f"{WHEEL_JOB} must build ({build}), download ({download}) and install "
        f"({install}) in that order"
    )


def test_the_verifying_job_waits_for_the_native_legs_only() -> None:
    """It needs every native wheel, and no removed job name may linger."""
    declared = jobs(WORKFLOW)[WHEEL_JOB]
    assert isinstance(declared, dict), f"{WHEEL_JOB} must be a mapping"
    needs = declared.get("needs")
    assert needs == ["build-native-wheels"], (
        f"{WHEEL_JOB} must need exactly build-native-wheels, got {needs!r}"
    )


def test_the_verification_installs_into_a_fresh_environment() -> None:
    """A virtual environment is created before anything is installed into it."""
    script = str(steps(WORKFLOW, WHEEL_JOB)[_step_position(INSTALL_STEP)].get("run"))
    lines = [line.strip() for line in script.splitlines()]
    assert "python -m venv .venv" in lines, (
        f"{INSTALL_STEP!r} must create its own virtual environment"
    )
    created = lines.index("python -m venv .venv")
    first_install = next(
        index for index, line in enumerate(lines) if line.startswith("python -m pip")
    )
    assert created < first_install, (
        "the virtual environment must exist before the first pip install"
    )
