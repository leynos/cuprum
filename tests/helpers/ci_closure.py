"""Enumerate every workflow a set of events can reach, calls included.

A pull-request prohibition is only as wide as the set of workflows it reads. A
workflow that declares only ``workflow_call`` still runs on a pull request when
a pull-request workflow calls it, and ``secrets: inherit`` hands it every
secret, so a contract that enumerates triggers alone cannot see it. Episodic
measured the hole: a called workflow curling CodeScene with the inherited token
passed thirteen contract tests. The set here is therefore the transitive
closure through same-repository ``uses:`` calls.

Two readings fail towards refusal rather than a quietly smaller set:

``on:``
    Read as a scalar, a sequence, or a mapping, under the string key or the
    boolean ``True`` YAML 1.1 turns an unquoted ``on`` into. A mapping-only
    reader stringifies ``on: [push, pull_request]`` into one unrecognized key
    and drops the workflow from every pull-request clause (mxd #563).
Local calls
    Matched by shape rather than by an enumerated prefix list: strip a leading
    ``./`` or ``$/`` and ask whether the rest is a file directly under this
    repository's workflow directory. A call of that shape which names no
    existing workflow is refused, since GitHub would not run a pull request
    that silently skipped it either.

``workflow_run``
    A workflow triggered by the completion of a reached workflow runs as a
    downstream run with the repository's secrets, so it is reached too. It is
    matched on the watched workflow's ``name:``, which is what GitHub matches.
Self-qualified calls
    ``leynos/cuprum/.github/workflows/x.yml@ref`` names this repository at a
    revision this contract cannot read, so it is refused rather than treated
    as remote and skipped.

Scope: the reachability question only. What a reached workflow may contain is
the business of the contract that consumes the closure.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_workflows import WORKFLOW_DIR, read_workflow

if typ.TYPE_CHECKING:
    from pathlib import Path

#: Events that run a workflow for a pull request. ``pull_request_target`` is
#: the more dangerous of the two: it runs with the base repository's secrets.
PULL_REQUEST_EVENTS: typ.Final = frozenset({"pull_request", "pull_request_target"})

#: The directory a same-repository reusable workflow must live in, as written
#: in a ``uses:`` value after any leading ``./`` or ``$/``.
_LOCAL_PREFIX = ".github/workflows/"

#: Leading spellings of a same-repository call. ``$/`` is read as local by
#: shape: if GitHub accepts it the callee runs, and reading it costs nothing
#: if GitHub does not.
_LOCAL_SPELLINGS = ("./", "$/")

#: This repository's qualified workflow path. A call through it names a ref
#: the contract cannot inspect, so it is refused.
SELF_QUALIFIED_PREFIX: typ.Final = "leynos/cuprum/.github/workflows/"

#: The extensions GitHub reads as workflows, compared case-insensitively.
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def triggers(document: dict[object, object], name: str) -> frozenset[str]:
    """Return the events one parsed workflow declares.

    Parameters
    ----------
    document : dict[object, object]
        The parsed workflow, as :func:`read_workflow` returns it.
    name : str
        The workflow's file name, for diagnostics.

    Returns
    -------
    frozenset[str]
        The declared event names.

    Notes
    -----
    Fails the contract when the workflow declares no trigger, declares it
    under both keys, or declares it in a shape GitHub does not accept.

    Examples
    --------
    >>> sorted(triggers({True: ["push", "pull_request"]}, "ci.yml"))
    ['pull_request', 'push']
    """
    keys = [key for key in ("on", True) if key in document]
    _require(
        condition=len(keys) == 1,
        message=f"{name} must declare its trigger exactly once, found {len(keys)}",
    )
    declared = document[keys[0]]
    match declared:
        case str():
            events: list[object] = [declared]
        case list():
            events = list(typ.cast("list[object]", declared))
        case dict():
            events = list(typ.cast("dict[object, object]", declared))
        case _:
            events = []
    _require(
        condition=bool(events) and all(isinstance(event, str) for event in events),
        message=f"{name} declares an unreadable trigger: {declared!r}",
    )
    return frozenset(typ.cast("list[str]", events))


def local_calls(document: dict[object, object], name: str) -> frozenset[str]:
    """Return the file names of the same-repository workflows one workflow calls.

    Parameters
    ----------
    document : dict[object, object]
        The parsed workflow.
    name : str
        The workflow's file name, for diagnostics.

    Returns
    -------
    frozenset[str]
        Workflow file names under the workflow directory.

    Notes
    -----
    Fails the contract when a local-shaped call names a nested path or
    carries a ref, neither of which GitHub accepts for a same-repository
    workflow.

    Examples
    --------
    >>> local_calls({"jobs": {"w": {"uses": "./.github/workflows/w.yml"}}}, "ci.yml")
    frozenset({'w.yml'})
    """
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return frozenset()
    called: set[str] = set()
    for job_name, job in typ.cast("dict[object, object]", jobs).items():
        reference = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(reference, str):
            continue
        path = reference.strip()
        _require(
            condition=not path.lower().startswith(SELF_QUALIFIED_PREFIX),
            message=(
                f"{name}:{job_name} calls this repository by its qualified name, "
                f"{reference!r}, at a revision this contract cannot read; call "
                "it as ./.github/workflows/<file> instead"
            ),
        )
        for spelling in _LOCAL_SPELLINGS:
            path = path.removeprefix(spelling)
        if not path.startswith(_LOCAL_PREFIX):
            continue
        callee = path.removeprefix(_LOCAL_PREFIX)
        _require(
            condition=bool(callee) and "/" not in callee and "@" not in callee,
            message=(
                f"{name}:{job_name} calls an unreadable local workflow {reference!r}"
            ),
        )
        called.add(callee)
    return frozenset(called)


def watched_workflows(document: dict[object, object], name: str) -> frozenset[str]:
    """Return the workflow names whose completion triggers this workflow.

    Returns
    -------
    frozenset[str]
        The names under ``on.workflow_run.workflows``; empty when the workflow
        has no ``workflow_run`` trigger.

    Examples
    --------
    >>> watched_workflows({True: {"workflow_run": {"workflows": ["CI"]}}}, "x.yml")
    frozenset({'CI'})
    """
    if "workflow_run" not in triggers(document, name):
        return frozenset()
    declared = document.get("on", document.get(True))
    settings = typ.cast("dict[object, object]", declared).get("workflow_run")
    watched = settings.get("workflows") if isinstance(settings, dict) else None
    _require(
        condition=isinstance(watched, list)
        and bool(watched)
        and all(isinstance(item, str) for item in watched),
        message=f"{name} declares workflow_run without a readable workflows list",
    )
    return frozenset(typ.cast("list[str]", watched))


def _display_name(document: dict[object, object], name: str) -> str:
    """Return the name GitHub reports for a workflow: ``name:`` or its path."""
    declared = document.get("name")
    return declared if isinstance(declared, str) else f"{_LOCAL_PREFIX}{name}"


def workflow_names(directory: Path = WORKFLOW_DIR) -> list[str]:
    """Return every workflow file name in ``directory``, whatever its case.

    Returns
    -------
    list[str]
        Sorted file names with a ``.yml`` or ``.yaml`` suffix in any case.
    """
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.suffix.lower() in _WORKFLOW_SUFFIXES
    )


def reachable(
    events: frozenset[str], directory: Path = WORKFLOW_DIR
) -> dict[str, dict[object, object]]:
    """Return every workflow the events start, and everything those call.

    Parameters
    ----------
    events : frozenset[str]
        Trigger names that start the traversal.
    directory : Path
        The workflow directory; the reader tests pass a temporary one.

    Returns
    -------
    dict[str, dict[object, object]]
        Each reached workflow's file name mapped to its parsed document.

    Notes
    -----
    Fails the contract when a reached workflow calls a local workflow that
    does not exist.
    """
    documents = {
        name: read_workflow(directory / name) for name in workflow_names(directory)
    }
    pending = [
        name
        for name, document in documents.items()
        if triggers(document, name) & events
    ]
    reached: dict[str, dict[object, object]] = {}
    while pending:
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached[current] = documents[current]
            for callee in local_calls(documents[current], current):
                _require(
                    condition=callee in documents,
                    message=f"{current} calls {callee}, which is not a workflow here",
                )
                pending.append(callee)
        # Downstream runs: a workflow watching a reached one runs after it,
        # with secrets, so it joins the closure and its own calls follow.
        names = {_display_name(document, name) for name, document in reached.items()}
        pending = [
            name
            for name, document in documents.items()
            if name not in reached and watched_workflows(document, name) & names
        ]
    return reached
