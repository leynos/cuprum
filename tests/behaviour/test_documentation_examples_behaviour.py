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
MARKER = re.compile(r"<!-- tested-example: ([a-z0-9-]+) -->")


def _documented_examples() -> list[tuple[str, str]]:
    """Load every marked Python fence and reject uncovered fences."""
    examples: list[tuple[str, str]] = []
    identifiers: set[str] = set()
    for path in DOCUMENTS:
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        pending: str | None = None
        index = 0
        while index < len(lines):
            line = lines[index]
            marker = MARKER.fullmatch(line)
            if marker is not None:
                assert pending is None, f"{path}:{index + 1}: marker without a fence"
                pending = marker.group(1)
                index += 1
                continue
            if line.lstrip().startswith("```"):
                assert pending is not None, f"{path}:{index + 1}: untested fence"
                assert line == "```python", f"{path}:{index + 1}: expected Python"
                assert pending not in identifiers, f"duplicate example: {pending}"
                identifiers.add(pending)
                start = index + 1
                index = start
                while index < len(lines) and lines[index] != "```":
                    index += 1
                assert index < len(lines), f"{path}:{start}: unterminated fence"
                code = "\n".join(lines[start:index]) + "\n"
                assert "assert " in code, (
                    f"{path}:{start}: example has no outcome check"
                )
                examples.append((pending, code))
                pending = None
            elif pending is not None:
                assert not line, f"{path}:{index + 1}: marker is not beside a fence"
            index += 1
        assert pending is None, f"{path}: marker without a fence"
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
