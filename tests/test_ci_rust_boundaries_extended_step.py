"""Exercise the ``extended`` job's "Execute extended verification" step.

The step derives its ``make`` target from ``BOUNDARY_CHECK`` and pipes the
target's output through ``tee`` into a per-check log, under ``pipefail`` so a
failing ``make`` fails the step even though ``tee`` itself always succeeds.
Reading the YAML cannot show that the derived target name is right for both
matrix values, that the log lands where the archive step expects it, or that
``pipefail`` is doing its job rather than the pipeline succeeding regardless
of what ``make`` did.
"""

from __future__ import annotations

import os
import typing as typ

import pytest

from tests.helpers.workflow_steps import install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

WORKFLOW = "rust-boundaries.yml"
JOB = "extended"
STEP = "Execute extended verification"


def _run(
    tmp_path: Path, *, boundary_check: str, make_body: str
) -> tuple[int, list[str]]:
    """Run the step with a fake ``make`` and return its exit code and argv."""
    tools = tmp_path / "tools"
    record = tmp_path / "make-argv"
    install_tool(
        tools,
        "make",
        f'printf \'%s\\n\' "$@" > "{record}"\n{make_body}',
    )
    script = step_script(workflow=WORKFLOW, job=JOB, name=STEP)
    result = run_bash(
        script,
        cwd=tmp_path,
        env={
            "BOUNDARY_CHECK": boundary_check,
            "PATH": f"{tools}:{os.environ['PATH']}",
        },
    )
    argv = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return result.returncode, argv


@pytest.mark.parametrize("boundary_check", ["kani", "miri"])
def test_derives_make_target_from_boundary_check(
    tmp_path: Path, boundary_check: str
) -> None:
    """The make target must name the matrix's own boundary check."""
    returncode, argv = _run(
        tmp_path, boundary_check=boundary_check, make_body="echo ok\n"
    )

    assert returncode == 0, (
        f"the step must succeed on a passing make, argv was {argv!r}"
    )
    assert argv == [f"boundary-{boundary_check}"], (
        f"make must be invoked with the boundary-{boundary_check} target, got {argv!r}"
    )


@pytest.mark.parametrize("boundary_check", ["kani", "miri"])
def test_writes_log_file_named_for_the_boundary_check(
    tmp_path: Path, boundary_check: str
) -> None:
    """The log must land where the archive step later expects to find it."""
    _run(
        tmp_path,
        boundary_check=boundary_check,
        make_body="echo verification output\n",
    )

    log = (
        tmp_path / "rust" / "target" / "boundary-verification" / f"{boundary_check}.log"
    )
    assert log.exists(), f"expected the boundary log at {log}"
    assert "verification output" in log.read_text(encoding="utf-8"), (
        f"the log at {log} must capture make's output"
    )


def test_failing_make_fails_the_step_despite_tee(tmp_path: Path) -> None:
    """``pipefail`` must carry a failing ``make`` through the ``tee`` pipe."""
    returncode, _argv = _run(
        tmp_path,
        boundary_check="kani",
        make_body='echo "boom" >&2\nexit 1\n',
    )

    assert returncode != 0, "pipefail must carry make's failure through tee"
