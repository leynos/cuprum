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
from pathlib import Path

# Pin ``scripts`` to this file's own directory *before* importing from it.
#
# ``scripts`` has no ``__init__.py``, so it is a namespace package and CPython
# builds its search path from every ``sys.path`` entry holding a ``scripts``
# directory. A development install of the application contributes the checkout
# root, so a gate launched from an out-of-tree workspace still resolved
# ``scripts.<name>`` — and with it ``PYPROJECT``, the detector location, and the
# allow list — to the application checkout, then reported the result as if it
# had read the workspace. Handling the entry point's own directory first makes
# an out-of-tree gate read its own tree or fail to import at all; both are
# visible, and silently reading another tree is not. This statement is exempt
# from the import-order rules precisely because it is a ``sys.path`` change.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomllib
from collections import abc as cabc

import cyclopts

from scripts.duplication_allowlist import (
    AllowEntry,
    append_allow_entry,
    key_matches,
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


def _stale_line(entry: AllowEntry, findings: cabc.Sequence[Finding]) -> str:
    """Explain why an entry covers nothing, without asserting more than it can.

    An entry that covered a family which has since grown a location still
    matches nothing, but the duplication is emphatically not gone. The
    distinction that matters to whoever reads the line is whether to widen the
    entry or delete it, so it is drawn from evidence rather than guessed at.
    Growth is only readable when the entry named more than one location: only
    then does the surviving family have to match *every* key to show the entry
    was superseded. A single-key entry has no such evidence available — its key
    is a glob, so any family it still touches may equally be one the entry
    never named — and it is reported as a coincidence rather than as growth.
    Claiming growth on a coincidental match would be a confident instruction to
    widen an entry for duplication that is not the entry's subject.

    Returns
    -------
    str
        The report line for *entry*.
    """
    joined = " ~ ".join(entry.keys)
    grown = _superseding_family(entry, findings)
    if grown is not None:
        return (
            f"stale allow entry ({joined}): the family it covered has grown "
            f"to {len(grown.locations)} locations, so the entry no longer "
            f"covers all of them; widen it to match {grown.label}"
        )
    coincidental = _coincidental_family(entry, findings)
    if coincidental is not None:
        return (
            f"stale allow entry ({joined}): remove it; no family in this scan "
            f"matches the entry, and the overlap at {coincidental.label} is a "
            f"path-glob coincidence rather than the family it covered"
        )
    return (
        f"stale allow entry ({joined}): remove it; no family in this scan "
        f"reports any of its locations"
    )


def _matched_keys(entry: AllowEntry, finding: Finding) -> list[str]:
    """Return the entry keys that any location in *finding* matches."""
    return [
        key
        for key in entry.keys
        if any(key_matches(key, location) for location in finding.locations)
    ]


def _superseding_family(
    entry: AllowEntry,
    findings: cabc.Sequence[Finding],
) -> Finding | None:
    """Return the family that outgrew *entry*, when one is readable.

    Only a multi-key entry can show this: it named several locations, so a
    family matching *every* one of them is the family the entry covered, now
    carrying a member the entry does not name. A single-key entry has no such
    evidence, because its key is a glob rather than a list.

    Returns
    -------
    Finding | None
        The outgrown family, or ``None`` when growth is not readable.
    """
    if len(entry.keys) > 1:
        return next(
            (
                finding
                for finding in findings
                if len(_matched_keys(entry, finding)) == len(entry.keys)
            ),
            None,
        )
    return None


def _coincidental_family(
    entry: AllowEntry,
    findings: cabc.Sequence[Finding],
) -> Finding | None:
    """Return the first family *entry* merely overlaps, if any."""
    return next(
        (finding for finding in findings if _matched_keys(entry, finding)), None
    )


def _report(
    blocking: list[Finding],
    allowed: list[Finding],
    stale: list[AllowEntry],
    findings: cabc.Sequence[Finding],
) -> None:
    """Print the gate outcome in a concise, actionable form."""
    for entry in stale:
        print(_stale_line(entry, findings))
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
        "[MEMBERS='<path[::name]> ...'] REASON='<why this stays>'\n"
        "MEMBERS carries every location past the first, space-separated; "
        "`unit` entries need only FIRST."
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
    _report(blocking, allowed, stale, findings)
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
