"""Provide pure helpers used while constructing program catalogues.

The helpers keep path-specific coercion separate from catalogue indexing.

Examples
--------
>>> derive_project_name(("/usr/bin/git", "cargo"))
'git-cargo'
"""

from __future__ import annotations

import typing as typ
from pathlib import Path, PureWindowsPath

from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def coerce_program(raw: Program | str) -> Program:
    """Return input as Program for type narrowing; no transformation performed."""
    return Program(raw)


def derive_project_name(programs: cabc.Iterable[Program | str]) -> str:
    r"""Return a deterministic project name from the programs' base names.

    Absolute paths reduce to their final path component. Windows drive paths
    reduce the same way on every host, while a POSIX literal backslash keeps it.

    Returns
    -------
    str
        The programs' base names joined with ``-``.
    """
    return "-".join(
        PureWindowsPath(program).name
        if PureWindowsPath(program).drive
        else Path(program).name
        for program in programs
    )
