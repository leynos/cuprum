#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.14"
# dependencies = ["cyclopts==4.25.2", "tomlkit==0.15.1"]
# ///
"""Blocking code-duplication gate over the pinned nose detector.

The gate runs the pinned ``nose`` binary with the repository's ``[tool.nose]``
settings, removes families covered by reasoned ``[tool.duplication_gate]``
allow entries, and fails while unsuppressed families remain. Stale allow
entries are reported so that resolved duplication does not leave dead
configuration behind.

``scripts/duplication_allowlist.py`` documents the allow-entry key syntax
and matching rules; ``scripts/nose_detector.py`` owns the detector itself.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/duplication_gate.py``), the merged revision of PR #276, under the
ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import sys
import tomllib
from collections import abc as cabc
from pathlib import Path

import cyclopts

from scripts.duplication_allowlist import (
    AllowEntry,
    append_allow_entry,
    load_allowlist,
    validate_key,
)
from scripts.nose_detector import PYPROJECT, load_settings, run_detector
from scripts.nose_schema import Finding, GateConfigError, GateExecutionError

app = cyclopts.App(help="Run or configure the code-duplication gate.")

type AllowlistReader = cabc.Callable[[Path], tuple[AllowEntry, ...]]
type FindingDetector = cabc.Callable[[], list[Finding]]


def partition_findings(
    findings: cabc.Sequence[Finding],
    allowlist: cabc.Sequence[AllowEntry],
) -> tuple[list[Finding], list[Finding], list[AllowEntry]]:
    """Split findings into blocking and allowed, and spot stale entries.

    Parameters
    ----------
    findings : collections.abc.Sequence[Finding]
        Normalized detector findings.
    allowlist : collections.abc.Sequence[AllowEntry]
        Reasoned exceptions from the repository configuration.

    Returns
    -------
    tuple[list[Finding], list[Finding], list[AllowEntry]]
        Blocking findings, silenced findings, and allow entries that no
        longer cover any finding.
    """
    blocking: list[Finding] = []
    allowed: list[Finding] = []
    used: set[int] = set()
    for finding in findings:
        matched = False
        for position, entry in enumerate(allowlist):
            if entry.matches(finding):
                used.add(position)
                matched = True
        (allowed if matched else blocking).append(finding)
    stale = [entry for position, entry in enumerate(allowlist) if position not in used]
    return blocking, allowed, stale


def detect_findings() -> list[Finding]:
    """Run the pinned detector with the repository's ``[tool.nose]`` settings.

    Returns
    -------
    list[Finding]
        Normalized duplication families emitted by the pinned detector.

    Notes
    -----
    Configuration and execution errors from the settings loader and detector
    propagate to the CLI boundary.
    """
    return run_detector(load_settings(PYPROJECT))


def _report(
    blocking: list[Finding], allowed: list[Finding], stale: list[AllowEntry]
) -> None:
    """Print the gate outcome in a concise, actionable form."""
    for entry in stale:
        joined = " ~ ".join(entry.keys)
        print(f"stale allow entry ({joined}): remove it; the duplication is gone")
    if not blocking:
        suffix = f"; {len(allowed)} allowed by reasoned exceptions" if allowed else ""
        print(f"duplication gate passed{suffix}")
        return
    print(f"duplicate code: {len(blocking)} unsuppressed family/families")
    for finding in blocking:
        print(f"  {finding.label} ({finding.witness}, value {finding.value:.1f})")
    print(
        "Extract the shared logic into one helper, or record a considered "
        "exception:\n  make duplication-allow FIRST='<path[::name]>' "
        "[SECOND='<path[::name]>'] REASON='<why this stays>'"
    )


def _read_allowlist(allowlist_reader: AllowlistReader) -> tuple[AllowEntry, ...]:
    """Read the reasoned allowlist, naming unreadable configuration."""
    try:
        return allowlist_reader(PYPROJECT)
    except GateConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot load duplication allowlist: {error}"
        raise GateExecutionError(msg) from error


def _detect_findings(detector: FindingDetector) -> list[Finding]:
    """Run the detector, separating execution failures from bad configuration."""
    try:
        return detector()
    except GateConfigError:
        raise
    except OSError as error:
        msg = f"nose detector failed: {error}"
        raise GateExecutionError(msg) from error
    except (TypeError, ValueError) as error:
        raise GateConfigError(str(error)) from error


def _check_inputs(
    *,
    allowlist_reader: AllowlistReader,
    detector: FindingDetector,
) -> tuple[tuple[AllowEntry, ...], list[Finding]]:
    """Load the gate inputs with explicit local-environment failures."""
    return (_read_allowlist(allowlist_reader), _detect_findings(detector))


@app.command
def check() -> None:
    """Run the blocking duplication gate and exit non-zero on findings.

    The command reads the repository's fixed ``pyproject.toml`` path and the
    detector executes with an explicit repository working directory; it does
    not mutate the caller's process directory.

    Raises
    ------
    SystemExit
        With status 1 for blocking findings or status 2 for malformed
        configuration.
    """
    try:
        allowlist, findings = _check_inputs(
            allowlist_reader=load_allowlist,
            detector=detect_findings,
        )
    except GateConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    blocking, allowed, stale = partition_findings(findings, allowlist)
    _report(blocking, allowed, stale)
    if blocking:
        raise SystemExit(1)


@app.command
def allow(
    *,
    first: str,
    second: list[str] | None = None,
    reason: str,
) -> None:
    """Record one reasoned exception in ``[tool.duplication_gate]``.

    Parameters
    ----------
    first : str
        Location key (``path`` or ``path::name``) of the first or only member.
    second : list[str] | None
        Further location keys; when supplied, the entry silences only families
        whose every location matches one of the listed keys.
    reason : str
        Reviewable justification for keeping the duplication.

    Raises
    ------
    SystemExit
        If the reason is empty or a location key is malformed.
    """
    if not reason.strip():
        print("REASON must not be empty", file=sys.stderr)
        raise SystemExit(2)
    keys = (first, *(second or ()))
    try:
        for key in keys:
            validate_key(key, context=f"'{key}'")
        append_allow_entry(PYPROJECT, keys=keys, reason=reason)
    except (GateConfigError, OSError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    print(f"recorded duplication exception for {' ~ '.join(keys)}")


if __name__ == "__main__":
    app()
