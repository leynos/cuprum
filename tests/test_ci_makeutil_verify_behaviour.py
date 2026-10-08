"""Run each consumer's `Verify makeutil` script against a stand-in binary.

The workflow contract in `tests/test_ci_makeutil_install.py` pins the text of
the verify script. These tests execute that text, so the guard is proved by what
it does: it passes for a matching version and a complete parse, and fails for a
version the action did not report, or an incomplete parse. A
stand-in `makeutil` on `PATH` keeps the check offline.
"""

from __future__ import annotations

import os
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import typing as typ

import pytest

from tests.helpers.ci_makeutil import VERIFY_STEP
from tests.helpers.ci_workflows import steps
from tests.test_ci_makeutil_install import CONSUMERS

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The version the install action reports in these scenarios.
REPORTED_VERSION: typ.Final = "9.8.7"
#: A stand-in that answers `--version` and `parse` from its environment.
STAND_IN: typ.Final = """#!/usr/bin/env bash
case "$1" in
  --version) echo "makeutil ${STUB_VERSION}" ;;
  parse) printf '{"parse": {"status": "%s"}}\\n' "${STUB_STATUS}" ;;
  *) exit 64 ;;
esac
"""


def _verify_script(workflow_name: str, job_name: str) -> str:
    """Return the `run` text of the consumer's verify step."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("name") == VERIFY_STEP
    ]
    assert len(matches) == 1, f"{workflow_name}:{job_name} needs one verify step"
    script = matches[0].get("run")
    assert isinstance(script, str), f"{workflow_name}:{job_name} verify needs a run"
    return script


def _run_verify(
    script: str, tmp_path: Path, *, installed: str, version: str, status: str
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` with the stand-in binary on ``PATH``, or none at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in = bin_dir / "makeutil"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
    environment = {
        **os.environ,
        "INSTALLED_VERSION": installed,
        "STUB_VERSION": version,
        "STUB_STATUS": status,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
    return subprocess.run(
        ["/usr/bin/env", "bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
@pytest.mark.parametrize(
    ("version", "status", "passes"),
    [
        (REPORTED_VERSION, "complete", True),
        ("1.2.3", "complete", False),
        (REPORTED_VERSION, "recovered", False),
    ],
    ids=["matching-and-complete", "version-differs", "parse-incomplete"],
)
def test_the_verify_script_accepts_only_the_reported_version_and_a_complete_parse(
    workflow_name: str,
    job_name: str,
    version: str,
    status: str,
    passes: bool,
    tmp_path: Path,
) -> None:
    """The step succeeds only for the reported version and a complete parse."""
    result = _run_verify(
        _verify_script(workflow_name, job_name),
        tmp_path,
        installed=REPORTED_VERSION,
        version=version,
        status=status,
    )
    assert (result.returncode == 0) is passes, (
        f"{workflow_name}:{job_name} returned {result.returncode}: {result.stderr}"
    )
