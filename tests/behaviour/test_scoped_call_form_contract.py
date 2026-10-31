"""Reject the retired keyword-scope call form in tracked files."""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest

from cuprum import GIT, ExecutionContext, sh

if typ.TYPE_CHECKING:
    import collections.abc as cabc

ROOT = Path(__file__).resolve().parents[2]
# Assembled from fragments so this test does not flag its own source text.
KEYWORD = "allow" + "list"
SCOPED_CALL = re.compile(r"\bscoped\s*\(")
DIRECT_KEYWORD = re.compile(KEYWORD + r"\s*=")


def _scoped_arguments(text: str) -> cabc.Iterator[str]:
    """Yield the argument text of every balanced ``scoped(...)`` call."""
    for match in SCOPED_CALL.finditer(text):
        depth = 1
        for index in range(match.end(), len(text)):
            char = text[index]
            depth += char == "("
            depth -= char == ")"
            if depth == 0:
                yield text[match.end() : index]
                break


def _without_nested_calls(arguments: str) -> str:
    """Return ``arguments`` with every balanced bracketed group removed."""
    kept: list[str] = []
    depth = 0
    for char in arguments:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def _passes_retired_keyword(text: str) -> bool:
    """Return whether ``text`` gives ``scoped()`` a direct keyword argument."""
    return any(
        DIRECT_KEYWORD.search(_without_nested_calls(arguments))
        for arguments in _scoped_arguments(text)
    )


def _tracked_text_files() -> list[Path]:
    """Return every tracked regular file below the repository root."""
    result = sh.make(GIT)("ls-files", "-z").run_sync(context=ExecutionContext(cwd=ROOT))
    assert result.ok, result.stderr
    paths: list[Path] = []
    for name in result.stdout.split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.is_file():
            paths.append(path)
    return paths


@pytest.mark.parametrize(
    ("source", "is_retired"),
    [
        pytest.param(
            "scoped(" + KEYWORD + "=mate.allowlist)", True, id="leading-keyword"
        ),
        pytest.param(
            "scoped(catalogue=CATALOGUE, " + KEYWORD + "=mate.allowlist)",
            True,
            id="keyword-after-catalogue",
        ),
        pytest.param(
            "scoped(\n    catalogue=CATALOGUE,\n    " + KEYWORD + "=m,\n)",
            True,
            id="keyword-on-its-own-line",
        ),
        pytest.param(
            "scoped(ScopeConfig(" + KEYWORD + "=mate.allowlist))",
            False,
            id="nested-scope-config",
        ),
        pytest.param(
            "scoped(catalogue=build(" + KEYWORD + "))", False, id="nested-call"
        ),
        pytest.param("scoped(catalogue=CATALOGUE)", False, id="current-form"),
    ],
)
def test_retired_keyword_detection(source: str, is_retired: bool) -> None:
    """Detect the retired keyword only among ``scoped()``'s direct arguments."""
    detected = _passes_retired_keyword(source)
    assert detected is is_retired, f"{source!r}: detected {detected}"


def test_no_tracked_file_passes_allowlist_directly_to_scoped() -> None:
    """No tracked file calls ``scoped()`` with a bare ``allowlist=`` keyword."""
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _passes_retired_keyword(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "the retired keyword-scope call form remains in: " + ", ".join(offenders)
    )
