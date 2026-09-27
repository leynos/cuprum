"""Resolve which matrix legs a guarded step runs on.

`ci_leg_gate` strips the pre-release flag from a step's guard so a contract can
compare what is left, but it stops one conjunct short of the question the guard
exists to answer: *which* legs does the remaining condition admit? A rule that
only reads the guard's text cannot tell "the suite runs on a pull-request leg"
from "the suite runs on a leg a pull request never schedules", and those are
different claims about whether CI collects anything.

That question has two halves, and they are resolved in two different places:

* which legs exist, and what each one sets — GitHub expands ``include``, so the
  legs are the entries of that list and nothing else (`matrix_legs`);
* whether a condition admits a leg — evaluated over the leg's own values, which
  is what the guard's ``matrix.<key>`` references read (`admits`).

Publishing both here rather than in a private helper keeps one answer to "what
does this guard mean" for every reader that needs it: the cache-ownership rule
asks whether a *save* step fires on a leg, and the selection contract asks
whether the suite *runs* on one. Both questions are the same evaluation.

An unmodellable clause is not a failure. A guard naming
``steps.tool-cache.outputs.cache-hit`` reads a value no matrix leg settles, and
a rule that refused it would reject ordinary workflows. Those clauses are
treated as satisfied, so the set of admitted legs is a **superset** of the legs
that really run. That direction is the safe one for every caller here: a
contract asking "does this step run on a pull-request leg" is answered yes only
when some leg's own values make it so, and an unknown clause can never turn a
leg that genuinely runs into one that appears not to.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_placement import require
from tests.helpers.ci_workflows import job

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "admits",
    "matrix_legs",
)

#: A clause that reduces to ``matrix.<key>`` alone, and the value it compares
#: against when the clause is a comparison rather than a bare truthiness test.
#: Hyphens are part of the key grammar, as in ``matrix.python-suite``, so they
#: belong in the character class; a pattern without them matches neither the
#: hyphenated key nor its own tail, which is how a guard gated on the
#: typecheck-only leg reads as gated on nothing.
_MATRIX_CLAUSE = re.compile(
    r"\Amatrix\.(?P<key>[a-z0-9-]+)"
    r"(?:\s*(?P<operator>==|!=)\s*'(?P<literal>[^']*)')?\Z",
    re.IGNORECASE,
)


def matrix_legs(workflow_name: str, job_name: str) -> list[dict[str, object]]:
    """Expand one job's matrix into the legs it runs as.

    Parameters
    ----------
    workflow_name : str
        Workflow file name, such as ``"ci.yml"``.
    job_name : str
        Job key within that workflow.

    Returns
    -------
    list of dict
        One mapping per ``include`` entry, or a single empty mapping when the
        job declares no matrix, so callers can iterate uniformly over a job
        that simply has no legs to distinguish.

    Raises
    ------
    AssertionError
        When the job declares a matrix this reader cannot expand. A ``matrix``
        that is present but not an ``include`` list is refused rather than
        read as "no matrix": that shape would report one leg for a job that
        really runs several, and every "some leg admits this" question over it
        would then be answered from a leg GitHub never schedules. The refusal
        is by name, so the reader is extended rather than trusted when a
        workflow adopts a shape it does not model.

    Notes
    -----
    A job with no ``strategy`` at all, or with a ``strategy`` declaring no
    ``matrix``, genuinely has one leg: its steps run once, and there is no
    value for a guard to read. That is the empty mapping, which
    :func:`admits` treats as admitting a matrix-free guard.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    strategy = job(workflow_name, job_name).get("strategy")
    if not isinstance(strategy, dict):
        return [{}]
    matrix = typ.cast("dict[str, object]", strategy).get("matrix")
    if not isinstance(matrix, dict):
        return [{}]
    include = typ.cast("dict[str, object]", matrix).get("include")
    require(
        condition=isinstance(include, list),
        message=(
            f"{workflow_name}:{job_name} declares a matrix this reader cannot "
            "expand; only `include` lists are supported, and reading any other "
            "shape as a single leg would answer leg questions from a leg no "
            "event schedules"
        ),
    )
    return [
        typ.cast("dict[str, object]", leg)
        for leg in typ.cast("list[object]", include)
        if isinstance(leg, dict)
    ]


def _admitted_clause(clause: str, leg: cabc.Mapping[str, object]) -> bool:
    """Report whether one ``&&``-joined clause admits one leg.

    Parameters
    ----------
    clause : str
        One clause of a step guard, already split on ``&&``.
    leg : Mapping of str to object
        The leg to evaluate the clause against.

    Returns
    -------
    bool
        Whether the clause holds on this leg. ``True`` for a clause naming no
        matrix key — a ``steps.`` output, an ``always()`` call — because the
        caller wants a superset of the legs that run; See the module
        docstring. ``False`` for a bare ``matrix.<key>`` the leg does not
        carry, since GitHub reads an absent property as falsy.
    """
    match = _MATRIX_CLAUSE.match(clause.strip())
    if match is None:
        return True
    key = match.group("key")
    if key not in leg:
        # The leg does not carry the key at all. `matrix.experimental` is
        # absent from every leg that is not pre-release, and GitHub reads an
        # absent property as null, which is falsy — so a bare reference is
        # false and an equality test against a non-empty literal is too.
        # Returning `True` here would admit every leg for a guard that really
        # excludes them, which is the one direction that makes a "no leg
        # admits this" failure go unnoticed.
        return False
    actual = leg[key]
    operator = match.group("operator")
    if operator is None:
        return bool(actual)
    equal = actual == match.group("literal")
    return equal if operator == "==" else not equal


def admits(condition: object, leg: cabc.Mapping[str, object]) -> bool:
    """Report whether a step guard admits one matrix leg.

    Parameters
    ----------
    condition : object
        A step's ``if:`` value as parsed, or ``None`` when the step has none.
    leg : Mapping of str to object
        One leg, as :func:`matrix_legs` returns it, or the empty mapping for a
        job that has no matrix.

    Returns
    -------
    bool
        Whether the guard's matrix clauses all hold on that leg. An unguarded
        step and a guard naming no matrix key both admit every leg.

    Notes
    -----
    Only ``&&`` is evaluated. A guard whose top level is ``||`` is a union the
    clause splitter would read as one long unmodellable clause, which returns
    ``True`` — admitting the leg, the safe direction described in the module
    docstring. No guard in this repository takes that shape; a contract that
    needed one would have to model the disjunction rather than trust this.

    Examples
    --------
    >>> admits("matrix.python-suite && env.LEG_RUNS == 'true'", {"python-suite": True})
    True
    >>> admits("matrix.python-suite && env.LEG_RUNS == 'true'", {"python-suite": False})
    False
    """
    if not isinstance(condition, str):
        return True
    return all(_admitted_clause(clause, leg) for clause in _split_clauses(condition))


def _split_clauses(condition: str) -> list[str]:
    """Split a guard into its top-level ``&&`` clauses.

    A ``${{ ... }}`` wrapper is unwrapped first: GitHub accepts the expression
    syntax in ``if:``, and a guard carrying it would otherwise reduce to one
    unmodellable clause and admit every leg. The step guards in this repository
    are mostly bare, which is exactly why the wrapped form has to be handled
    rather than assumed away.

    Parameters
    ----------
    condition : str
        A step's ``if:`` value, with or without the expression wrapper.

    Returns
    -------
    list of str
        The clauses, trimmed, with the wrapper removed and empty clauses
        dropped.
    """
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return [clause for clause in text.split("&&") if clause.strip()]
