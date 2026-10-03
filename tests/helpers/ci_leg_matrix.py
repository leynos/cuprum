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

:func:`admits` resolves a clause over the leg alone, so a value a leg does not
carry — an unmodellable ``steps.`` output, or the event name — is treated as
satisfied and the set of admitted legs is a **superset** of the legs that
really run. That direction is the safe one for the cache-ownership caller,
which asks "could this save step fire on this leg" and must not drop a writer.

It is the wrong direction for the caller that asks "does this step run on a
pull request". There, an event clause is exactly what decides the answer, and
reading ``github.event_name == 'push'`` as satisfied would certify a suite on a
lane that never runs it. That caller needs the clause evaluated rather than
trusted, which the leg alone cannot do: :func:`admits_event` folds in the event
name and refuses the clauses it still cannot settle. The split is the point —
`admits` stays a conservative superset over legs, and the event-aware reading
that the pull-request question actually needs is stated beside it rather than
weakened into it.

The two readings are also visibly different at the call site, which is the
point of keeping both rather than one flag on a single function:

>>> admits_event(
...     "matrix.python-suite", {"python-suite": True}, "pull_request", subject="s"
... )
True
>>> admits_event(
...     "github.event_name == 'push'", {"python-suite": True}, "pull_request",
...     subject="s",
... )
False
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.ci_workflows import job

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "MATRIX_CLAUSE_PATTERN",
    "admits",
    "admits_event",
    "clauses",
    "matrix_legs",
)

#: A clause that reduces to ``matrix.<key>`` alone, and the value it compares
#: against when the clause is a comparison rather than a bare truthiness test.
#: Hyphens are part of the key grammar, as in ``matrix.python-suite``, so they
#: belong in the character class; a pattern without them matches neither the
#: hyphenated key nor its own tail, which is how a guard gated on the
#: typecheck-only leg reads as gated on nothing.
#:
#: Public because a caller resolving *more* than the leg has to recognize this
#: clause before replacing it with its own evaluation; :func:`admits_event`
#: does exactly that.
MATRIX_CLAUSE_PATTERN: typ.Final = re.compile(
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
        would then be answered from a leg GitHub never schedules. An
        ``include`` entry that is not a mapping is refused for the same
        reason: dropping it reports fewer legs than the workflow declares, and
        a list that is entirely malformed would report none at all, which
        makes every "some leg admits this" question vacuously false rather
        than loud. Both refusals name the workflow, the job, and (for an
        entry) its index, so the reader is extended rather than trusted when a
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
    legs = typ.cast("list[object]", include)
    for index, leg in enumerate(legs):
        require(
            condition=isinstance(leg, dict),
            message=(
                f"{workflow_name}:{job_name} declares a matrix `include` entry "
                f"at index {index} that is not a mapping; dropping it would "
                "report fewer legs than the workflow declares, and a list of "
                "such entries would report none"
            ),
        )
    return [typ.cast("dict[str, object]", leg) for leg in legs]


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
    match = MATRIX_CLAUSE_PATTERN.match(clause.strip())
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
    return all(_admitted_clause(clause, leg) for clause in clauses(condition))


def clauses(condition: str) -> list[str]:
    """Split a guard into its top-level ``&&`` clauses.

    A ``${{ ... }}`` wrapper is unwrapped first: GitHub accepts the expression
    syntax in ``if:``, and a guard carrying it would otherwise reduce to one
    unmodellable clause and admit every leg. The step guards in this repository
    are mostly bare, which is exactly why the wrapped form has to be handled
    rather than assumed away.

    Public because a reader that resolves a guard against more than the leg —
    :func:`admits_event` resolves the event name too — has to walk the same
    clauses `admits` does. Splitting them twice would let the two readers
    disagree about where one clause ends and the next begins.

    Parameters
    ----------
    condition : str
        A step's ``if:`` value, with or without the expression wrapper.

    Returns
    -------
    list of str
        The clauses, each stripped of surrounding whitespace, with the wrapper
        removed and empty clauses dropped.

    Examples
    --------
    >>> clauses("${{ matrix.python-suite && env.LEG_RUNS == 'true' }}")
    ['matrix.python-suite', "env.LEG_RUNS == 'true'"]
    """
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return [clause.strip() for clause in text.split("&&") if clause.strip()]


#: A clause of the shape ``github.event_name == 'pull_request'``, and the event
#: it compares against. Only truthiness so far: a bare ``github.event_name``,
#: or a ``!=``, is a guard this reader does not model, and treating an
#: unrecognized shape as satisfied is the error the function exists to refuse.
_EVENT_CLAUSE: typ.Final = re.compile(
    r"\Agithub\.event_name\s*==\s*'(?P<event>[^']*)'\Z", re.IGNORECASE
)

#: The status functions GitHub evaluates from the run's own history rather
#: than from context. They answer "should this step run given what came
#: before", so they are satisfied on every leg and every event; admitting one
#: is a reading, not a concession. Anything outside this set and the two
#: clause forms above is a value no leg and no event name settles.
_STATUS_FUNCTION: typ.Final = re.compile(
    r"\A(?:always|success|failure|cancelled)\s*\(\s*\)\Z", re.IGNORECASE
)


def admits_event(
    condition: str,
    leg: cabc.Mapping[str, object],
    event: str,
    *,
    subject: str,
) -> bool:
    """Report whether a guard admits a leg *and* runs on the named event.

    `admits` leaves the event clauses unread, which is the safe direction for
    the cache-ownership caller and the wrong one for the pull-request question
    this feeds. See the module docstring for why the two contracts differ.

    Parameters
    ----------
    condition : str
        A step's ``if:`` value, as `ci_leg_gate.ungated` returns it, so the
        trailing leg flag is already stripped.
    leg : Mapping of str to object
        One leg, as :func:`matrix_legs` returns it.
    event : str
        The event name to decide against, such as ``"pull_request"``.
    subject : str
        What the guard belongs to, named in the failure so a reader is sent to
        the step rather than to this helper.

    Returns
    -------
    bool
        Whether every clause holds. Matrix clauses are read over the leg, event
        comparisons against ``event``, and the status functions as satisfied.

    Raises
    ------
    AssertionError
        When a clause is neither a matrix reference, an event-name comparison,
        nor a status function. Whether the step runs is not decidable from the
        leg, and treating the clause as satisfied would certify a lane that may
        never run it; the shape is reported instead of guessed at.

    Notes
    -----
    A clause is admitted here by being *recognized*, not by escaping
    recognition, so an unrecognized shape is refused rather than read as
    satisfied. Only the top-level ``&&`` split :func:`clauses` performs is
    modelled; a guard whose top level is ``||`` reduces to one clause matching
    no form and is refused rather than read as one of its branches.
    """  # ruff: ignore[docstring-extraneous-exception] - the refusal is raised by require()
    for clause in clauses(condition):
        event_match = _EVENT_CLAUSE.match(clause)
        if event_match is not None:
            if event_match.group("event") != event:
                return False
            continue
        require(
            condition=(
                MATRIX_CLAUSE_PATTERN.match(clause) is not None
                or _STATUS_FUNCTION.match(clause) is not None
            ),
            message=(
                f"{subject} is guarded by {clause!r}, which reads a value this "
                f"reader cannot resolve for the {event!r} event. Whether the "
                "step runs on a pull request is not decidable from the leg "
                "alone, and treating the clause as satisfied would certify a "
                "lane that may never run it; model the term or drop it"
            ),
        )
    # Every clause is now one of the three recognized forms, and `admits`
    # settles the matrix ones over the leg while reading the status functions
    # as satisfied. The event clauses are already known to hold, since a
    # mismatch returned above, so re-reading them there as satisfied is exact.
    return admits(condition, leg)
