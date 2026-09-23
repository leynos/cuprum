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
    ``./`` and ask whether the rest is a file directly under this repository's
    workflow directory. A call of that shape which names no existing workflow
    is refused, since GitHub would not run a pull request that silently skipped
    it either.

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
#: in a ``uses:`` value after any leading ``./``.
_LOCAL_PREFIX = ".github/workflows/"

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
        path = reference.strip().removeprefix("./")
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
    return reached
