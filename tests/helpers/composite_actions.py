"""Run a composite action's shell steps directly, outside a GitHub runner.

A contract test that only checks a workflow references an action proves the
wiring and nothing about the behaviour: the sampler could export no process
identifier, sample nothing, or compute its peaks wrongly, and every workflow
assertion would still pass. These helpers execute a named step's ``run`` body
under ``bash`` with the runner's file-backed protocol emulated, so the shell
itself is under test.

Only the protocol the actions here actually use is emulated: ``GITHUB_ENV``
and ``GITHUB_STEP_SUMMARY`` as append-only files, and ``GITHUB_ENV`` fed back
into the environment between steps the way the runner does.
"""

from __future__ import annotations

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import typing as typ

import yaml

from tests.helpers.ci_workflows import ROOT

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


@dc.dataclass(frozen=True, slots=True)
class StepResult:
    """What running one step's shell produced."""

    returncode: int
    stdout: str
    stderr: str
    #: Values the step appended to ``GITHUB_ENV``, in the runner's ``NAME=value``
    #: form. The runner exports these to every later step.
    exported: dict[str, str]
    #: Text the step appended to ``GITHUB_STEP_SUMMARY``.
    summary: str


def action_document(action_path: str) -> dict[str, object]:
    """Parse one repository composite action.

    Parameters
    ----------
    action_path:
        Path to the action directory, relative to the repository root, such as
        ``.github/actions/resource-sampler``.

    Returns
    -------
    dict[str, object]
        The parsed ``action.yml``.

    Notes
    -----
    Fails the contract, through :func:`_require`, when the file does not parse
    to a mapping.
    """
    document = yaml.safe_load(
        (ROOT / action_path / "action.yml").read_text(encoding="utf-8")
    )
    _require(
        condition=isinstance(document, dict),
        message=f"{action_path}/action.yml must parse to a mapping",
    )
    return typ.cast("dict[str, object]", document)


def step_script(action_path: str, step_name: str) -> str:
    """Return the ``run`` body of one named step of a composite action."""
    declared = typ.cast(
        "dict[str, object]", action_document(action_path).get("runs", {})
    ).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"{action_path} must declare a list of steps",
    )
    for step in typ.cast("list[object]", declared):
        candidate = typ.cast("dict[str, object]", step)
        if candidate.get("name") != step_name:
            continue
        script = candidate.get("run")
        _require(
            condition=isinstance(script, str),
            message=f"{action_path}:{step_name} must run a script",
        )
        return typ.cast("str", script)
    _require(
        condition=False,
        message=f"{action_path} declares no step named {step_name!r}",
    )
    raise AssertionError


def _parse_exports(text: str) -> dict[str, str]:
    """Read a ``GITHUB_ENV`` file's ``NAME=value`` lines."""
    exported: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            name, value = line.split("=", maxsplit=1)
            exported[name] = value
    return exported


def run_step(
    script: str,
    *,
    workdir: Path,
    environment: cabc.Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> StepResult:
    """Execute one step's shell body with the runner's file protocol emulated.

    Parameters
    ----------
    script:
        The ``run`` body, as written in the action.
    workdir:
        Directory to run in, and where the emulated protocol files are placed.
    environment:
        Extra variables the step should see, such as the ``env`` block a
        workflow would supply.
    timeout:
        Seconds to allow before failing the test rather than hanging it.

    Returns
    -------
    StepResult
        The exit status, both output streams, the values appended to
        ``GITHUB_ENV``, and the text appended to ``GITHUB_STEP_SUMMARY``.
    """
    env_file = workdir / "github_env"
    summary_file = workdir / "github_step_summary"
    env_file.touch()
    summary_file.touch()
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(workdir),
        "GITHUB_ENV": str(env_file),
        "GITHUB_STEP_SUMMARY": str(summary_file),
        **(environment or {}),
    }
    # ruff: ignore[subprocess-without-shell-equals-true] - the argv is fixed and
    # the script comes from a tracked action file, not from user input; running
    # it is the point of this helper.
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=workdir,
        env=env,
        timeout=timeout,
        check=False,
    )
    return StepResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exported=_parse_exports(env_file.read_text(encoding="utf-8")),
        summary=summary_file.read_text(encoding="utf-8"),
    )
