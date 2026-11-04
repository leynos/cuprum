"""Retain redacted Cargo preparation diagnostics for failed runs.

A Cargo failure during snapshot preparation used to report only an exit code,
so a maintainer could not tell a missing registry index from an expired
credential. This module records what Cargo said, in a run-local JSON-lines
file, after removing every value a caller's environment marks as secret.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "DIAGNOSTICS_FILE_NAME",
    "CargoDiagnostic",
    "is_secret_environment_name",
    "record_cargo_diagnostic",
    "redact_environment_values",
)

#: Run-local file name. It sits beside ``timings.jsonl`` so whichever run
#: directory the workflow archives also archives the diagnostics.
DIAGNOSTICS_FILE_NAME = "cargo-diagnostics.jsonl"

#: Environment-name fragments whose values must never reach a retained record.
_SECRET_NAME_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")

#: Shorter values are indistinguishable from ordinary words, so replacing them
#: would corrupt otherwise useful diagnostics without protecting anything.
_MINIMUM_SECRET_LENGTH = 8

_REDACTED = "<redacted>"


@dc.dataclass(frozen=True, slots=True)
class CargoDiagnostic:
    """Describe one Cargo invocation and the streams it produced.

    Attributes
    ----------
    operation:
        Stable name for the preparation step, such as ``cargo version``.
    working_directory:
        Directory Cargo ran in.
    cargo_home:
        Cache home visible to the invocation, or ``None`` when unset.
    exit_code:
        Process exit status.
    elapsed_seconds:
        Monotonic duration of the invocation.
    stdout:
        Captured standard output after secret values were removed.
    stderr:
        Captured standard error after secret values were removed.
    """

    operation: str
    working_directory: str
    cargo_home: str | None
    exit_code: int
    elapsed_seconds: float
    stdout: str
    stderr: str


def is_secret_environment_name(name: str) -> bool:
    """Report whether a variable's name marks its value as secret.

    Returns
    -------
    bool
        ``True`` when the upper-cased name contains a secret marker.
    """
    upper_name = name.upper()
    return any(marker in upper_name for marker in _SECRET_NAME_MARKERS)


def redact_environment_values(
    text: str,
    environment: cabc.Mapping[str, str],
) -> str:
    """Replace every secret-looking environment value that appears in ``text``.

    Values shorter than :data:`_MINIMUM_SECRET_LENGTH` are left alone: they can
    collide with ordinary words, and redacting them would hide the diagnostic
    without protecting a usable credential.

    Returns
    -------
    str
        The text with each secret value replaced by ``<redacted>``.
    """
    for name, value in environment.items():
        if not is_secret_environment_name(name):
            continue
        if len(value) < _MINIMUM_SECRET_LENGTH:
            continue
        text = text.replace(value, _REDACTED)
    return text


def record_cargo_diagnostic(path: Path, diagnostic: CargoDiagnostic) -> None:
    """Append one diagnostic record as a JSON line.

    The record is written even for a successful invocation so a retained file
    shows what preparation actually did, and so a later failure has the
    preceding context beside it.

    Parameters
    ----------
    path:
        Run-local JSON-lines file that receives the record.
    diagnostic:
        The already-redacted record to append.

    Raises
    ------
    OSError
        If the file cannot be opened or written.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from Path.open and write.
    record = {
        "operation": diagnostic.operation,
        "working_directory": diagnostic.working_directory,
        "cargo_home": diagnostic.cargo_home,
        "exit_code": diagnostic.exit_code,
        "elapsed_seconds": diagnostic.elapsed_seconds,
        "stdout": diagnostic.stdout,
        "stderr": diagnostic.stderr,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
