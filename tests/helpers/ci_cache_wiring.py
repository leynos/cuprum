"""Pure rules for wiring that must not drift while the cache experiment runs.

These are the predicates the mutation tests drive directly. A rule asserted
only against today's correct workflows passes whether or not it discriminates,
so each is a function a test can feed a seeded fault to.

The rules cover three ways a compiler cache stops being trustworthy without
any visible failure: a step overriding the wrapper sccache installed, a cache
namespace taken from a pull-request-controlled value, and a workflow triggered
by ``pull_request_target`` running repository code with a write token.
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: A namespace is a fixed identifier, never an expression.
NAMESPACE = re.compile(r"[a-z0-9][a-z0-9._-]{0,40}")
#: The trigger that runs a workflow in the base repository's context.
NEGATIVE_TRIGGER = "pull_request_target"
#: The evidence action and the cache actions the experiment must not place in
#: a privileged context. `actions/cache@` restores as well as saves, so it is
#: matched alongside the restore-only form.
EVIDENCE_ACTION = "./.github/actions/compiler-cache-evidence"
CACHE_RESTORE_ACTION = "actions/cache/restore@"
CACHE_ACTION = "actions/cache@"


def wrapper_overrides(
    job_steps: cabc.Iterable[cabc.Mapping[str, object]],
) -> list[str]:
    """Return the steps that set ``RUSTC_WRAPPER`` themselves.

    Returns
    -------
    list[str]
        One entry per offending step, named. The setup-sccache action exports
        the wrapper through ``GITHUB_ENV``; a step that also sets it can point
        Cargo at a binary the action never verified, or at nothing at all.
    """
    offenders: list[str] = []
    for step in job_steps:
        declared = step.get("env")
        named = isinstance(declared, dict) and "RUSTC_WRAPPER" in declared
        scripted = "RUSTC_WRAPPER=" in str(step.get("run", ""))
        if named or scripted:
            offenders.append(str(step.get("name", step.get("uses", "?"))))
    return offenders


def unsafe_namespaces(
    job_steps: cabc.Iterable[cabc.Mapping[str, object]],
) -> list[str]:
    """Return every setup-sccache namespace that is not a fixed identifier.

    Returns
    -------
    list[str]
        One entry per offending call. An empty namespace is the documented
        default and is not an offence; an expression, a branch name, or any
        other pull-request-controlled value is.
    """
    offenders: list[str] = []
    for step in job_steps:
        if step.get("uses") != "./.github/actions/setup-sccache":
            continue
        declared = step.get("with")
        if not isinstance(declared, dict):
            continue
        value = typ.cast("dict[str, object]", declared).get("namespace")
        if value is None or not value:
            continue
        if not NAMESPACE.fullmatch(str(value)):
            offenders.append(f"{step.get('name')}: {value}")
    return offenders


def privileged_cache_workflows(
    sources: cabc.Iterable[tuple[str, cabc.Mapping[object, object]]],
) -> list[str]:
    """Return ``pull_request_target`` workflows that also touch the experiment.

    A ``pull_request_target`` run executes in the base repository's context
    with a writable token, so it must never carry the compiler-cache evidence
    steps or a compiler-cache restore: both would run repository code, or read
    a store, under privileges a pull request is not trusted with. The trigger
    itself is legitimate elsewhere in the estate; this rule is about the
    combination.

    Returns
    -------
    list[str]
        The offending workflow file names.
    """
    offenders: list[str] = []
    for name, document in sources:
        triggers = document.get("on", document.get(True))
        if isinstance(triggers, (dict, list, tuple)):
            privileged = NEGATIVE_TRIGGER in triggers
        else:
            privileged = triggers == NEGATIVE_TRIGGER
        if not privileged:
            continue
        text = str(document)
        forbidden = (EVIDENCE_ACTION, CACHE_RESTORE_ACTION, CACHE_ACTION)
        if any(token in text for token in forbidden):
            offenders.append(name)
    return offenders
