#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cyclopts>=4.0"]
# ///
"""Synchronize Cuprum's derived tool pins from their canonical declarations.

``pyproject.toml``'s ``dev`` dependency group is the one authoritative
declaration of the Ruff, ty and maturin versions Cuprum develops and builds
with. The Makefile defaults, the workflow-level ``env`` pins, and the composite
action's ``maturin-version`` default are plain Make or YAML text that cannot
read that file, so this command derives those literals from the canonical
group. A routine dependency update then cannot leave one of them behind.

``uv.lock`` is never rewritten: uv owns the lockfile, and resolving it can also
move unrelated transitive packages. The command reports outstanding lock work
as ``uv lock`` instead. The built wheel's ``Generator`` header is asserted
against the canonical pin before normalization by the wheel contract tests, so
no wheel snapshot needs regenerating either.

Usage::

    make check-tool-pins
    make sync-tool-pins
    uv run --no-project --python 3.13 --with cyclopts \
        python -m scripts.sync_tool_pins check

``check`` never writes and exits non-zero when a site has drifted. ``sync``
rewrites only the named literal values, preserving quoting, comments and
formatting, and is idempotent: a second run changes no bytes.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import typing as typ
from pathlib import Path

from cyclopts import App

from scripts.tool_pins import (
    LOCKFILE,
    PinError,
    Site,
    canonical_pins,
    check_pins,
    lock_drift,
    match_site,
    read_text,
    sites_by_file,
)

if typ.TYPE_CHECKING:
    import re

ROOT = Path(__file__).resolve().parents[1]


def _apply_replacements(
    text: str,
    replacements: list[tuple[re.Match[str], str]],
) -> str:
    """Replace each match's value group, working back to front."""
    ordered = sorted(replacements, key=lambda item: item[0].start(), reverse=True)
    for match, value in ordered:
        start, end = match.span("value")
        text = f"{text[:start]}{value}{text[end:]}"
    return text


def _atomic_write_text(path: Path, payload: bytes) -> None:
    """Replace a file's bytes atomically, preserving its permission bits."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary.replace(path)
        replaced = True
    finally:
        if not replaced:
            temporary.unlink(missing_ok=True)


def _rewrite_file(
    root: Path, relative: str, entries: list[tuple[Site, str]], pins: dict[str, str]
) -> Path | None:
    """Rewrite one file's derived literals, returning it when its bytes changed."""
    path = root / relative
    text = read_text(root, relative)
    replacements = [(match_site(text, site), pins[tool]) for site, tool in entries]
    updated = _apply_replacements(text, replacements)
    if updated == text:
        return None
    _atomic_write_text(path, updated.encode("utf-8"))
    return path


def _report_sync(
    root: Path, changed: list[Path], pins: dict[str, str], stream: typ.TextIO
) -> None:
    """Print what the sync did, and what it deliberately left to uv."""
    if changed:
        for path in changed:
            print(f"Updated {path.relative_to(root)}", file=stream)
        print("Derived tool pins now match pyproject.toml.", file=stream)
    else:
        print("Derived tool pins already match pyproject.toml.", file=stream)
    if lock_drift(root, pins):
        print(
            f"{LOCKFILE} is owned by uv and was left unchanged: "
            "run `uv lock` to resolve the lockfile.",
            file=stream,
        )
    print(
        "The wheel snapshot needs no regeneration: the WHEEL Generator header "
        "is asserted against this pin before normalization.",
        file=stream,
    )


def sync_pins(root: Path = ROOT, stream: typ.TextIO = sys.stdout) -> tuple[Path, ...]:
    """Rewrite derived literals from the canonical pins.

    Parameters
    ----------
    root:
        Repository root.
    stream:
        Destination for the human-readable summary.

    Returns
    -------
    tuple[Path, ...]
        The files whose bytes changed, in processing order.

    Raises
    ------
    PinError
        If a canonical pin or a site declaration is missing, malformed or
        ambiguous. No file is written when this is raised during validation;
        a write failure restores the original bytes because each file is
        replaced atomically.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the pin readers this delegates to
    pins = canonical_pins(root)
    changed = [
        path
        for relative, entries in sites_by_file().items()
        if (path := _rewrite_file(root, relative, entries, pins)) is not None
    ]
    _report_sync(root, changed, pins, stream)
    return tuple(changed)


def run_check(root: Path = ROOT, stream: typ.TextIO = sys.stdout) -> int:
    """Report drift and return the process exit status."""
    try:
        drifts = check_pins(root)
    except PinError as error:
        print(f"error: {error}", file=stream)
        return 1
    if not drifts:
        print("Tool pins match pyproject.toml.", file=stream)
        return 0
    print("Tool pin drift detected:", file=stream)
    for drift in drifts:
        print(
            f"  {drift.location}: expected {drift.expected}, found {drift.found}",
            file=stream,
        )
    print("Run `make sync-tool-pins` to rewrite the derived literals.", file=stream)
    if any(drift.location.startswith(f"{LOCKFILE} ") for drift in drifts):
        print(
            f"{LOCKFILE} is owned by uv: run `uv lock` to resolve the lockfile.",
            file=stream,
        )
    return 1


def run_sync(root: Path = ROOT, stream: typ.TextIO = sys.stdout) -> int:
    """Synchronize pins and return the process exit status."""
    try:
        sync_pins(root, stream=stream)
    except PinError as error:
        print(f"error: {error}", file=stream)
        return 1
    return 0


app = App(name="sync-tool-pins", help=__doc__.splitlines()[0])


@app.command
def check(root: Path = ROOT) -> None:
    """Report derived pin drift without writing any file."""
    raise SystemExit(run_check(root))


@app.command
def sync(root: Path = ROOT) -> None:
    """Rewrite derived pin literals from the canonical declarations."""
    raise SystemExit(run_sync(root))


if __name__ == "__main__":
    app()
