"""Property tests for the reader that decides what a workflow guard admits.

`ci_leg_matrix.admits_event` resolves the `if:` condition that decides whether a
suite runs on a matrix leg, and it is the only thing between a contract and a
vacuous pass: a reader that answered "satisfied" for a clause it cannot model
would certify a suite on a lane that never runs it.

The example tests beside these pin the shapes this repository declares today,
so every case they can express is one the estate already satisfies; a reader
could stop reading and the contracts above it would keep passing. The input
space is generated here instead of enumerated, and every property is checked
against a small independent model rather than a second copy of the reader, so
the two agreeing means both are right about the invariant rather than both
sharing a defect.

Each generator guarantees the shape its property is about, and each property
asserts that shape before asserting the behaviour, so a generator that stopped
producing the interesting case fails its own witness rather than leaving the
property true and empty.

The Makefile and recipe readers this module's sibling covers live in
`tests/test_ci_makefile_reader_properties.py`; the two were one module until it
exceeded the 400-line ceiling the `pylint-classic` gate enforces on `tests/`.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers import ci_leg_matrix

if typ.TYPE_CHECKING:
    import re

    from hypothesis.strategies import DrawFn

#: Hypothesis's default deadline measures the host rather than the code, and a
#: flaky contract test is worse than a slow one. The same setting and the same
#: reason as `tests/test_ci_placement_properties.py`.
SETTINGS = settings(deadline=None, max_examples=200)

_MESSAGE = "synthetic guard"

#: Matrix key names, hyphenated as `MATRIX_CLAUSE_PATTERN` allows.
KEYS = st.from_regex(r"\A[a-z][a-z0-9-]{0,12}\Z")
#: Leg values whose string form is never a generated literal, so a bare clause
#: and its equality form are decided by the comparison rather than by `bool`.
VALUES = st.one_of(st.booleans(), st.integers(min_value=2, max_value=9))
#: A leg: at most three keys, each with a generated value.
LEGS = st.dictionaries(KEYS, VALUES, max_size=3)
#: The event a clause names and the event under test, drawn independently so
#: both the match and the mismatch case are generated.
EVENTS = st.tuples(
    st.sampled_from(["pull_request", "push"]),
    st.sampled_from(["pull_request", "push"]),
)
STATUS_FUNCTIONS = st.sampled_from(["always()", "success()", "failure()"])
#: Clause shapes the reader does not model: a disjunction, an `env.` read, a
#: `!=` against the event name, and a wrapped expression it cannot unwrap.
UNRECOGNIZED = st.sampled_from([
    "matrix.python-suite || matrix.experimental",
    "env.LEG_RUNS == 'true'",
    "github.event_name != 'push'",
    "${{ secrets.TOKEN != '' }}",
])


def _matrix_clause(key: str, value: object) -> tuple[str, bool]:
    """Render one clause naming ``key``, and say whether the leg admits it.

    Returning the expectation beside the text keeps the two from drifting: the
    single-clause property below asserts exactly the value this states, so a
    clause form added here without a matching expectation cannot pass silently.

    Returns
    -------
    tuple of (str, bool)
        The clause text, and whether a leg carrying ``value`` under ``key``
        satisfies it.
    """
    if value is True:
        return f"matrix.{key}", True
    if value is False:
        return f"matrix.{key} == 'on'", False
    return f"matrix.{key} != 'off'", True


@st.composite
def _recognized_clauses(draw: DrawFn) -> str:
    """Build a conjunction of clauses `admits_event` can settle.

    Every draw carries at least one matrix clause and exactly one event clause,
    so the conjunction is never empty and always exercises the event path. The
    clauses are permuted rather than appended, because clause *order* is one of
    the inputs the reader's split has to survive.

    Returns
    -------
    str
        An ``&&``-joined condition the reader can settle for any leg and event.
    """
    leg = draw(LEGS)
    named_event = draw(st.sampled_from(["pull_request", "push"]))
    parts = [
        *(_matrix_clause(key, value)[0] for key, value in leg.items()),
        draw(STATUS_FUNCTIONS),
        f"github.event_name == '{named_event}'",
    ]
    return " && ".join(draw(st.permutations(parts)))


def _holds(clause: str, leg: dict[str, object], queried: str) -> bool:
    """Decide one clause from the docstring's rules, not from the reader.

    The three recognized forms are settled here as prose states them: a matrix
    reference holds when the leg carries its key and the comparison holds, an
    event comparison when its literal is the event under test, and a status
    function always. The model deliberately reaches for the public
    `MATRIX_CLAUSE_PATTERN` and nothing else, so a reader that changed how it
    *decides* a form still has to agree with this.

    Parameters
    ----------
    clause : str
        One clause of an ``&&``-joined condition.
    leg : dict of str to object
        The matrix leg the clause is read over.
    queried : str
        The event the condition is being decided against.

    Returns
    -------
    bool
        Whether the clause holds.
    """
    match = ci_leg_matrix.MATRIX_CLAUSE_PATTERN.match(clause)
    if match is not None:
        return _matrix_holds(match, leg)
    if clause.startswith("github.event_name =="):
        return clause.endswith(f"'{queried}'")
    assert clause.casefold().startswith(("always", "success", "failure")), (
        f"the clause generator produced {clause!r}, which none of the "
        "reader's recognized forms describes; the model would decide it "
        "by accident rather than by reading it"
    )
    return True


def _matrix_holds(match: re.Match[str], leg: dict[str, object]) -> bool:
    """Decide a matrix clause: a missing key fails, a bare key is truthy.

    Returns
    -------
    bool
        Whether the leg satisfies the clause.
    """
    key = match.group("key")
    if key not in leg:
        return False
    operator = match.group("operator")
    if operator is None:
        return bool(leg[key])
    literal = match.group("literal")
    return leg[key] == literal if operator == "==" else leg[key] != literal


@SETTINGS
@given(condition=_recognized_clauses(), leg=LEGS, event=EVENTS)
def test_admits_event_agrees_with_an_independent_evaluation(
    condition: str, leg: dict[str, object], event: tuple[str, str]
) -> None:
    """Evaluate every recognized clause directly, not through the reader.

    The model below is written from the module docstring rather than from
    `_admitted_clause`: a matrix clause holds when the leg carries the key and
    the comparison holds, an event clause when its literal is the event under
    test, and a status function always. A conjunction holds when every clause
    does, so drawing the named event and the tested event independently covers
    both the match and the mismatch that the module's own docstring example
    pins. Agreement on every generated conjunction is what two readers being
    independently right looks like; a second call into the reader would only
    prove it self-consistent.
    """
    presented, queried = event
    assert "github.event_name ==" in condition, (
        f"the generator must always produce an event clause; {condition!r} has none"
    )
    admitted = ci_leg_matrix.admits_event(condition, leg, queried, subject=_MESSAGE)
    expected = all(
        _holds(clause.strip(), leg, queried) for clause in condition.split("&&")
    )
    assert admitted == expected, (
        f"{condition!r} over {leg!r} at {presented!r} must be {expected}; the "
        f"reader reported {admitted}, so it disagreed with a direct "
        "evaluation of the same clauses"
    )


@SETTINGS
@given(name=KEYS, value=VALUES, event=EVENTS)
def test_a_matrix_clause_alone_is_decided_by_the_leg(
    name: str, value: object, event: tuple[str, str]
) -> None:
    """One clause, one leg, one answer — and never an answer from another key."""
    _presented, queried = event
    clause, expected = _matrix_clause(name, value)
    admitted = ci_leg_matrix.admits_event(
        clause, {name: value}, queried, subject=_MESSAGE
    )
    assert admitted == expected, (
        f"{clause!r} over {name!r} = {value!r} must be {expected}; the reader "
        f"reported {admitted}"
    )
    assert not ci_leg_matrix.admits_event(
        clause, {f"other-{name}": value}, queried, subject=_MESSAGE
    ), f"{clause!r} must not be admitted by a leg carrying a different key"


@SETTINGS
@given(event=EVENTS)
def test_an_event_clause_holds_only_on_the_event_it_names(
    event: tuple[str, str],
) -> None:
    """Hold on the named event and on no other."""
    presented, queried = event
    condition = f"github.event_name == '{presented}'"
    admitted = ci_leg_matrix.admits_event(condition, {}, queried, subject=_MESSAGE)
    assert admitted == (presented == queried), (
        f"{condition!r} must be {presented == queried} for a {queried!r} run; "
        f"the reader reported {admitted}"
    )


@SETTINGS
@given(clause=UNRECOGNIZED)
def test_an_unrecognized_clause_is_refused_not_admitted(clause: str) -> None:
    """Refuse the shapes this reader cannot settle rather than certify them.

    Reading an unmodellable clause as satisfied is the error `admits_event`
    exists to avoid: it would certify a suite on a lane that may never run it.
    The refusal is what makes the recognized forms meaningful.
    """
    with pytest.raises(AssertionError, match="cannot resolve"):
        ci_leg_matrix.admits_event(
            clause, {"python-suite": True}, "pull_request", subject=_MESSAGE
        )
