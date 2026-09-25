"""Run a named `run:` step from any workflow job or composite action.

``tests/helpers/release_workflow.py`` runs steps scoped to ``release.yml``,
and ``tests/helpers/composite_actions.py`` runs steps scoped to one composite
action with the runner's ``GITHUB_ENV``/``GITHUB_STEP_SUMMARY`` protocol
emulated. Neither generalizes to an arbitrary workflow job, which is what a
contract over ``build-wheels.yml`` or ``rust-boundaries.yml`` needs. This
module holds that generalization: locate a step by name in either a workflow
job or a composite action, then execute its ``run`` body under Bash with
stand-in tools on ``PATH``, so the contract exercises the checked-in script
rather than a restatement of it.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - executes checked-in workflow code.
import typing as typ

from tests.helpers.ci_workflows import steps as workflow_job_steps
from tests.helpers.composite_actions import action_document

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow_types import Step


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def _action_steps(action_path: str) -> list[Step]:
    """Return the steps declared by one composite action."""
    declared = typ.cast(
        "dict[str, object]", action_document(action_path).get("runs", {})
    ).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"{action_path} must declare a list of steps",
    )
    return typ.cast("list[Step]", declared)


def find_step(
    *,
    workflow: str | None = None,
    job: str | None = None,
    action: str | None = None,
    name: str,
) -> Step:
    """Return one named step from a workflow job or a composite action.

    Parameters
    ----------
    workflow : str | None
        File name of the workflow under ``.github/workflows``. Pass this and
        ``job`` together to look up a workflow step.
    job : str | None
        Job the step is expected in, paired with ``workflow``.
    action : str | None
        Path to a composite action directory, relative to the repository
        root, such as ``.github/actions/build-wheels``. Pass this alone to
        look up a composite action's step.
    name : str
        The step's ``name:`` value.

    Returns
    -------
    Step
        The one matching step.

    Notes
    -----
    Fails the contract, through :func:`_require`, when neither or both of a
    workflow job and an action are given, or when no step of the given name
    exists there.

    Examples
    --------
    >>> step = find_step(
    ...     action=".github/actions/pure-python-wheel", name="Build sdist and wheel"
    ... )
    >>> "uv build" in step["run"]
    True
    """
    if action is not None:
        _require(
            condition=workflow is None and job is None,
            message="pass either a workflow job or an action, not both",
        )
        candidates = _action_steps(action)
        location = action
    else:
        _require(
            condition=workflow is not None and job is not None,
            message="pass a workflow and job, or an action",
        )
        candidates = workflow_job_steps(typ.cast("str", workflow), typ.cast("str", job))
        location = f"{workflow}:{job}"
    found = next((item for item in candidates if item.get("name") == name), None)
    _require(condition=found is not None, message=f"{location} must have step {name!r}")
    return typ.cast("Step", found)


def step_script(
    *,
    workflow: str | None = None,
    job: str | None = None,
    action: str | None = None,
    name: str,
) -> str:
    """Return the ``run:`` script of one named workflow or action step."""
    script = find_step(workflow=workflow, job=job, action=action, name=name).get("run")
    _require(
        condition=isinstance(script, str), message=f"step {name!r} must run a script"
    )
    return typ.cast("str", script)


def run_bash(
    script: str, cwd: pth.Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under Bash in ``cwd`` with ``env`` layered over ours."""
    bash = shutil.which("bash", path=os.defpath)
    _require(condition=bash is not None, message="workflow step tests require Bash")
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
