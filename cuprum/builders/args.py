"""Typed argument helpers for core builders."""

from __future__ import annotations

import enum
import os
import re
import typing as typ
from pathlib import Path, PurePath

SafePath = typ.NewType("SafePath", str)
GitRef = typ.NewType("GitRef", str)

_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_WINDOWS_ABS_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class PathRejection(enum.Enum):
    """Reason a raw path string fails :func:`safe_path` validation.

    Each member's value is the exact error message raised for that category,
    so callers (and property tests) can reason about the rejection category
    rather than only pass/fail. Members are declared in the order the checks
    are applied; :func:`classify_path_string` returns the first match.
    """

    EMPTY = "SafePath cannot be empty"
    NUL = "SafePath cannot contain NUL characters"
    PARENT_SEGMENT = "SafePath cannot contain '..' segments"
    NOT_ABSOLUTE = "SafePath requires an absolute path by default"


class GitRefRejection(enum.Enum):
    """Reason a string fails :func:`git_ref` validation.

    Each member's value is the exact error message raised for that category.
    Members are declared in the order the checks are applied;
    :func:`classify_git_ref` returns the first match.
    """

    EMPTY = "GitRef cannot be empty"
    LEADING_DASH = "GitRef cannot start with '-'"
    WHITESPACE = "GitRef cannot contain whitespace"
    SLASH_BOUNDARY = "GitRef cannot start or end with '/'"
    LOCK_SUFFIX = "GitRef cannot end with '.lock'"
    DOT_SUFFIX = "GitRef cannot end with '.'"
    PARENT_SEQUENCE = "GitRef cannot contain '..'"
    DOUBLE_SLASH = "GitRef cannot contain '//' sequences"
    AT_BRACE = "GitRef cannot contain '@{' sequences"
    UNSUPPORTED_CHARS = "GitRef contains unsupported characters"


def _convert_to_string(value: str | Path | os.PathLike[str]) -> str:
    """Convert path-like inputs into a string for validation."""
    try:
        result = os.fspath(value)
    except TypeError:
        msg = f"SafePath expects str or Path, got {type(value).__name__}"
        raise TypeError(msg) from None
    if isinstance(result, bytes):
        msg = f"SafePath expects str or Path, got {type(value).__name__}"
        raise TypeError(msg)
    return PurePath(result).as_posix() if isinstance(value, Path) else result


def classify_path_string(
    raw_value: str,
    *,
    allow_relative: bool,
) -> PathRejection | None:
    """Classify why a raw path string is rejected, or ``None`` if valid.

    Parameters
    ----------
    raw_value:
        Path string to classify (already converted from any path-like input).
    allow_relative:
        When True, relative paths are permitted.

    Returns
    -------
    PathRejection | None
        The first failing category, or ``None`` when ``raw_value`` is a valid
        :class:`SafePath` candidate.

    Examples
    --------
    >>> classify_path_string("", allow_relative=False)
    <PathRejection.EMPTY: 'SafePath cannot be empty'>
    >>> classify_path_string("/etc/hosts", allow_relative=False) is None
    True
    """
    path = PurePath(raw_value)
    is_absolute = path.is_absolute() or bool(_WINDOWS_ABS_PATTERN.match(raw_value))
    if not raw_value:
        return PathRejection.EMPTY
    if "\x00" in raw_value:
        return PathRejection.NUL
    if ".." in path.parts:
        return PathRejection.PARENT_SEGMENT
    if not allow_relative and not is_absolute:
        return PathRejection.NOT_ABSOLUTE
    return None


def _validate_path_string(raw_value: str, *, allow_relative: bool) -> None:
    """Validate raw path strings before building a SafePath."""
    rejection = classify_path_string(raw_value, allow_relative=allow_relative)
    if rejection is not None:
        raise ValueError(rejection.value)


def safe_path(value: str | Path, *, allow_relative: bool = False) -> SafePath:
    """Validate and normalize a filesystem path.

    Parameters
    ----------
    value:
        Path value to validate.
    allow_relative:
        When True, relative paths are permitted. Defaults to False.

    Returns
    -------
    SafePath
        Normalized path string.
    """
    raw_value = _convert_to_string(value)
    _validate_path_string(raw_value, allow_relative=allow_relative)
    return SafePath(PurePath(raw_value).as_posix())


def classify_git_ref(value: str) -> GitRefRejection | None:
    """Classify why a git ref string is rejected, or ``None`` if valid.

    Parameters
    ----------
    value:
        Ref string to classify.

    Returns
    -------
    GitRefRejection | None
        The first failing category, or ``None`` when ``value`` is a valid
        :class:`GitRef` candidate.

    Examples
    --------
    >>> classify_git_ref("main") is None
    True
    >>> classify_git_ref("feature..bug")
    <GitRefRejection.PARENT_SEQUENCE: "GitRef cannot contain '..'">
    """
    checks = (
        (not value, GitRefRejection.EMPTY),
        (value.startswith("-"), GitRefRejection.LEADING_DASH),
        (any(char.isspace() for char in value), GitRefRejection.WHITESPACE),
        (
            value.startswith("/") or value.endswith("/"),
            GitRefRejection.SLASH_BOUNDARY,
        ),
        (value.endswith(".lock"), GitRefRejection.LOCK_SUFFIX),
        (value.endswith("."), GitRefRejection.DOT_SUFFIX),
        (".." in value, GitRefRejection.PARENT_SEQUENCE),
        ("//" in value, GitRefRejection.DOUBLE_SLASH),
        ("@{" in value, GitRefRejection.AT_BRACE),
    )
    for condition, rejection in checks:
        if condition:
            return rejection
    if _GIT_REF_PATTERN.fullmatch(value) is None:
        return GitRefRejection.UNSUPPORTED_CHARS
    return None


def _validate_git_ref(value: str) -> None:
    """Validate git reference strings before wrapping as GitRef."""
    rejection = classify_git_ref(value)
    if rejection is not None:
        raise ValueError(rejection.value)


def git_ref(value: str) -> GitRef:
    """Validate a git ref name or object name.

    Parameters
    ----------
    value:
        Ref value to validate.

    Returns
    -------
    GitRef
        Validated ref string.
    """
    if not isinstance(value, str):
        msg = f"GitRef expects str, got {type(value).__name__}"
        raise TypeError(msg)

    _validate_git_ref(value)
    return GitRef(value)


# The public API surface is the validators and their return types. The
# rejection classifiers and their reason enums are developer-facing helpers
# (a reasoning seam for in-tree callers and property tests), documented in
# docs/developers-guide.md, so they are importable but intentionally omitted
# from ``__all__`` rather than advertised as end-user API.
__all__ = [
    "GitRef",
    "SafePath",
    "git_ref",
    "safe_path",
]
