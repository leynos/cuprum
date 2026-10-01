"""Validation and advisory checking for executable paths.

This module owns the "is this string an acceptable executable path?" question in
isolation from the question "which path should this logical program run?". The
companion module :mod:`cuprum.executable_binding` owns the second question and
re-exports the names here, so callers reach the whole feature through one
import.

Both modules are pure: neither reads or writes
:class:`~cuprum.context.CuprumContext`, and neither imports the context package
at runtime. That keeps the dependency direction acyclic, so the context package
and the execution layer can both depend on them.

Validation is deliberately shallow. :func:`executable_path` rejects strings that
cannot be a usable executable path, and :func:`advisory_path_rejection` reports
whether a path currently exists as an executable file. Neither makes any claim
about the identity of the binary that eventually runs; see
:mod:`cuprum.executable_binding` for the limits of that claim.
"""

from __future__ import annotations

import enum
import os
import re
import typing as typ
from pathlib import Path, PurePath

if typ.TYPE_CHECKING:
    from cuprum.program import Program

ExecutablePath = typ.NewType("ExecutablePath", str)
"""A validated string naming an executable to run.

The string is not necessarily absolute; whether an absolute path is required is
decided by :func:`executable_path`'s ``allow_relative`` flag, not by the type.
"""

_WINDOWS_ABS_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class PathBindingRejection(enum.Enum):
    """Reason a raw string fails executable-path validation or its advisory check.

    Each member's value is the exact error message raised for that category, so
    callers can reason about the rejection category rather than only pass/fail.
    Members are declared in the order the checks are applied; the classifiers
    return the first match.
    """

    EMPTY = "ExecutablePath cannot be empty"
    NUL = "ExecutablePath cannot contain NUL characters"
    PARENT_SEGMENT = "ExecutablePath cannot contain '..' segments"
    NOT_ABSOLUTE = "ExecutablePath requires an absolute path by default"
    NOT_FOUND = "ExecutablePath does not exist"
    NOT_EXECUTABLE = "ExecutablePath is not executable"


class InvalidExecutableBindingError(ValueError):
    """Raised when an executable path or a binding for it cannot be built.

    The error lives beside the rejection vocabulary rather than beside the
    binding type, because both :func:`executable_path` here and
    ``executable_binding`` in :mod:`cuprum.executable_binding` raise it, and the
    binding module already depends on this one.

    Parameters
    ----------
    program : Program | None
        The logical identity the binding was being built for, or ``None`` when
        the path was validated on its own.
    path : str
        The path the caller supplied, quoted verbatim.
    reason : PathBindingRejection
        The classified reason for the rejection.

    Attributes
    ----------
    program : Program | None
        The logical identity the binding was being built for, or ``None``.
    path : str
        The rejected path, quoted verbatim.
    reason : PathBindingRejection
        The classified reason for the rejection.
    """

    def __init__(
        self,
        program: Program | None,
        path: str,
        reason: PathBindingRejection,
    ) -> None:
        """Record the program, the path, and the classified reason."""
        self.program = program
        self.path = path
        self.reason = reason
        subject = "Path" if program is None else f"Program '{program}'"
        super().__init__(f"{subject} cannot be bound to {path!r}: {reason.value}")


def classify_executable_path(
    raw_value: str,
    *,
    allow_relative: bool,
) -> PathBindingRejection | None:
    """Classify why a raw executable-path string is rejected, or ``None``.

    Mirrors :func:`cuprum.builders.args.classify_path_string`: the checks run in
    declaration order, and the first match wins, so a NUL-bearing traversal is
    reported as a NUL rather than a traversal.

    Parameters
    ----------
    raw_value : str
        Path string to classify.
    allow_relative : bool
        When True, relative paths are permitted.

    Returns
    -------
    PathBindingRejection | None
        The rejection category, or ``None`` when the value is acceptable.

    Examples
    --------
    >>> classify_executable_path("/opt/tools/tool", allow_relative=False) is None
    True
    >>> classify_executable_path("bin/tool", allow_relative=False).name
    'NOT_ABSOLUTE'
    >>> classify_executable_path("bin/tool", allow_relative=True) is None
    True
    """
    path = PurePath(raw_value)
    is_absolute = path.is_absolute() or bool(_WINDOWS_ABS_PATTERN.match(raw_value))
    if not raw_value:
        return PathBindingRejection.EMPTY
    if "\x00" in raw_value:
        return PathBindingRejection.NUL
    if ".." in path.parts:
        return PathBindingRejection.PARENT_SEGMENT
    if not allow_relative and not is_absolute:
        return PathBindingRejection.NOT_ABSOLUTE
    return None


def executable_path(
    value: str | Path,
    *,
    allow_relative: bool = False,
) -> ExecutablePath:
    """Validate and normalize a string into an :data:`ExecutablePath`.

    Accepts a :class:`~pathlib.Path` for symmetry with
    :func:`cuprum.builders.args.safe_path`, but unlike ``safe_path`` it does not
    require an absolute path unless ``allow_relative`` is left at its default.
    Validation is purely syntactic; the filesystem is not consulted, so a path
    may be validated and bound before the file it names exists.

    Parameters
    ----------
    value : str | Path
        The path to validate.
    allow_relative : bool, optional
        When True, relative paths are permitted. Defaults to False, which
        requires an absolute path.

    Returns
    -------
    ExecutablePath
        The normalized path.

    Raises
    ------
    InvalidExecutableBindingError
        The path is empty, contains a NUL byte or a ``..`` segment, or is
        relative while ``allow_relative`` is False. The error carries the
        classified reason and a ``program`` of ``None``, because no logical
        identity was supplied.

    Examples
    --------
    >>> executable_path("/opt//tools/./tool")
    '/opt/tools/tool'
    >>> executable_path("bin/tool", allow_relative=True)
    'bin/tool'
    """
    raw_value = coerce_path_string(value)
    rejection = classify_executable_path(raw_value, allow_relative=allow_relative)
    if rejection is not None:
        raise InvalidExecutableBindingError(None, raw_value, rejection)
    return ExecutablePath(PurePath(raw_value).as_posix())


def coerce_path_string(value: str | Path) -> str:
    """Coerce a string or path-like value into the string to validate.

    Parameters
    ----------
    value : str | Path
        The value to coerce.

    Returns
    -------
    str
        The value's string form.

    Raises
    ------
    TypeError
        The value is not a string or path-like object.

    Examples
    --------
    >>> coerce_path_string("/opt/tools/tool")
    '/opt/tools/tool'
    """
    if isinstance(value, str):
        return value
    try:
        result = os.fspath(value)
    except TypeError:
        msg = f"ExecutablePath expects str or Path, got {type(value).__name__}"
        raise TypeError(msg) from None
    if isinstance(result, bytes):
        msg = f"ExecutablePath expects str or Path, got {type(value).__name__}"
        raise TypeError(msg)
    return result


def advisory_path_rejection(raw_value: str) -> PathBindingRejection | None:
    """Report whether *raw_value* currently looks like a runnable executable.

    This is an advisory probe, not a guarantee. It answers "at the instant it
    was called, did this path exist as an executable file?" and says nothing
    about what will be executed later; see the module docstring of
    :mod:`cuprum.executable_binding` for the limits of that claim.

    A bare name with no directory component is left unchecked, because the
    platform resolves it against ``PATH`` and Cuprum does not replicate that
    search. Only absolute paths and relative paths containing a separator are
    probed. The check is skipped entirely on Windows, where the execute bit is
    not part of the file's identity.

    Parameters
    ----------
    raw_value : str
        The path to probe.

    Returns
    -------
    PathBindingRejection | None
        ``NOT_FOUND`` when the path is absent, ``NOT_EXECUTABLE`` when it exists
        as something other than an executable file, or ``None`` when nothing
        was found to report.

    Examples
    --------
    >>> advisory_path_rejection("tool") is None
    True
    """
    if os.name == "nt":
        return None
    candidate = Path(raw_value)
    if not candidate.is_absolute() and os.sep not in raw_value:
        return None
    if not candidate.exists():
        return PathBindingRejection.NOT_FOUND
    # Directories carry an execute bit, but running one is not what a caller
    # binding an executable means; report them alongside plain files.
    if not candidate.is_file() or not os.access(raw_value, os.X_OK):
        return PathBindingRejection.NOT_EXECUTABLE
    return None


__all__ = [
    "ExecutablePath",
    "InvalidExecutableBindingError",
    "PathBindingRejection",
    "advisory_path_rejection",
    "classify_executable_path",
    "coerce_path_string",
    "executable_path",
]
