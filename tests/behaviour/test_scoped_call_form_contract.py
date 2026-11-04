"""Reject the retired keyword-scope call form in tracked files."""

from __future__ import annotations

import stat
import typing as typ
from pathlib import Path

import pytest

from cuprum import GIT, ExecutionContext, sh
from tests.behaviour._retired_call_scan import KEYWORD, _passes_retired_keyword

if typ.TYPE_CHECKING:
    import collections.abc as cabc

ROOT = Path(__file__).resolve().parents[2]


def _tracked_files() -> list[Path]:
    """Return every tracked regular file below the repository root."""
    result = sh.make(GIT)("ls-files", "-z").run_sync(context=ExecutionContext(cwd=ROOT))
    assert result.ok, f"git ls-files failed: {result.stderr}"
    paths: list[Path] = []
    uninspectable: list[str] = []
    for name in result.stdout.split("\0"):
        if not name:
            continue
        path = ROOT / name
        try:
            mode = path.stat().st_mode
        except OSError:
            uninspectable.append(name)
            continue
        if stat.S_ISREG(mode):
            paths.append(path)
    assert not uninspectable, f"cannot inspect tracked paths: {uninspectable}"
    return paths


def _read_tracked_text(path: Path) -> str:
    """Return ``path``'s text without letting an I/O failure pass unnoticed.

    A tracked file that is not valid UTF-8 is still scanned rather than
    skipped: the retired keyword is ASCII, so replacement characters cannot
    hide it. Read errors propagate with the offending path intact.

    Returns
    -------
    str
        The file's text, with undecodable bytes replaced.
    """
    return path.read_bytes().decode("utf-8", errors="replace")


def _retired_call_offenders(paths: cabc.Iterable[Path]) -> list[str]:
    """Return the repository-relative paths whose text uses the retired form."""
    return [
        str(path.relative_to(ROOT))
        for path in paths
        if _passes_retired_keyword(_read_tracked_text(path))
    ]


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
            "the runner's scoped(" + KEYWORD + "=m) isn't ok",
            True,
            id="apostrophe-after-a-word-ending-in-a-prefix-letter",
        ),
        pytest.param(
            "call f(x, r" + "'scoped(" + KEYWORD + "=m)')",
            False,
            id="real-prefixed-literal-at-a-token-start",
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
            'usage: f"{scoped(' + KEYWORD + '=mate.allowlist)}"',
            True,
            id="f-string-replacement-field",
        ),
        pytest.param(
            'usage: f"{scoped(ScopeConfig(' + KEYWORD + '=m))}"',
            False,
            id="f-string-with-nested-scope-config",
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
    offenders = _retired_call_offenders(_tracked_files())
    assert not offenders, (
        "the retired keyword-scope call form remains in: " + ", ".join(offenders)
    )
