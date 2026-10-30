"""Reject the retired keyword-scope call form in tracked files."""

from __future__ import annotations

import re
from pathlib import Path

from cuprum import GIT, ExecutionContext, sh

ROOT = Path(__file__).resolve().parents[2]
# Assembled from fragments so this test does not flag its own source text.
FORBIDDEN = re.compile(r"scoped\s*\(\s*" + "allowlist" + r"\s*=")


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


def test_no_tracked_file_passes_allowlist_directly_to_scoped() -> None:
    """No tracked file calls ``scoped()`` with a bare ``allowlist=`` keyword."""
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if FORBIDDEN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "the retired keyword-scope call form remains in: " + ", ".join(offenders)
    )
