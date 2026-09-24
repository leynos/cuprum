"""Execute the exact examples published in Cuprum's user-facing documents."""

from __future__ import annotations

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
FENCES = {"tested": "```python", "shell": "```shell"}


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
            index += 1
            continue
        if line.lstrip().startswith("```"):
            assert pending is not None, f"{path}:{index + 1}: unmarked fence"
            kind, name = pending
            assert line == FENCES[kind], (
                f"{path}:{index + 1}: expected {FENCES[kind]} for {kind} example"
            )
            start = index + 1
            index = start
            while index < len(lines) and lines[index] != "```":
                index += 1
            assert index < len(lines), f"{path}:{start}: unterminated fence"
            if kind == "tested":
                code = "\n".join(lines[start:index]) + "\n"
                assert "assert " in code, (
                    f"{path}:{start}: example has no outcome check"
                )
                examples.append((name, code))
            pending = None
        elif pending is not None:
            assert not line, f"{path}:{index + 1}: marker is not beside a fence"
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


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("```shell\npip install cuprum\n```\n", id="unmarked-fence"),
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
