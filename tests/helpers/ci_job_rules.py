"""Rules about a job that are not about which runner it selects.

Split from `ci_placement`, which models `runs-on`. These three read the other
declarations a placement contract depends on: the ceiling that bounds a job,
whether its own guard lets it run, and which context properties a declaration
reads, which is what the job-name stability rule compares.

Each is a predicate rather than an assertion inside a contract, so it can be
driven directly over the values this repository does not happen to declare.
That is not a stylistic preference: a rule parametrized over correct sources
passes whether or not it discriminates, and two of these survived their own
mutations until they were extracted and tested this way.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_placement import EXPRESSION, require
from tests.helpers.ci_workflows import job

#: One `${{ ... }}` span. Non-greedy to the first `}}`, because `[^}]*` stops
#: at the `}` inside `format('{0}', matrix.os)` and reports no reference at all.
_EXPRESSION_SPAN = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
#: A quoted literal inside an expression body. Stripped before references are
#: read, so a label such as `'ubuntu-22.04'` is not mistaken for a property
#: path on the strength of its dot.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
#: A dotted context property such as `matrix.os` or
#: `github.event.pull_request.head.repo.fork`. A bare function name such as
#: `format` carries no dot and is correctly not a reference.
_PROPERTY = re.compile(r"[A-Za-z_][\w-]*(?:\.[\w-]+)+")
#: Expressions a job-level ``if:`` may not reduce to. A job that cannot run
#: satisfies every declaration-reading rule while gating nothing.
#: GitHub Actions evaluates `false`, numeric zero, the empty string and `null`
#: as falsy, and **a non-empty string as truthy**, so the quoted `'false'`
#: belongs on the other side of this line rather than in it.
_NEVER_RUNS: typ.Final = frozenset({"false", "0", "0.0", "''", '""', "null"})


def ceiling(workflow_name: str, job_name: str) -> int:
    """Return a job's declared ``timeout-minutes``, refusing an unusable value.

    GitHub requires a positive integer. Two spellings pass an ``isinstance``
    check and should not: YAML's ``true`` is an ``int`` to Python, and zero and
    negatives are integers that bound nothing.

    Extracted from the contract so the rule can be driven directly. Asserted
    against this repository's own correct workflows it would pass whether or
    not it discriminated, and a mutation from ``type(...) is int`` back to
    ``isinstance`` survived until this existed.

    Returns
    -------
    int
        The declared ceiling, guaranteed a positive, non-boolean integer.

    Notes
    -----
    Fails the contract, through :func:`_require`, when the value is absent,
    boolean, a non-integer, or not positive.
    """
    declared = job(workflow_name, job_name).get("timeout-minutes")
    where = f"{workflow_name}:{job_name}"
    require(
        condition=type(declared) is int,
        message=(
            f"{where} must declare an integer timeout-minutes, got {declared!r}; "
            "note YAML's `true` is an int to `isinstance`"
        ),
    )
    value = typ.cast("int", declared)
    require(
        condition=value > 0,
        message=f"{where} must declare a positive timeout-minutes, got {value!r}",
    )
    return value


def references(value: object) -> frozenset[str]:
    """Return every context property a declaration reads.

    Property paths rather than whole expression bodies. A name built with
    ``${{ format('{0}', matrix.os) }}`` reads ``matrix.os`` and has to be
    comparable with a ``runs-on`` reading the same key; returning the body made
    that comparison miss entirely, so an unstable name passed the stability
    rule.

    Returns
    -------
    frozenset[str]
        Every dotted context property the value reads, across every
        ``${{ ... }}`` span, with quoted literals excluded.
    """
    if not isinstance(value, str):
        return frozenset()
    found: set[str] = set()
    for body in _EXPRESSION_SPAN.findall(value):
        found.update(_PROPERTY.findall(_QUOTED.sub(" ", body)))
    return frozenset(found)


def never_runs(workflow_name: str, job_name: str) -> bool:
    """Report whether a job's own ``if:`` makes it unreachable.

    A constant-false guard leaves every declaration intact and runs nothing,
    which is exactly the state a placement contract must refuse rather than
    report as compliant.

    Returns
    -------
    bool
        Whether the job's own ``if:`` reduces to a constant false.
    """
    condition = job(workflow_name, job_name).get("if")
    if not isinstance(condition, str):
        return False
    text = " ".join(condition.split())
    expression = EXPRESSION.match(text)
    if expression is not None:
        text = " ".join(expression.group("body").split())
    lowered = text.lower()
    if lowered in _NEVER_RUNS:
        return True
    # A containment test would accept `false && matrix.target == 'x'`, so the
    # leading clause is matched explicitly, and for every falsy literal rather
    # than only `false`: `null && ...` and `0 && ...` are equally unreachable.
    return any(lowered.startswith(f"{literal} &&") for literal in _NEVER_RUNS)
