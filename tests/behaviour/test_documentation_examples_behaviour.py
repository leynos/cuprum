"""Execute the exact examples published in Cuprum's user-facing documents."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from cuprum import ExecutionContext, Program, ProgramCatalogue, sh

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    "README.md",
    "docs/users-guide.md",
    "docs/v0-2-0-migration-guide.md",
)
MARKER = re.compile(r"<!-- (tested|shell)-example: ([a-z0-9-]+) -->")
# Python fences must execute; shell fences (installation commands, for
# example) are published for copying and are never run by this suite.
LANGUAGES = {"tested": "python", "shell": "shell"}
# Both CommonMark fence characters open a block, so neither can hide an
# example from marker validation.
FENCE_DELIMITERS = ("```", "~~~")


def _has_outcome_assertion(code: str) -> bool:
    """Return whether ``code`` contains an executable ``assert`` statement."""
    return any(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(code)))


def _consume_fence(
    path: str, lines: list[str], index: int, marker: tuple[str, str]
) -> tuple[int, list[tuple[str, str]]]:
    """Validate the marked fence opening at ``index`` and consume it.

    Returns
    -------
    tuple[int, list[tuple[str, str]]]
        The index of the first line after the closing fence, and the tested
        example the fence holds, if any.
    """
    kind, name = marker
    opening = lines[index]
    delimiter = opening[:3]
    assert opening == f"{delimiter}{LANGUAGES[kind]}", (
        f"{path}:{index + 1}: expected {LANGUAGES[kind]} fence for {kind} example"
    )
    start = index + 1
    closing = (
        number for number in range(start, len(lines)) if lines[number] == delimiter
    )
    end = next(closing, None)
    assert end is not None, f"{path}:{start}: unterminated fence"
    if kind != "tested":
        return end + 1, []
    code = "\n".join(lines[start:end]) + "\n"
    assert _has_outcome_assertion(code), f"{path}:{start}: example has no outcome check"
    return end + 1, [(name, code)]


def _parse_examples(path: str, text: str) -> list[tuple[str, str]]:
    """Return a document's tested Python examples, rejecting uncovered fences."""
    examples: list[tuple[str, str]] = []
    lines = text.splitlines()
    pending: tuple[str, str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = MARKER.fullmatch(line)
        if marker is not None:
            assert pending is None, f"{path}:{index + 1}: marker without a fence"
            pending = (marker.group(1), marker.group(2))
        elif line.lstrip().startswith(FENCE_DELIMITERS):
            assert pending is not None, f"{path}:{index + 1}: unmarked fence"
            index, found = _consume_fence(path, lines, index, pending)
            examples.extend(found)
            pending = None
            continue
        else:
            assert pending is None or not line, (
                f"{path}:{index + 1}: marker is not beside a fence"
            )
        index += 1
    assert pending is None, f"{path}: marker without a fence"
    return examples


def _documented_examples() -> list[tuple[str, str]]:
    """Load every tested example across the published documents."""
    examples: list[tuple[str, str]] = []
    for path in DOCUMENTS:
        text = (ROOT / path).read_text(encoding="utf-8")
        examples.extend(_parse_examples(path, text))
    names = [name for name, _ in examples]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate examples: {duplicates}"
    return examples


EXAMPLES = _documented_examples()
PYTHON = sh.make(
    Program(sys.executable),
    catalogue=ProgramCatalogue.from_programs(sys.executable, name="documentation"),
)


@pytest.mark.parametrize(("name", "code"), EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_published_example_behaves_as_documented(name: str, code: str) -> None:
    """Execute one published example and its observable outcome assertions."""
    result = PYTHON("-c", code).run_sync(context=ExecutionContext(cwd=ROOT))
    assert result.ok, f"{name} failed:\n{result.stdout}\n{result.stderr}"


def test_shell_examples_are_published_but_not_executed() -> None:
    """A marked shell fence is accepted without joining the executed set."""
    text = "<!-- shell-example: install -->\n\n```shell\npip install cuprum\n```\n"
    assert not _parse_examples("doc.md", text), "shell example was queued to run"


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
