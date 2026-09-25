"""Argument-vector construction helpers shared by ``sh.make`` builders.

This module hosts the small, allocation-free helpers that turn positional and
keyword Python values into an argv tuple using the same rules that
``cuprum.sh.make`` builders apply. The ``cuprum.sh`` package re-exports
``build_argv``.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

type ArgValue = str | int | float | bool | Path
"""Values a ``sh.make`` builder accepts as positional or keyword arguments.

The alias is public so callers can annotate their own wrappers and fixtures
with the same argument domain the builders enforce, without repeating the
union or reaching for ``object``. ``None`` is deliberately absent: a builder
that receives it raises :exc:`TypeError` at call time.
"""

__all__ = [
    "ArgValue",
    "build_argv",
]

# Runtime tuple derived from ``ArgValue`` so validation and the published
# annotation cannot drift. ``ArgValue`` is a PEP 695 alias, whose
# ``__value__`` carries the union that ``typing.get_args`` can unpack.
_ARG_TYPES = typ.get_args(ArgValue.__value__)


def _stringify_arg(value: ArgValue) -> str:
    """Convert values into argv-safe strings."""
    if value is None:
        # None is disallowed because it is almost always a mistake in CLI argv
        # construction; callers must represent missing values themselves (for
        # example, by omitting the flag) before invoking sh.make.
        msg = "None is not a valid argv element for sh.make"
        raise TypeError(msg)
    if not isinstance(value, _ARG_TYPES):
        # str() would happily render any object, silently putting a repr such
        # as "<object object at 0x...>" on a real command line. Rejecting the
        # type keeps the runtime domain identical to the annotated one.
        msg = f"{type(value).__name__} is not a valid argv element for sh.make"
        raise TypeError(msg)
    return str(value)


def _serialize_kwargs(kwargs: dict[str, ArgValue]) -> tuple[str, ...]:
    """Serialize keyword arguments to CLI-style ``--flag=value`` entries."""
    flags: list[str] = []
    for key, value in kwargs.items():
        normalized_key = key.replace("_", "-")
        flags.append(f"--{normalized_key}={_stringify_arg(value)}")
    return tuple(flags)


def build_argv(*args: ArgValue, **kwargs: ArgValue) -> tuple[str, ...]:
    """Build an argv tuple using the same rules as ``sh.make`` builders.

    Parameters
    ----------
    *args
        Positional argument values. Values are stringified with ``str()`` in
        the order supplied and appear before generated keyword flags.
    **kwargs
        Keyword flag values. Each key is normalized by replacing underscores
        with hyphens, then serialized as ``--flag=value`` in insertion order.

    Returns
    -------
    tuple[str, ...]
        The constructed argv tuple, excluding the program name.

    Raises
    ------
    TypeError
        If any value is ``None``, or is not a ``str``, ``int``, ``float``,
        ``bool``, or :class:`pathlib.Path`, in either position.

    Examples
    --------
    >>> build_argv("status", porcelain=True, branch="main")
    ('status', '--porcelain=True', '--branch=main')
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError propagates from _stringify_arg
    positional = tuple(_stringify_arg(arg) for arg in args)
    flags = _serialize_kwargs(kwargs)
    return positional + flags
