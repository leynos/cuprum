"""Narrow YAML readers for the CI workflow contract tests.

The manifests that say which jobs and caches are intended live in
``tests/helpers/ci_runners.py``; this module only reads the workflows back.
Every accessor validates the shape it narrows, so a malformed workflow fails
with a named diagnostic rather than an opaque ``TypeError`` deep in a test.

Reading and parsing are fallible too, and every query in the contract helpers
reaches the filesystem through the two readers here, ``read_workflow`` and
``read_source``. They translate an I/O or YAML failure into an
``AssertionError`` naming the file, so a contract over an unreadable workflow
fails as a contract, citing the file, rather than raising an unattributed
parser error from whichever query happened to touch it first.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from tests.helpers import strict_yaml

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


def read_source(path: Path) -> str:
    """Return one workflow's source text, naming the file if it cannot be read.

    Parameters
    ----------
    path : Path
        The workflow file to read.

    Returns
    -------
    str
        The file's UTF-8 text.

    Raises
    ------
    AssertionError
        If the file cannot be read, naming the file and the ``OSError``.

    Examples
    --------
    >>> "jobs:" in read_source(WORKFLOW_DIR / "ci.yml")
    True
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        message = f"{path.name} could not be read: {error}"
        raise AssertionError(message) from error


def read_workflow(path: Path) -> dict[object, object]:
    """Parse one workflow file, without narrowing its top-level keys.

    Parameters
    ----------
    path : Path
        The workflow file to parse.

    Returns
    -------
    dict[object, object]
        The parsed document, whose trigger key YAML 1.1 reads as ``True``.

    Notes
    -----
    Fails the contract when the file cannot be read, is not valid YAML,
    declares a mapping key twice, or does not parse to a mapping, naming the
    file in each case.

    Examples
    --------
    >>> "jobs" in read_workflow(WORKFLOW_DIR / "ci.yml")
    True
    """
    document = strict_yaml.load(read_source(path), path.name)
    # YAML 1.1 reads the `on:` trigger key as the boolean `True`, so the
    # document is genuinely not string-keyed and the return type says so.
    # Claiming `dict[str, object]` here would be a false contract that hides
    # the one key a caller cannot reach by name. Callers that need a
    # string-keyed mapping narrow one of its values through `_mapping`.
    _require(
        condition=isinstance(document, dict),
        message=f"{path.name} must parse to a mapping",
    )
    return typ.cast("dict[object, object]", document)


def workflow_document(workflow_name: str) -> dict[object, object]:
    """Parse one repository workflow, without narrowing its top-level keys."""
    return read_workflow(WORKFLOW_DIR / workflow_name)


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


def job_env(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return the ``env`` mapping one job declares for all of its steps."""
    return _mapping(
        job(workflow_name, job_name).get("env"),
        f"{workflow_name}:{job_name} must declare job-level env",
    )


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


def _invokes(step: Step, *, uses: str, prefix: bool) -> bool:
    """Report whether a step's ``uses:`` names the action under test."""
    declared = str(step.get("uses", ""))
    return declared.startswith(uses) if prefix else declared == uses


def single_step_position_using(
    workflow_name: str,
    job_name: str,
    *,
    uses: str,
    prefix: bool = False,
) -> int:
    """Return the position of the single step of a job that invokes an action.

    Exactly one, not merely the first: a job that invokes the same action twice
    runs its work twice, and a contract that reported the first would call that
    clean. Every caller wants the same rule, so the match and its diagnostic
    live here rather than being restated in each contract module.

    The position exists so a contract can assert that one step runs before
    another — a producer and its reader in the same job are only ordered by
    their order in the file, which nothing else checks.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow under ``.github/workflows``.
    job_name : str
        Job the step is expected in.
    uses : str
        The ``uses:`` value to match.
    prefix : bool
        Match a ``uses:`` that starts with ``uses`` rather than equalling it.

    Returns
    -------
    int
        Zero-based position of the matching step among the job's steps.
    """
    matched = [
        index
        for index, step in enumerate(steps(workflow_name, job_name))
        if _invokes(step, uses=uses, prefix=prefix)
    ]
    _require(
        condition=len(matched) == 1,
        message=(
            f"{workflow_name}:{job_name} must invoke {uses!r} exactly once, "
            f"found {len(matched)}"
        ),
    )
    return matched[0]


def single_step_using(
    workflow_name: str,
    job_name: str,
    *,
    uses: str,
    prefix: bool = False,
) -> Step:
    """Return the single step of a job that invokes a named action.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow under ``.github/workflows``.
    job_name : str
        Job the step is expected in.
    uses : str
        The ``uses:`` value to match.
    prefix : bool
        Match a ``uses:`` that starts with ``uses`` rather than equalling it.

    Returns
    -------
    Step
        The one matching step.
    """
    position = single_step_position_using(
        workflow_name, job_name, uses=uses, prefix=prefix
    )
    return steps(workflow_name, job_name)[position]


def expand(manifest: cabc.Mapping[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Flatten a workflow-to-job-names manifest into per-job cases."""
    return [
        (workflow_name, job_name)
        for workflow_name, job_names in manifest.items()
        for job_name in job_names
    ]


def workflow_sources(directory: Path = WORKFLOW_DIR) -> list[tuple[str, str]]:
    """Return every workflow's name and source text.

    Parameters
    ----------
    directory : Path
        The directory to sweep. It defaults to this repository's workflows;
        the loader tests pass a temporary one to show that an unreadable file
        fails by name.

    Returns
    -------
    list[tuple[str, str]]
        Each ``*.yml`` file's name and text, sorted by path. Every read goes
        through ``read_source``, so an unreadable file fails by name.

    Raises
    ------
    AssertionError
        If ``directory`` is not a readable directory or holds no workflow.
        ``Path.glob`` returns nothing for a missing directory, and an empty
        sweep would satisfy every "no workflow does X" contract vacuously.
    """
    _require(
        condition=directory.is_dir(),
        message=f"{directory} is not a workflow directory",
    )
    try:
        paths = sorted(directory.glob("*.yml"))
    except OSError as error:
        message = f"{directory} could not be listed: {error}"
        raise AssertionError(message) from error
    _require(condition=bool(paths), message=f"{directory} holds no workflow")
    return [(path.name, read_source(path)) for path in paths]


def _job_run_scripts(workflow_name: str, job_name: str) -> list[tuple[str, str]]:
    """Return one job's ``(step index, script)`` pairs for its ``run:`` steps.

    A reusable-workflow call declares ``uses:`` instead of ``steps:``, so it
    holds no ``run:`` script and contributes nothing.

    Parameters
    ----------
    workflow_name : str
        Workflow file name, as ``workflow_sources`` reports it.
    job_name : str
        Job key within that workflow.

    Returns
    -------
    list[tuple[str, str]]
        One pair per ``run:`` step, in declaration order.
    """
    if not isinstance(job(workflow_name, job_name).get("steps"), list):
        return []
    return [
        (str(index), script)
        for index, step in enumerate(steps(workflow_name, job_name))
        if isinstance(script := step.get("run"), str)
    ]


def run_scripts(
    directory: Path = WORKFLOW_DIR,
) -> list[tuple[str, str, str, str]]:
    """Return every ``run:`` script, flattened with the location that holds it.

    The nested walk over workflows, jobs, and steps otherwise recurs in every
    contract that asks a question of all of them at once — the selector guard
    asks which step runs ``make test-python``, but the same shape answers any
    "does a step do X anywhere" question. Returning the location alongside the
    script lets a caller name what it found without re-walking the manifest.

    Parameters
    ----------
    directory : Path
        The directory to sweep, as ``workflow_sources`` takes it.

    Returns
    -------
    list[tuple[str, str, str, str]]
        One ``(workflow, job, step index, script)`` tuple per ``run:`` step,
        in file order, with the step index rendered as a string so the location
        reads the same in a message as it does in the YAML. Steps that declare
        no ``run:`` — ``uses:`` steps, and any step keyed by something else —
        contribute nothing.
    """
    found: list[tuple[str, str, str, str]] = []
    for workflow_name, _source in workflow_sources(directory):
        for job_name in jobs(workflow_name):
            found.extend(
                (workflow_name, job_name, index, script)
                for index, script in _job_run_scripts(workflow_name, job_name)
            )
    return found
