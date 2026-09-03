"""Narrow YAML readers for the CI workflow contract tests.

The manifests that say which jobs and caches are intended live in
``tests/helpers/ci_runners.py``; this module only reads the workflows back.
Every accessor validates the shape it narrows, so a malformed workflow fails
with a named diagnostic rather than an opaque ``TypeError`` deep in a test.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Job, Step

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: Ubicloud's transparent cache intercepts `actions/cache` at this version, so
#: a Linux archive written on an Ubicloud runner lands in Ubicloud's store
#: rather than GitHub's. Verified against the Ubicloud console listings on
#: 2026-09-03; v4.3.0 left nothing there. The deprecated `ubicloud/cache` fork
#: is therefore unnecessary.
CACHE_ACTION_PIN = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
CACHE_RESTORE = f"actions/cache/restore@{CACHE_ACTION_PIN}"
CACHE_SAVE = f"actions/cache/save@{CACHE_ACTION_PIN}"
CACHE_PLAIN = f"actions/cache@{CACHE_ACTION_PIN}"


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def _mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping."""
    _require(
        condition=isinstance(value, dict)
        and all(isinstance(key, str) for key in value),
        message=message,
    )
    return typ.cast("dict[str, object]", value)


def workflow_document(workflow_name: str) -> dict[str, object]:
    """Parse one repository workflow, without narrowing its top-level keys."""
    path = WORKFLOW_DIR / workflow_name
    # YAML 1.1 reads the `on:` trigger key as the boolean `True`, so the
    # document itself is not string-keyed. Only the mappings below are narrowed.
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(
        condition=isinstance(document, dict),
        message=f"{workflow_name} must parse to a mapping",
    )
    return typ.cast("dict[str, object]", document)


def workflow_env(workflow_name: str) -> dict[str, object]:
    """Return the workflow-level ``env`` mapping."""
    return _mapping(
        workflow_document(workflow_name).get("env"),
        f"{workflow_name} must declare workflow-level env",
    )


def jobs(workflow_name: str) -> dict[str, object]:
    """Load the jobs mapping from one repository workflow."""
    return _mapping(
        workflow_document(workflow_name).get("jobs"),
        f"{workflow_name} must declare jobs",
    )


def job(workflow_name: str, job_name: str) -> Job:
    """Return one named job from a repository workflow."""
    payload = _mapping(
        jobs(workflow_name).get(job_name),
        f"{workflow_name} must define {job_name}",
    )
    return typ.cast("Job", payload)


def steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the validated steps for one workflow job."""
    declared = job(workflow_name, job_name).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"{workflow_name}:{job_name} must declare steps",
    )
    return [
        typ.cast(
            "Step",
            _mapping(
                step, f"{workflow_name}:{job_name} step {index} must be a mapping"
            ),
        )
        for index, step in enumerate(typ.cast("list[object]", declared))
    ]


def step_inputs(step: Step, message: str) -> dict[str, object]:
    """Return the ``with`` mapping declared by one workflow step."""
    return _mapping(step.get("with"), message)


def cache_paths(step: Step, message: str) -> list[str]:
    """Return the paths a cache step owns, one per line."""
    declared = step_inputs(step, message).get("path")
    _require(
        condition=isinstance(declared, str),
        message=f"{message}: paths must be a newline-delimited string",
    )
    paths = [line.strip() for line in str(declared).splitlines() if line.strip()]
    # A cache step with no paths owns nothing, so every ownership assertion
    # over it would hold vacuously. Fail here instead of reporting a clean run.
    _require(
        condition=bool(paths),
        message=f"{message}: a cache step must declare at least one path",
    )
    return paths


def _steps_using(
    workflow_name: str, job_name: str, actions: cabc.Collection[str]
) -> list[Step]:
    """Return the steps of one job that invoke any of ``actions``."""
    wanted = frozenset(actions)
    return [
        step for step in steps(workflow_name, job_name) if step.get("uses") in wanted
    ]


def restore_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the cache restore steps declared by one job, in order."""
    return _steps_using(workflow_name, job_name, (CACHE_RESTORE,))


def save_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the cache save steps declared by one job, in order."""
    return _steps_using(workflow_name, job_name, (CACHE_SAVE,))


def cache_steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return every step of one job that owns a cached path."""
    return _steps_using(
        workflow_name, job_name, (CACHE_RESTORE, CACHE_SAVE, CACHE_PLAIN)
    )


def expand(manifest: cabc.Mapping[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Flatten a workflow-to-job-names manifest into per-job cases."""
    return [
        (workflow_name, job_name)
        for workflow_name, job_names in manifest.items()
        for job_name in job_names
    ]


def workflow_sources() -> list[tuple[str, str]]:
    """Return every workflow's name and source text."""
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
    ]
