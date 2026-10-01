"""Read and execute workflow step scripts in a scratch directory.

The release tests run the checked-in ``run`` bodies of ``release.yml`` rather
than restating them, so the contract is what a tag push actually executes.
Steps that call ``scripts/`` see the repository's copy through
:func:`link_scripts`, and :func:`fake_release` routes ``gh`` and ``curl`` to
``release_fake_tools.py``, whose directories stand in for the GitHub Release
and PyPI. Scope: ``release.yml`` by default, and any other workflow whose
steps are executed the same way (``delayed-pr-comment.yml``) by naming it;
composite actions have ``tests/helpers/composite_actions.py``.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import pathlib as pth
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import sys
import typing as typ

from tests.helpers.ci_workflows import ROOT, steps

WORKFLOW = "release.yml"
_FAKE_TOOLS = pth.Path(__file__).with_name("release_fake_tools.py")


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def step(job_name: str, name: str, *, workflow: str = WORKFLOW) -> dict[str, object]:
    """Return the step called ``name`` in one job of ``workflow``."""
    found = next(
        (item for item in steps(workflow, job_name) if item.get("name") == name),
        None,
    )
    _require(
        condition=found is not None,
        message=f"{workflow} job {job_name!r} must have step {name!r}",
    )
    return typ.cast("dict[str, object]", found)


def step_script(job_name: str, name: str, *, workflow: str = WORKFLOW) -> str:
    """Return the ``run`` script of one step in one job of ``workflow``."""
    script = step(job_name, name, workflow=workflow).get("run")
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
    """Run ``script`` under Bash in ``cwd`` with ``env`` layered over ours.

    ``BASH_ENV`` is dropped rather than forwarded. Bash sources the file it
    names for every non-interactive shell, so a developer's copy would run host
    setup ahead of the checked-in step and silently change what the step does.
    GitHub Actions does not set it, so neither does this helper. A caller that
    passes ``BASH_ENV`` in ``env`` still gets it: explicit intent is respected,
    ambient leakage is not.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed process, with standard output and error captured as text.
    """
    bash = shutil.which("bash", path=os.defpath)
    _require(
        condition=bash is not None, message="the release workflow tests require Bash"
    )
    environment: dict[str, str] = dict(os.environ)
    environment.pop("BASH_ENV", None)
    environment.update(env or {})
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - checked-in workflow code.
        [typ.cast("str", bash), "-c", script],
        capture_output=True,
        check=False,
        cwd=cwd,
        env=environment,
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


def link_scripts(cwd: pth.Path) -> None:
    """Make the repository's ``scripts`` visible to steps run in ``cwd``."""
    (cwd / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)


@dc.dataclass(frozen=True, slots=True)
class FakeRelease:
    """Directories standing in for the GitHub Release and PyPI, and a call log."""

    root: pth.Path

    @property
    def github(self) -> pth.Path:
        """The directory holding one file per release asset."""
        return self.root / "github-assets"

    @property
    def pypi(self) -> pth.Path:
        """The directory holding one file per PyPI file."""
        return self.root / "pypi-files"

    def env(self, **extra: str) -> dict[str, str]:
        """Return the environment that routes ``gh`` and ``curl`` to the fakes."""
        return {
            "PATH": os.pathsep.join((str(self.root / "tools"), os.defpath)),
            "FAKE_GITHUB_ASSETS": str(self.github),
            "FAKE_PYPI_FILES": str(self.pypi),
            "FAKE_CALLS": str(self.root / "calls.jsonl"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            **extra,
        }

    def calls(self, tool: str) -> list[list[str]]:
        """Return the argument vectors ``tool`` was called with, in order."""
        log = self.root / "calls.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [call[1:] for call in map(json.loads, lines) if call[0] == tool]


def fake_release(root: pth.Path) -> FakeRelease:
    """Install ``gh``, ``curl``, ``python3``, and a no-op ``sleep`` under ``root``."""
    tools = root / "tools"
    for tool in ("gh", "curl"):
        install_tool(
            tools, tool, f'exec "{sys.executable}" "{_FAKE_TOOLS}" {tool} "$@"'
        )
    install_tool(tools, "python3", f'exec "{sys.executable}" "$@"')
    install_tool(tools, "sleep", ":")
    fake = FakeRelease(root)
    fake.github.mkdir(parents=True, exist_ok=True)
    fake.pypi.mkdir(parents=True, exist_ok=True)
    return fake


def run_version_check(
    cwd: pth.Path, declared: str, tag_version: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run ``check-version``'s pyproject.toml step for one declared version."""
    (cwd / "pyproject.toml").write_text(
        f'[project]\nname = "cuprum"\nversion = "{declared}"\n', encoding="utf-8"
    )
    if not (cwd / "scripts").exists():
        link_scripts(cwd)
    github_output = cwd / "github-output"
    github_output.unlink(missing_ok=True)
    completed = run_bash(
        step_script(
            "check-version", "Check the pyproject.toml version against the tag"
        ),
        cwd,
        {"TAG_VERSION": tag_version, "GITHUB_OUTPUT": str(github_output)},
    )
    return completed, outputs(github_output)
