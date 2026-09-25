"""Exercise the ``verify-wheel-install`` job's source-distribution count check.

The release publishes whichever single ``.tar.gz`` sits in ``dist/wheels-pure``
alongside the pure wheel, so a build that produced none or two would publish
the wrong thing, or nothing, at tag time. This runs the checked-in glob-count
check in isolation from the surrounding venv setup and package install, which
would otherwise need network access, against zero, one, and two candidate
files.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.workflow_steps import run_bash, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

WORKFLOW = "build-wheels.yml"
JOB = "verify-wheel-install"
STEP = "Install pure Python wheel, then native wheel"

#: The full step also creates a venv and installs packages from the network,
#: which this contract does not exercise. Both the pure-wheel and sdist
#: cardinality checks run before any of that, bounded by these markers taken
#: from the checked-in script, so a rewrite that moves the checks fails loudly
#: here rather than silently testing something else.
_CHECKS_START = "# Expand pure wheel glob and verify exactly one match"
_CHECKS_END = "PURE_WHEEL=$(realpath"


def _cardinality_check_script() -> str:
    """Return just the pure-wheel and sdist glob-count checks."""
    full_script = step_script(workflow=WORKFLOW, job=JOB, name=STEP)
    start = full_script.index(_CHECKS_START)
    end = full_script.index(_CHECKS_END)
    return f"set -euo pipefail\n{full_script[start:end]}"


def _run_with_sdist_count(tmp_path: Path, count: int) -> tuple[int, str]:
    """Run the checks with one pure wheel and ``count`` source distributions."""
    wheels_pure = tmp_path / "dist" / "wheels-pure"
    wheels_pure.mkdir(parents=True)
    (wheels_pure / "cuprum-1.0-py3-none-any.whl").touch()
    for index in range(count):
        (wheels_pure / f"cuprum-1.0-{index}.tar.gz").touch()
    result = run_bash(_cardinality_check_script(), cwd=tmp_path)
    return result.returncode, result.stderr


def test_zero_source_distributions_fails_with_expected_message(
    tmp_path: Path,
) -> None:
    """No sdist candidate must fail the step, not silently proceed."""
    returncode, stderr = _run_with_sdist_count(tmp_path, 0)

    assert returncode != 0, f"zero sdists must fail the step, got {stderr!r}"
    assert "Expected exactly one source distribution." in stderr, (
        f"missing the expected diagnostic, got {stderr!r}"
    )


def test_one_source_distribution_passes(tmp_path: Path) -> None:
    """Exactly one sdist candidate is the only shape the step must accept."""
    returncode, stderr = _run_with_sdist_count(tmp_path, 1)

    assert returncode == 0, stderr


def test_two_source_distributions_fails_with_expected_message(
    tmp_path: Path,
) -> None:
    """Two sdist candidates are as ambiguous as none and must also fail."""
    returncode, stderr = _run_with_sdist_count(tmp_path, 2)

    assert returncode != 0, f"two sdists must fail the step, got {stderr!r}"
    assert "Expected exactly one source distribution." in stderr, (
        f"missing the expected diagnostic, got {stderr!r}"
    )
