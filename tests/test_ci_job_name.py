"""Unit tests for the fail-closed job-name renderer.

The renderer exists so a contract can compare the check context GitHub will
show with a literal. Its value depends on refusing what it cannot read: a
renderer that passes an unsupported expression through compares the template
with itself, which is the vacuous assertion the module exists to avoid. These
cases drive the supported forms and every refusal directly, rather than only
against this repository's own correct declaration, so a renderer that had
stopped discriminating still fails here.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers import ci_job_name as renderer

#: The shape `ci.yml`'s `typecheck-test` declares, exercised with a small leg.
NAME = (
    "Test (Python ${{ matrix.label }}${{ matrix.experimental && ', PR skip' || '' }})"
)
#: The non-experimental values behind that template.
LEG = {"label": "3.12", "experimental": False}
#: The same leg with the experimental flag set, which selects the suffix.
EXPERIMENTAL_LEG = {**LEG, "experimental": True}
#: Hypothesis's default deadline measures the host rather than the code, and
#: these cases are trivial; a flaky contract test is worse than a slow one.
SETTINGS = settings(deadline=None, max_examples=200)
#: A literal the choice grammar can carry. A quote would end the literal, a
#: brace or `$` would leave an expression delimiter in the rendered name, and
#: a line break makes `render_template` refuse the whole template.
LITERALS = st.text(
    alphabet=st.characters(blacklist_characters=["'", "$", "{", "}", "\n", "\r"]),
    max_size=6,
)
#: Leg values across the coercion domain: every scalar in GitHub's falsy set,
#: both empty and non-empty containers, and the containers Python calls falsy.
LEG_VALUES = st.one_of(
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(),
    st.none(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=2), st.integers(), max_size=2),
)
#: Operators the renderer does not model, so a body built from one is outside
#: the supported forms by construction rather than by luck of the draw.
COMPARISONS = st.sampled_from(["==", "!=", ">", "<", ">=", "<="])


def _github_truthy(value: object) -> bool:
    """Return GitHub's coercion, stated from its documented falsy set.

    The falsy values are `false`, `0`, `-0`, `NaN`, `null` and the empty
    string. Everything else, including an empty array or object, is truthy.

    Returns
    -------
    bool
        Whether the documented set makes the value truthy.
    """
    if isinstance(value, float) and math.isnan(value):
        return False
    # A set cannot be used: arrays and objects are unhashable, and they are
    # exactly the values this oracle has to decide as truthy.
    return value not in (False, 0, "", None)  # ruff: ignore[literal-membership] - containers are unhashable.


@pytest.mark.parametrize(
    ("template", "leg", "expected"),
    [
        pytest.param("plain text", {}, "plain text", id="literal"),
        pytest.param("Test (${{ matrix.label }})", LEG, "Test (3.12)", id="bare"),
        pytest.param(NAME, LEG, "Test (Python 3.12)", id="falsy-choice"),
        pytest.param(
            NAME, EXPERIMENTAL_LEG, "Test (Python 3.12, PR skip)", id="truthy-choice"
        ),
        pytest.param(
            "a${{ matrix.x && '+' || '-' }}d",
            {"x": False},
            "a-d",
            id="falsy-reads-otherwise",
        ),
        pytest.param(
            "a${{ matrix.x && '+' || '-' }}d",
            {"x": True},
            "a+d",
            id="truthy-reads-when",
        ),
        pytest.param(
            "a${{ matrix.x && '' || '-' }}",
            {"x": True},
            "a-",
            id="empty-when-falls-through-to-otherwise",
        ),
        pytest.param(
            "a${{ matrix.x && '+' || '' }}",
            {"x": False},
            "a",
            id="empty-otherwise",
        ),
        pytest.param(
            "a${{ matrix.x && '+' || '-' }}${{ matrix.x && '+' || '-' }}",
            {"x": True},
            "a++",
            id="two-expressions",
        ),
    ],
)
def test_supported_forms_render(
    template: str, leg: dict[str, object], expected: str
) -> None:
    """Render every form the job name may use, including empty literals."""
    assert renderer.render_template(template, leg, subject="test") == expected, (
        f"{template!r} over {leg!r} must render {expected!r}"
    )


@pytest.mark.parametrize(
    ("template", "leg", "message"),
    [
        pytest.param(
            "${{ matrix.missing }}", {}, "does not declare", id="unknown-field"
        ),
        pytest.param(
            "${{ matrix.x && 'b' || '' }}",
            {},
            "does not declare",
            id="unknown-choice-field",
        ),
        pytest.param(
            "${{ matrix.x == 'y' }}", {"x": "y"}, "cannot resolve", id="comparison"
        ),
        pytest.param(
            "${{ github.event_name }}", {}, "names the event", id="event-reference"
        ),
        pytest.param(
            "${{ matrix.x }}",
            {"x": True},
            "as text",
            id="non-string-value",
        ),
        pytest.param("a\nb", {}, "line break", id="folded-continuation"),
        pytest.param("${{ matrix.x", {"x": "y"}, "unbalanced", id="unterminated"),
        pytest.param("a }} b", {}, "unbalanced", id="stray-delimiter"),
        pytest.param(42, {}, "string template", id="non-string-template"),
    ],
)
def test_unreadable_templates_are_refused(
    template: object, leg: dict[str, object], message: str
) -> None:
    """Refuse a template the renderer cannot reproduce exactly.

    A pass-through would make every downstream comparison a comparison of the
    declaration with itself, so each shape is reported rather than copied.
    """
    with pytest.raises(AssertionError, match=message):
        renderer.render_template(template, leg, subject="test")


def test_render_name_reads_the_declared_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render the declaration a job actually carries, not a caller's copy."""
    monkeypatch.setattr(renderer, "job", lambda *_: {"name": NAME})
    rendered = renderer.render_name("ci.yml", "typecheck-test", EXPERIMENTAL_LEG)
    assert rendered == "Test (Python 3.12, PR skip)", (
        f"the declared template must render for the supplied leg, got {rendered!r}"
    )


def test_rendered_names_iterates_the_declared_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source the legs from the matrix expansion every other contract reads."""
    monkeypatch.setattr(renderer, "job", lambda *_: {"name": "${{ matrix.label }}"})
    monkeypatch.setattr(
        renderer,
        "matrix_legs",
        lambda *_: [{"label": "3.12"}, {"label": "3.15.2"}],
    )
    rendered = [name for _leg, name in renderer.rendered_names("ci.yml", "job")]
    assert rendered == ["3.12", "3.15.2"], (
        f"one name per declared leg is required, got {rendered!r}"
    )


def test_a_job_without_a_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unnamed job's context is its key, which is a different question."""
    monkeypatch.setattr(renderer, "job", lambda *_: {})
    with pytest.raises(AssertionError, match="string name"):
        renderer.declared_name("ci.yml", "typecheck-test")


@SETTINGS
@given(when=LITERALS, otherwise=LITERALS, value=LEG_VALUES)
def test_a_choice_renders_the_branch_github_selects(
    when: str, otherwise: str, value: object
) -> None:
    """Model the `&&`/`||` pair over the values a matrix can hold.

    The table beside this test fixes both flags; the coercion is what a leg
    with another value would rely on, and it is not Python's truthiness.
    """
    template = f"a${{{{ matrix.x && '{when}' || '{otherwise}' }}}}d"
    rendered = renderer.render_template(template, {"x": value}, subject="property")
    selected = (when or otherwise) if _github_truthy(value) else otherwise
    assert rendered == f"a{selected}d", (
        f"a leg value of {value!r} must render {selected!r}, choosing between "
        f"{when!r} and {otherwise!r}, got {rendered!r}"
    )
    assert "${{" not in rendered, (
        f"a rendered name may never carry an unread opener, got {rendered!r}"
    )
    assert "}}" not in rendered, (
        f"a rendered name may never carry an unread closer, got {rendered!r}"
    )


@SETTINGS
@given(
    field=st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True),
    operator=COMPARISONS,
    literal=LITERALS,
)
def test_a_comparison_expression_is_refused(
    field: str, operator: str, literal: str
) -> None:
    """Refuse bodies outside the two supported forms, whatever their parts.

    The refusal set is unbounded, so no table can enumerate it; generating
    the parts and building an unsupported shape is what shows the renderer
    reports what it cannot read instead of copying it through.
    """
    body = f"matrix.{field} {operator} '{literal}'"
    with pytest.raises(AssertionError, match="cannot resolve"):
        renderer.render_template(f"${{{{ {body} }}}}", {}, subject="property")
