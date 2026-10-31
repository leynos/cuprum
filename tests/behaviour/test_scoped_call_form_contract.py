"""Reject the retired keyword-scope call form in tracked files."""

from __future__ import annotations

import ast
import re
import typing as typ
import warnings
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


def _text_passes_retired_keyword(text: str) -> bool:
    """Return whether a text scan finds the retired keyword on ``scoped()``."""
    return any(
        DIRECT_KEYWORD.search(_without_nested_calls(arguments))
        for arguments in _scoped_arguments(text)
    )


def _is_scoped_call(node: ast.AST) -> bool:
    """Return whether ``node`` calls the module-level ``scoped`` function."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "scoped"
    )


def _python_passes_retired_keyword(tree: ast.AST) -> bool:
    """Return whether parsed code gives ``scoped()`` the retired keyword."""
    return any(
        keyword.arg == KEYWORD
        for node in ast.walk(tree)
        if _is_scoped_call(node)
        for keyword in node.keywords
    )


def _passes_retired_keyword(text: str) -> bool:
    """Return whether ``text`` gives ``scoped()`` a direct keyword argument.

    Python sources are parsed, so a parenthesis inside a string argument
    cannot cut the call short. Files that are not one Python module, such as
    Markdown documents, fall back to a text scan.

    Returns
    -------
    bool
        Whether any ``scoped()`` call passes the retired keyword directly.
    """
    with warnings.catch_warnings():
        # Most tracked files are not Python, so a SyntaxWarning here describes
        # the scanned document's own escapes rather than a defect in this test.
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return _text_passes_retired_keyword(text)
    return _python_passes_retired_keyword(tree)


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
            'scoped(catalogue=build(")"), ' + KEYWORD + "=mate.allowlist)",
            True,
            id="parenthesis-inside-a-string",
        ),
        pytest.param(
            "usage: scoped(" + KEYWORD + "=mate.allowlist) in prose",
            True,
            id="text-that-is-not-a-python-module",
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
