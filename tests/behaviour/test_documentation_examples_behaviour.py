"""Execute the exact examples published in Cuprum's user-facing documents."""

from __future__ import annotations

import re
import sys

import pytest

from cuprum import ExecutionContext, Program, ProgramCatalogue, sh
from tests.behaviour._documentation_examples_support import (
    DOCUMENTS,
    EXAMPLES,
    ROOT,
    _parse_examples,
)

SCRIPTING_STANDARDS_EXAMPLES = frozenset({
    "catalogue-definition",
    "scoped-catalogue",
    "scoped-scope-config",
    "capture-output",
    "safe-globbing",
})

PYTHON = sh.make(
    Program(sys.executable),
    catalogue=ProgramCatalogue.from_programs(sys.executable, name="documentation"),
)


@pytest.mark.parametrize(("name", "code"), EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_published_example_behaves_as_documented(name: str, code: str) -> None:
    """Execute one published example and its observable outcome assertions."""
    result = PYTHON("-c", code).run_sync(context=ExecutionContext(cwd=ROOT))
    assert result.ok, f"{name} failed:\n{result.stdout}\n{result.stderr}"


def test_the_scripting_standards_document_is_gated() -> None:
    """The imported standards document is registered and its examples are gated."""
    assert "docs/scripting-standards.md" in DOCUMENTS, (
        "docs/scripting-standards.md is not registered for example gating"
    )
    # Parse the named document rather than the combined set: an example that
    # moved to another document, or that became illustrative here, still has
    # a name in EXAMPLES and would otherwise satisfy this check.
    path = "docs/scripting-standards.md"
    text = (ROOT / path).read_text(encoding="utf-8")
    published = {name for name, _ in _parse_examples(path, text)}
    missing = SCRIPTING_STANDARDS_EXAMPLES - published
    assert not missing, f"scripting standards examples are not gated: {sorted(missing)}"


def test_shell_examples_are_published_but_not_executed() -> None:
    """A marked shell fence is accepted without joining the executed set."""
    text = "<!-- shell-example: install -->\n\n```shell\npip install cuprum\n```\n"
    assert not _parse_examples("doc.md", text), "shell example was queued to run"


def test_illustrative_examples_are_published_but_not_executed() -> None:
    """An illustrative fence in any language is accepted without being run."""
    text = (
        "- `workflow`: needs a prepared runner environment.\n"
        "<!-- illustrative-example: workflow -->\n"
        "```yaml\n- name: Build\n```\n"
    )
    assert not _parse_examples("doc.md", text), "illustrative example was queued to run"


def test_a_wrapped_bullet_states_its_reason() -> None:
    """A reason that wraps onto continuation lines still justifies exclusion."""
    text = (
        "- `build`:\n"
        "  needs a prepared runner and a checkout.\n"
        "<!-- illustrative-example: build -->\n"
        "```yaml\n- name: Build\n```\n"
    )
    assert not _parse_examples("doc.md", text), "wrapped reason was not accepted"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "<!-- illustrative-example: unexplained -->\n```yaml\n- name: Build\n```\n",
            id="no-prose",
        ),
        pytest.param(
            "Building happens elsewhere.\n"
            "<!-- illustrative-example: build -->\n"
            "```yaml\n- name: Build\n```\n",
            id="name-only-inside-a-longer-word",
        ),
        pytest.param(
            "- `build`:\n<!-- illustrative-example: build -->\n"
            "```yaml\n- name: Build\n```\n",
            id="bullet-names-the-example-without-a-reason",
        ),
        pytest.param(
            "- build\n<!-- illustrative-example: build -->\n"
            "```yaml\n- name: Build\n```\n",
            id="name-only-bullet-without-a-code-span",
        ),
        pytest.param(
            "The build example needs a prepared runner.\n"
            "<!-- illustrative-example: build -->\n"
            "```yaml\n- name: Build\n```\n",
            id="reason-away-from-a-bullet",
        ),
    ],
)
def test_illustrative_examples_require_a_documented_reason(text: str) -> None:
    """An exclusion the document's prose does not name fails the gate."""
    with pytest.raises(AssertionError, match="has no documented exclusion reason"):
        _parse_examples("doc.md", text)


def test_indented_markers_and_fences_are_recognized() -> None:
    """A tested fence nested in a list item keeps its Python, dedented."""
    text = (
        "  <!-- tested-example: nested -->\n"
        "  ```python\n"
        "  value = 1\n"
        "  assert value == 1\n"
        "  ```\n"
    )
    assert _parse_examples("doc.md", text) == [
        ("nested", "value = 1\nassert value == 1\n")
    ], "an indented tested example must be dedented into valid Python"


def test_short_indented_body_lines_lose_their_indentation() -> None:
    """A body line indented less than the fence keeps no stray indentation."""
    text = (
        "  <!-- tested-example: partial -->\n"
        "  ```python\n"
        " value = 1\n"
        "  assert value == 1\n"
        "  ```\n"
    )
    assert _parse_examples("doc.md", text) == [
        ("partial", "value = 1\nassert value == 1\n")
    ], "a shallower body line must not stay indented"


@pytest.mark.parametrize(
    "body_line",
    [
        pytest.param("    ```", id="four-spaces"),
        pytest.param("\t```", id="tab"),
    ],
)
def test_indented_fence_lines_stay_inside_the_example(body_line: str) -> None:
    """Indentation of three columns or more keeps a fence line inside the example."""
    text = (
        "<!-- tested-example: quoted -->\n"
        "```python\n"
        'text = """\n'
        f"{body_line}\n"
        '"""\n'
        'assert "```" in text\n'
        "```\n"
    )
    assert _parse_examples("doc.md", text) == [
        ("quoted", f'text = """\n{body_line}\n"""\nassert "```" in text\n')
    ], "an indented fence line must not end the example early"


def test_a_longer_fence_needs_an_equally_long_closer() -> None:
    """A four-backtick fence is not closed by a three-backtick line inside it."""
    text = (
        "- `workflow`: needs a prepared runner.\n"
        "<!-- illustrative-example: workflow -->\n"
        "````yaml\n"
        "```\n"
        "````\n"
    )
    assert not _parse_examples("doc.md", text), "the inner fence closed the example"


def test_tested_examples_are_returned_in_document_order() -> None:
    """Each marked Python fence, either fence style, yields its exact code."""
    text = (
        "<!-- tested-example: first -->\n\n```python\nassert 1\n```\n"
        "Prose between examples.\n"
        "<!-- tested-example: second -->\n~~~python\nassert 2\n~~~\n"
    )
    assert _parse_examples("doc.md", text) == [
        ("first", "assert 1\n"),
        ("second", "assert 2\n"),
    ], "tested examples must keep their order and exact code"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("```shell\npip install cuprum\n```\n", id="unmarked-fence"),
        pytest.param("~~~python\nassert True\n~~~\n", id="unmarked-tilde-fence"),
        pytest.param(
            "<!-- shell-example: sneaky -->\n```python\nprint(1)\n```\n",
            id="python-behind-shell-marker",
        ),
        pytest.param(
            "<!-- tested-example: wrong -->\n```shell\nls\n```\n",
            id="shell-behind-tested-marker",
        ),
    ],
)
def test_misdeclared_fences_are_rejected(text: str) -> None:
    """Every fence needs a marker whose kind matches its language."""
    with pytest.raises(AssertionError):
        _parse_examples("doc.md", text)


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    [
        pytest.param(
            "<!-- tested-example: gap -->\nProse.\n```python\nassert 1\n```\n",
            "doc.md:2: marker is not beside a fence",
            id="marker-separated-by-text",
        ),
        pytest.param(
            "Intro.\n<!-- tested-example: orphan -->\n",
            "doc.md: marker without a fence",
            id="marker-without-fence",
        ),
        pytest.param(
            "<!-- tested-example: open -->\n```python\nassert 1\n",
            "doc.md:2: unterminated fence",
            id="unterminated-fence",
        ),
        pytest.param(
            "<!-- tested-example: quiet -->\n```python\nprint(1)\n```\n",
            "doc.md:2: example has no outcome check",
            id="no-outcome-check",
        ),
        pytest.param(
            (
                "<!-- tested-example: fake -->\n"
                '```python\n# assert x\ns = "assert y"\n```\n'
            ),
            "doc.md:2: example has no outcome check",
            id="assert-only-in-comment-or-string",
        ),
    ],
)
def test_malformed_examples_report_their_location(text: str, diagnostic: str) -> None:
    """Placement and outcome errors name the document and the offending line."""
    with pytest.raises(AssertionError, match=re.escape(diagnostic)):
        _parse_examples("doc.md", text)
