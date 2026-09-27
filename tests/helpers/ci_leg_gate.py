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

The two readings compose into the question a caller usually has — "would this
job run that command on a pull request?" — which is :func:`pull_request_legs`:
the job's flag decides whether the leg is enabled for the event, and the
stripped guard decides whether the step admits it.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_leg_matrix import admits, matrix_legs
from tests.helpers.ci_workflows import job_env, steps
from tests.helpers.workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The one job whose steps carry the leg flag.
GATED_LEG_JOB: typ.Final = ("ci.yml", "typecheck-test")

#: The conjunct every step of that job must end with.
LEG_GATE: typ.Final = "env.LEG_RUNS == 'true'"

#: The job-level flag, false only for the experimental leg on a pull request.
LEG_FLAG_EXPRESSION: typ.Final = (
    "${{ !(matrix.experimental && github.event_name == 'pull_request') }}"
)

#: The event `pull_request_legs` resolves against. Named rather than spelled
#: inline at each use, so the lanes reported and the event they were resolved
#: against cannot drift apart.
_PULL_REQUEST: typ.Final = "pull_request"

#: One admitted leg, as its `key=value` fields. A leg is the identity of the
#: pull request lane, so a caller asking whether the suite runs on more than one
#: of them counts *these*, not the fields: reading the outer length would count
#: the matrix keys of a single leg and report any leg as several. The alias
#: exists to keep that distinction in the annotation rather than in a comment.
type Lane = tuple[str, ...]

#: One `(step name, lanes)` pair per step that admits at least one leg.
type StepLanes = tuple[str, tuple[Lane, ...]]

#: The negation inside the flag, read to decide a leg. The flag is
#: ``!(<predicate>)``, so it is false exactly when its predicate holds: the leg
#: is experimental *and* the event is a pull request. Reading the flag's own
#: text — rather than re-deriving it from the leg's fields — is what keeps this
#: honest if the predicate changes shape but still names the same two inputs.
_FLAG_PREDICATE = re.compile(
    r"\A\$\{\{\s*!\s*\((?P<predicate>.*)\)\s*\}\}\Z", re.DOTALL
)
#: The conjunction inside the predicate, in the two spellings the fields
#: appear: the leg's own matrix key, and the event name comparison.
_FLAG_TERMS = re.compile(
    r"matrix\.(?P<key>[a-z0-9-]+)|github\.event_name\s*==\s*'(?P<event>[^']*)'",
    re.IGNORECASE,
)


def flag_holds_on(
    workflow_name: str, job_name: str, leg: cabc.Mapping[str, object], event: str
) -> bool:
    """Report whether the leg flag is true, for one leg and one event name.

    Parameters
    ----------
    workflow_name : str
        The workflow file name, such as ``"ci.yml"``.
    job_name : str
        The job the flag belongs to. Only ``typecheck-test`` declares one.
    leg : Mapping of str to object
        One leg, as `ci_leg_matrix.matrix_legs` returns it. The empty mapping
        stands for a job whose steps are not expanded into legs.
    event : str
        The event name to evaluate against, such as ``"pull_request"``.

    Returns
    -------
    bool
        Whether the flag lets this leg run on that event. ``True`` for every
        job that declares no flag, since nothing switches its legs off.

    Raises
    ------
    AssertionError
        If the gated job declares a flag this reader cannot decompose into the
        predicate's terms. The contract this feeds asks *which* legs a pull
        request runs, and a leg record built from an unread flag would name a
        leg the flag might have switched off; the failure is reported rather
        than guessed at.

    Notes
    -----
    Only the leg field and the event name are read. A flag whose predicate
    compares the event name against a literal other than the one asked about
    is reported false, because the predicate then does not hold for this event
    at all — the leg runs.

    Examples
    --------
    >>> flag_holds_on("ci.yml", "typecheck-test", {"experimental": False}, "pull_request")
    True
    >>> flag_holds_on("ci.yml", "typecheck-test", {"experimental": True}, "pull_request")
    False
    >>> flag_holds_on("ci.yml", "typecheck-test", {"experimental": True}, "push")
    True
    """  # ruff: ignore[line-too-long] - the doctest lines are quoted invocations
    declared = job_env(workflow_name, job_name).get("LEG_RUNS")
    if not isinstance(declared, str):
        if (workflow_name, job_name) != GATED_LEG_JOB:
            return True
        message = (
            f"{workflow_name}:{job_name} must declare a string LEG_RUNS, got "
            f"{declared!r}; the contract this feeds names the legs a pull "
            "request runs, and without the flag it cannot tell a leg that runs "
            "from one the flag switches off"
        )
        raise AssertionError(message)
    match = _FLAG_PREDICATE.match(declared.strip())
    if match is None:
        message = (
            f"{workflow_name}:{job_name} declares LEG_RUNS as {declared!r}, "
            "which this reader cannot decompose; the leg records it builds "
            "would name legs the flag may have switched off"
        )
        raise AssertionError(message)
    terms = list(_FLAG_TERMS.finditer(match.group("predicate")))
    if not terms:
        message = (
            f"{workflow_name}:{job_name} declares LEG_RUNS as {declared!r}, "
            "whose predicate names neither a matrix key nor an event; nothing "
            "a leg carries could satisfy it, so reading the absence of terms "
            "as 'the flag holds' would report every leg as switched off"
        )
        raise AssertionError(message)
    holds = True
    for term in terms:
        key = term.group("key")
        if key is not None:
            holds = holds and bool(leg.get(key, False))
            continue
        holds = holds and term.group("event") == event
    return not holds


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

    Parameters
    ----------
    workflow_name : str
        The workflow file name, such as ``"ci.yml"``.
    job_name : str
        The job the step belongs to. Only ``typecheck-test`` carries the flag.
    condition : object
        The step's ``if:`` value as parsed, or ``None`` when it has none.

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


def pull_request_legs(
    workflow_name: str, job_name: str, target: str
) -> tuple[StepLanes, ...]:
    """Return the pull-request legs a workflow's job would run a target on.

    The suite selector is only evaluated if some job step runs the target on a
    lane a pull request actually schedules, and "runs" has two conditions that
    a reader of the step's text alone cannot separate: the step's own guard
    admits the leg, and the job's leg flag leaves the leg enabled for this
    event. A job whose suite step is gated on a pre-release-only matrix key,
    or whose every admitting leg is switched off by a flag, still contains the
    command text and would satisfy a text-only check while CI collected
    nothing on the branch that merges.

    Parameters
    ----------
    workflow_name : str
        The workflow file name, such as ``"ci.yml"``.
    job_name : str
        The job expected to run ``target``.
    target : str
        The command to look for, matched as a leading command rather than as a
        substring, so a mention inside another word or a comment does not
        count.

    Returns
    -------
    tuple of StepLanes
        One ``(step name, lanes)`` pair per step of that job that runs
        ``target`` and admits at least one leg the flag leaves enabled, in
        declaration order. ``lanes`` holds one :data:`Lane` per admitted
        pull-request *leg* — a tuple of that leg's ``key=value`` fields — so
        its length is the number of lanes and not the number of matrix keys.
        Empty when no step does, which is the failure the caller reports.

    Raises
    ------
    AssertionError
        If the job or its steps are not the shape the readers narrow them to.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the readers this composes
    found: list[StepLanes] = []
    for step in steps(workflow_name, job_name):
        script = step.get("run")
        if not isinstance(script, str) or not script_runs_command(script, target):
            continue
        guard = ungated(workflow_name, job_name, step.get("if"))
        lanes = [
            tuple(f"{key}={leg[key]}" for key in sorted(leg))
            for leg in matrix_legs(workflow_name, job_name)
            if flag_holds_on(workflow_name, job_name, leg, _PULL_REQUEST)
            and admits(guard, leg)
        ]
        if lanes:
            found.append((str(step.get("name", step.get("uses", "?"))), tuple(lanes)))
    return tuple(found)
