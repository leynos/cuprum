"""Typed argument helpers for core builders."""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path, PurePath

SafePath = typ.NewType("SafePath", str)
GitRef = typ.NewType("GitRef", str)

_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


def _convert_to_string(value: str | Path) -> str:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, str):
        return value
    msg = f"SafePath expects str or Path, got {type(value).__name__}"
    raise TypeError(msg)


def _validate_path_string(raw_value: str, *, allow_relative: bool) -> None:
    checks = (
        (raw_value == "", "SafePath cannot be empty"),
        ("\x00" in raw_value, "SafePath cannot contain NUL characters"),
        (
            ".." in PurePath(raw_value).parts,
            "SafePath cannot contain '..' segments",
        ),
        (
            not allow_relative and not PurePath(raw_value).is_absolute(),
            "SafePath requires an absolute path by default",
        ),
    )
    for condition, message in checks:
        if condition:
            msg = message
            raise ValueError(msg)


def safe_path(value: str | Path, *, allow_relative: bool = False) -> SafePath:
    """Validate and normalise a filesystem path.

    Parameters
    ----------
    value:
        Path value to validate.
    allow_relative:
        When True, relative paths are permitted. Defaults to False.

    Returns
    -------
    SafePath
        Normalised path string.
    """
    raw_value = _convert_to_string(value)
    _validate_path_string(raw_value, allow_relative=allow_relative)
    return SafePath(PurePath(raw_value).as_posix())


def _validate_git_ref(value: str) -> None:
    checks = (
        (value == "", "GitRef cannot be empty"),
        (value.startswith("-"), "GitRef cannot start with '-'"),
        (
            any(char.isspace() for char in value),
            "GitRef cannot contain whitespace",
        ),
        (
            value.startswith("/") or value.endswith("/"),
            "GitRef cannot start or end with '/'",
        ),
        (value.endswith(".lock"), "GitRef cannot end with '.lock'"),
        (value.endswith("."), "GitRef cannot end with '.'"),
        (".." in value, "GitRef cannot contain '..'"),
        ("//" in value, "GitRef cannot contain '//' sequences"),
        ("@{" in value, "GitRef cannot contain '@{' sequences"),
    )
    for condition, message in checks:
        if condition:
            msg = message
            raise ValueError(msg)

    if _GIT_REF_PATTERN.fullmatch(value) is None:
        msg = "GitRef contains unsupported characters"
        raise ValueError(msg)


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


__all__ = ["GitRef", "SafePath", "git_ref", "safe_path"]
