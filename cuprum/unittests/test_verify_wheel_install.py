"""Regression coverage for the native-wheel package inspection step."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import sys
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth

_WORKFLOW_PATH = ".github/workflows/build-wheels.yml"
_LISTING_MARKER = 'echo "=== Listing installed cuprum package ==="\n'
_LATER_CHECK_MARKER = 'echo "=== Testing Rust extension import ==="'


def _package_listing_script() -> str:
    """Extract the package-listing Python script from the wheel workflow."""
    workflow = yaml.safe_load(
        (repo_root() / _WORKFLOW_PATH).read_text(encoding="utf-8")
    )
    assert isinstance(workflow, dict), f"{_WORKFLOW_PATH} must parse to a mapping"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), f"{_WORKFLOW_PATH} must declare jobs"
    verify_job = jobs.get("verify-wheel-install")
    assert isinstance(verify_job, dict), "build-wheels must verify installed wheels"
    steps = verify_job.get("steps")
    assert isinstance(steps, list), "verify-wheel-install must declare steps"
    script = next(
        (
            step.get("run")
            for step in steps
            if isinstance(step, dict) and _LISTING_MARKER in str(step.get("run"))
        ),
        None,
    )
    assert isinstance(script, str), "verify-wheel-install must list its package"
    listing_section = script.split(_LISTING_MARKER, maxsplit=1)[1]
    listing_section = listing_section.split(_LATER_CHECK_MARKER, maxsplit=1)[0]
    return listing_section.removeprefix("  python - <<'PY'\n").removesuffix("PY\n")


def _write_failing_ls(directory: pth.Path) -> pth.Path:
    """Write an ``ls`` replacement that records and fails its invocation."""
    fake_ls = directory / "ls"
    fake_ls.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "${LS_ARGUMENTS}"\nexit 23\n',
        encoding="utf-8",
    )
    fake_ls.chmod(fake_ls.stat().st_mode | stat.S_IXUSR)
    return fake_ls


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The Linux wheel workflow executes its package check through Bash and ls.",
)
def test_package_listing_failure_stops_the_verify_wheel_step(
    tmp_path: pth.Path,
) -> None:
    """A failed package listing prevents later wheel checks from running."""
    package_root = tmp_path / "site-packages"
    package_directory = package_root / "cuprum"
    package_directory.mkdir(parents=True)
    (package_directory / "__init__.py").write_text("", encoding="utf-8")
    tools_directory = tmp_path / "tools"
    tools_directory.mkdir()
    _write_failing_ls(tools_directory)
    later_check_marker = tmp_path / "later-check-ran"
    ls_arguments = tmp_path / "ls-arguments"
    environment = {
        **os.environ,
        "LATER_CHECK_MARKER": str(later_check_marker),
        "LS_ARGUMENTS": str(ls_arguments),
        "PATH": os.pathsep.join((str(tools_directory), os.defpath)),
        "PYTHONPATH": str(package_root),
    }
    shell_script = "\n".join((
        "set -euo pipefail",
        "python - <<'PY'",
        _package_listing_script(),
        "PY",
        "printf '%s\\n' reached > \"${LATER_CHECK_MARKER}\"",
    ))
    bash_path = shutil.which("bash", path=os.defpath)
    assert bash_path is not None, "the Linux workflow test requires Bash"

    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [bash_path, "-c", shell_script],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert completed.returncode != 0, "a failing package listing unexpectedly passed"
    assert f"Package at: {package_directory}" in completed.stdout
    assert ls_arguments.read_text(encoding="utf-8").splitlines() == [
        "-la",
        str(package_directory),
    ]
    assert not later_check_marker.exists(), "later wheel checks ran after ls failed"
