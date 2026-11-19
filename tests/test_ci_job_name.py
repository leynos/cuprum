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

import pytest

from tests.helpers import ci_job_name as renderer

#: The shape `ci.yml`'s `typecheck-test` declares, exercised with a small leg.
NAME = (
    "Test (Python ${{ matrix.label }}${{ matrix.experimental && ', PR skip' || '' }})"
)
#: The non-experimental values behind that template.
LEG = {"label": "3.12", "experimental": False}
#: The same leg with the experimental flag set, which selects the suffix.
EXPERIMENTAL_LEG = {**LEG, "experimental": True}


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
            "a${{ matrix.x && 'b' || 'c' }}d",
            {"x": False},
            "acd",
            id="falsy-reads-otherwise",
        ),
        pytest.param(
            "a${{ matrix.x && 'b' || 'c' }}d",
            {"x": True},
            "abd",
            id="truthy-reads-when",
        ),
        pytest.param(
            "a${{ matrix.x && '' || 'd' }}",
            {"x": True},
            "a",
            id="empty-when",
        ),
        pytest.param(
            "a${{ matrix.x && 'b' || '' }}",
            {"x": False},
            "a",
            id="empty-otherwise",
        ),
        pytest.param(
            "a${{ matrix.x && 'b' || 'c' }}${{ matrix.x && 'b' || 'c' }}",
            {"x": True},
            "abb",
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
