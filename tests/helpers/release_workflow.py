"""Read and execute the release workflow's step scripts in a scratch directory.

The release tests run the checked-in ``run`` bodies of ``release.yml`` rather
than restating them, so the contract is what a tag push actually executes.
Scope: ``release.yml`` only; composite actions have
``tests/helpers/composite_actions.py``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import typing as typ

from tests.helpers.ci_workflows import steps

if typ.TYPE_CHECKING:
    import pathlib as pth

WORKFLOW = "release.yml"


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def step(job_name: str, name: str) -> dict[str, object]:
    """Return the step called ``name`` in one release job."""
    found = next(
        (item for item in steps(WORKFLOW, job_name) if item.get("name") == name),
        None,
    )
    _require(
        condition=found is not None,
        message=f"release job {job_name!r} must have step {name!r}",
    )
    return typ.cast("dict[str, object]", found)


def step_script(job_name: str, name: str) -> str:
    """Return the ``run`` script of one release-job step."""
    script = step(job_name, name).get("run")
    _require(
        condition=isinstance(script, str), message=f"step {name!r} must run a script"
    )
    return typ.cast("str", script)


def python_heredoc(script: str) -> str:
    """Return the Python program a step feeds to ``python - <<'PY'``."""
    body = script.split("<<'PY'\n", maxsplit=1)[1]
    return body.rsplit("PY", 1)[0]


def run_bash(
    script: str, cwd: pth.Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under Bash in ``cwd`` with ``env`` layered over ours."""
    bash = shutil.which("bash", path=os.defpath)
    _require(
        condition=bash is not None, message="the release workflow tests require Bash"
    )
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [typ.cast("str", bash), "-c", script],
        capture_output=True,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        text=True,
    )


def install_tool(tools: pth.Path, name: str, body: str) -> None:
    """Write an executable ``/bin/sh`` stand-in called ``name`` into ``tools``."""
    tools.mkdir(exist_ok=True)
    fake = tools / name
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


def outputs(path: pth.Path) -> dict[str, str]:
    """Parse the ``name=value`` lines a step appended to ``GITHUB_OUTPUT``."""
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)
