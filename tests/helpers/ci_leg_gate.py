"""Read step guards through the flag that switches the pre-release leg off.

GitHub cannot skip one leg of a matrix: a job-level `if:` cannot read
`matrix`, and `exclude` runs before `include`, so it cannot remove an
include-only leg. `ci.yml`'s `typecheck-test` therefore computes one job-level
flag, `LEG_RUNS`, false only for the experimental leg on a pull request, and
every step carries `env.LEG_RUNS == 'true'` as its last conjunct.

Contracts that compare a step's own guard in that job read it through
:func:`ungated`, which strips exactly that trailing conjunct and fails when it
is absent. The strip is exact rather than tolerant so that no other guard can
hide behind it. Scope: `typecheck-test` only; every other job's guard is
returned unchanged.
"""

from __future__ import annotations

import typing as typ

#: The one job whose steps carry the leg flag.
GATED_LEG_JOB: typ.Final = ("ci.yml", "typecheck-test")

#: The conjunct every step of that job must end with.
LEG_GATE: typ.Final = "env.LEG_RUNS == 'true'"

#: The job-level flag, false only for the experimental leg on a pull request.
LEG_FLAG_EXPRESSION: typ.Final = (
    "${{ !(matrix.experimental && github.event_name == 'pull_request') }}"
)


def normalized(condition: object) -> str:
    """Collapse the whitespace in a step guard so layout cannot decide a match.

    Guards are compared as text, and YAML folding or a reflow can change
    their spacing without changing what they mean.

    Parameters
    ----------
    condition : object
        A step's ``if:`` value as parsed, or ``None`` when the step has none.

    Returns
    -------
    str
        The guard with every run of whitespace collapsed to one space and the
        ends trimmed, or ``""`` for ``None``, so an unguarded step compares
        equal to an empty guard.

    Examples
    --------
    >>> normalized("always()  &&   env.LEG_RUNS == 'true' ")
    "always() && env.LEG_RUNS == 'true'"
    >>> normalized(None)
    ''
    """
    return " ".join(str(condition).split()) if condition is not None else ""


def ungated(workflow_name: str, job_name: str, condition: object) -> str:
    """Return a step's guard without the leg flag's trailing conjunct.

    Returns
    -------
    str
        The normalized guard as it would read without the flag, or ``""`` when
        the flag is the whole guard. Guards outside the gated job are returned
        normalized and otherwise unchanged.

    Raises
    ------
    AssertionError
        If a step of the gated job does not end with the flag.

    Examples
    --------
    >>> ungated("ci.yml", "typecheck-test", "always() && env.LEG_RUNS == 'true'")
    'always()'
    """
    text = normalized(condition)
    if (workflow_name, job_name) != GATED_LEG_JOB:
        return text
    if text == LEG_GATE:
        return ""
    suffix = f" && {LEG_GATE}"
    if not text.endswith(suffix):
        message = (
            f"{workflow_name}:{job_name} step guard {text!r} must end with {LEG_GATE!r}"
        )
        raise AssertionError(message)
    return text.removesuffix(suffix)
