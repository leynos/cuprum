"""Argument-vector construction helpers shared by ``sh.make`` builders.

This module hosts the small, allocation-free helpers that turn positional and
keyword Python values into an argv tuple using the same rules that
``cuprum.sh.make`` builders apply. The ``cuprum.sh`` package re-exports
``build_argv``.
"""

from __future__ import annotations

from pathlib import Path

type _ArgValue = str | int | float | bool | Path

__all__ = [
    "build_argv",
]


def _stringify_arg(value: _ArgValue) -> str:
    """Stringify supported argv values and reject ``bytes`` with TypeError."""
    if value is None:
        # None is disallowed because it is almost always a mistake in CLI argv
        # construction; callers must represent missing values themselves (for
        # example, by omitting the flag) before invoking sh.make.
        msg = "None is not a valid argv element for sh.make"
        raise TypeError(msg)
    if isinstance(value, bytes):
        msg = f"bytes is not a valid argv element for sh.make: {value!r}"
        raise TypeError(msg)
    return str(value)


def _serialize_kwargs(kwargs: dict[str, _ArgValue]) -> tuple[str, ...]:
    """Serialize keyword arguments to CLI-style ``--flag=value`` entries."""
    flags: list[str] = []
    for key, value in kwargs.items():
        normalized_key = key.replace("_", "-")
        flags.append(f"--{normalized_key}={_stringify_arg(value)}")
    return tuple(flags)


def build_argv(*args: _ArgValue, **kwargs: _ArgValue) -> tuple[str, ...]:
    """Build an argv tuple using the same rules as ``sh.make`` builders.

    Parameters
    ----------
    *args
        Positional text, integer, float, boolean, or path values. They are
        stringified in the order supplied and appear before generated keyword
        flags.
        **kwargs
        Keyword text, integer, float, boolean, or path values. Each key is
        normalized by replacing underscores with hyphens, then serialized as
        ``--flag=value`` in insertion order. ``None`` and ``bytes`` values
        raise ``TypeError`` in positional and keyword positions.

    Returns
    -------
    tuple[str, ...]
        The constructed argv tuple, excluding the program name.

    Examples
    --------
    >>> build_argv("status", porcelain=True, branch="main")
    ('status', '--porcelain=True', '--branch=main')
    """
    positional = tuple(_stringify_arg(arg) for arg in args)
    flags = _serialize_kwargs(kwargs)
    return positional + flags
