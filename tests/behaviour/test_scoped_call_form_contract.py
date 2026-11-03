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


_STRING_PREFIXES = "rbufRBUF"
_ARGUMENT_BOUNDARY = "=([{,: \t\n"


def _string_end(text: str, index: int) -> int | None:
    """Return the index just past a Python string literal at ``index``.

    Returns
    -------
    int | None
        The end of the literal, or ``None`` when no literal starts there.
    """
    prefix = 0
    while (
        prefix < 2
        and index + prefix < len(text)
        and text[index + prefix] in _STRING_PREFIXES
    ):
        prefix += 1
    quote_index = index + prefix
    if quote_index >= len(text) or text[quote_index] not in "'\"":
        return None
    has_boundary = index == 0 or text[index - 1] in _ARGUMENT_BOUNDARY
    if prefix == 0 and not has_boundary:
        return None
    quote = text[quote_index]
    delimiter = quote * 3 if text.startswith(quote * 3, quote_index) else quote
    position = quote_index + len(delimiter)
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text.startswith(delimiter, position):
            return position + len(delimiter)
        if len(delimiter) == 1 and text[position] == "\n":
            return None
        position += 1
    return None


def _mask_strings(text: str) -> str:
    """Return ``text`` with string-literal contents replaced by spaces."""
    masked = list(text)
    index = 0
    while index < len(text):
        end = _string_end(text, index)
        if end is None:
            index += 1
            continue
        for position in range(index, end):
            masked[position] = " "
        index = end
    return "".join(masked)


def _scoped_arguments(text: str) -> cabc.Iterator[str]:
    """Yield the argument text of every balanced ``scoped(...)`` call."""
    masked = _mask_strings(text)
    for match in SCOPED_CALL.finditer(masked):
        depth = 1
        for index in range(match.end(), len(masked)):
            char = masked[index]
            depth += char == "("
            depth -= char == ")"
            if depth == 0:
                yield masked[match.end() : index]
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


def _cuprum_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return the names bound to ``scoped`` and to the ``cuprum`` module.

    Returns
    -------
    tuple[set[str], set[str]]
        Imported or aliased names for ``scoped``, and for ``cuprum`` itself.
    """
    scoped_names = {"scoped"}
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "cuprum":
            for alias in node.names:
                if alias.name == "scoped":
                    scoped_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "cuprum":
                    module_names.add(alias.asname or alias.name)
    return scoped_names, module_names


def _is_scoped_call(
    node: ast.AST, scoped_names: set[str], module_names: set[str]
) -> typ.TypeGuard[ast.Call]:
    """Return whether ``node`` calls Cuprum's ``scoped`` function."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in scoped_names
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "scoped"
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
    )


def _scoped_keywords(
    node: ast.AST, scoped_names: set[str], module_names: set[str]
) -> list[ast.keyword]:
    """Return a ``scoped()`` call's keywords, or nothing for other nodes."""
    return node.keywords if _is_scoped_call(node, scoped_names, module_names) else []


def _python_passes_retired_keyword(tree: ast.AST) -> bool:
    """Return whether parsed code gives ``scoped()`` the retired keyword."""
    scoped_names, module_names = _cuprum_bindings(tree)
    return any(
        keyword.arg == KEYWORD
        for node in ast.walk(tree)
        for keyword in _scoped_keywords(node, scoped_names, module_names)
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
            'usage: scoped(catalogue=build(")"), ' + KEYWORD + "=mate.allowlist)",
            True,
            id="text-with-parenthesis-inside-a-string",
        ),
        pytest.param(
            "from cuprum import scoped as scope\nscope(" + KEYWORD + "=mate.allowlist)",
            True,
            id="imported-alias",
        ),
        pytest.param(
            "import cuprum\ncuprum.scoped(" + KEYWORD + "=mate.allowlist)",
            True,
            id="qualified-module",
        ),
        pytest.param(
            "import cuprum as c\nc.scoped(" + KEYWORD + "=mate.allowlist)",
            True,
            id="qualified-aliased-module",
        ),
        pytest.param(
            "import other\nother.scoped(" + KEYWORD + "=mate.allowlist)",
            False,
            id="unrelated-module",
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
