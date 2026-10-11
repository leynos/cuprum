"""Argument-vector construction helpers shared by ``sh.make`` builders.

This module hosts the small, allocation-free helpers that turn positional and
keyword Python values into an argv tuple using the same rules that
``cuprum.sh.make`` builders apply. The ``cuprum.sh`` package re-exports
``build_argv``.
"""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

type ArgValue = str | int | float | bool | Path | os.PathLike[str]
"""Values a ``sh.make`` builder accepts as positional or keyword arguments.

The alias is public so callers can annotate their own wrappers and fixtures
with the same argument domain the builders enforce, without repeating the
union or reaching for ``object``. ``None`` is deliberately absent: a builder
that receives it raises :exc:`TypeError` at call time.

``os.PathLike[str]`` is named rather than the bare ``os.PathLike`` so the
annotation states that only text-returning paths are accepted. The protocol is
structural, so the runtime cannot enforce the ``str`` parameter from the
annotation alone; :func:`_stringify_arg` therefore re-checks the ``os.fspath``
result and rejects a ``__fspath__`` that returns bytes.
"""

__all__ = [
    "ArgValue",
    "build_argv",
]

# Runtime tuple derived from ``ArgValue`` so validation and the published
# annotation cannot drift. ``ArgValue`` is a PEP 695 alias, whose
# ``__value__`` carries the union that ``typing.get_args`` can unpack.
#
# Each member is reduced to a runtime-checkable class: ``os.PathLike[str]`` is
# a parameterized generic, and ``isinstance``/``issubclass`` raise
# ``TypeError`` for one. ``typing.get_origin`` returns ``os.PathLike`` for it
# and ``None`` for the plain classes, so the reduction is a no-op for those.
_ARG_TYPES = tuple(
    typ.get_origin(member) or member for member in typ.get_args(ArgValue.__value__)
)


def _stringify_arg(value: ArgValue) -> str:
    """Convert a single argument value into an argv-safe string.

    This is the choke point where the annotated ``ArgValue`` domain and the
    accepted runtime domain are kept identical.

    Parameters
    ----------
    value : ArgValue
        The value to serialize.

    Returns
    -------
    str
        ``str(value)``, so a boolean yields ``"True"`` or ``"False"``. A
        path-like value yields its ``os.fspath`` representation instead, which
        is the same text for a :class:`pathlib.Path` and the correct
        conversion for any other implementation of the protocol.

    Raises
    ------
    TypeError
        If ``value`` is ``None``, is a ``bytes`` value, or is not a ``str``,
        ``int``, ``float``, ``bool``, or path-like value. A path-like whose
        ``__fspath__`` returns bytes is rejected as ``bytes``. ``None`` keeps
        its own dedicated message; every other rejected type is named in the
        error.
    """
    match value:
        case None:
            # None is disallowed because it is almost always a mistake in CLI
            # argv construction; callers must represent missing values
            # themselves (for example, by omitting the flag) before invoking
            # sh.make.
            msg = "None is not a valid argv element for sh.make"
            raise TypeError(msg)
        case _ if issubclass(type(value), bytes):
            # bytes is a sequence of integers rather than text, so it is
            # rejected ahead of the domain check to quote the offending value:
            # the caller almost always passed an encoded payload where a str
            # was intended. ``type(value)`` is deliberate; see the domain arm.
            msg = f"bytes is not a valid argv element for sh.make: {value!r}"
            raise TypeError(msg)
        case _ if not issubclass(type(value), _ARG_TYPES):
            # str() would happily render any object, silently putting a repr
            # such as "<object object at 0x...>" on a real command line.
            # Rejecting the type keeps the runtime domain identical to the
            # annotated one.
            #
            # ``type(value)`` rather than ``isinstance`` is deliberate. A
            # value may override ``__class__`` to claim membership in the
            # domain, and ``isinstance`` consults that property: ``Mock(spec=
            # int)`` and a hand-rolled object exposing ``__class__ = int``
            # both satisfy ``isinstance(value, int)`` while remaining their
            # own unsupported type. The real type is not spoofable, and
            # ``issubclass`` still admits genuine subclasses, so a ``str`` or
            # ``Path`` subclass keeps working.
            msg = f"{type(value).__name__} is not a valid argv element for sh.make"
            raise TypeError(msg)
        case os.PathLike():
            # ``os.fspath`` is the documented conversion for the protocol and
            # is what path consumers use, so a str-returning implementation
            # yields the same text a Path would.
            #
            # The result still needs a type check: ``os.PathLike`` is
            # structural, so ``issubclass(type(value), os.PathLike)`` accepts
            # a ``__fspath__`` that returns bytes, and ``os.fspath`` would
            # hand that straight back. Returning early keeps the bytes rule
            # that applies everywhere else on the command line.
            text = os.fspath(value)
            if isinstance(text, bytes):
                msg = f"bytes is not a valid argv element for sh.make: {text!r}"
                raise TypeError(msg)
            return text
        case _:
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
        Positional text, integer, float, boolean, or path values. They are
        stringified in the order supplied and appear before generated keyword
        flags.

    **kwargs
        Keyword text, integer, float, boolean, or path values. Each key is
        normalized by replacing underscores with hyphens, then serialized as
        ``--flag=value`` in insertion order.

    Returns
    -------
    tuple[str, ...]
        The constructed argv tuple, excluding the program name.

    Raises
    ------
    TypeError
        If any value is ``None``, is a ``bytes`` value, or is not a ``str``,
        ``int``, ``float``, ``bool``, or path-like value, in either position.
        A path-like whose ``__fspath__`` returns bytes is rejected as
        ``bytes``. Rejected ``bytes`` values are quoted in the message.

    Examples
    --------
    >>> build_argv("status", porcelain=True, branch="main")
    ('status', '--porcelain=True', '--branch=main')
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError propagates from _stringify_arg
    positional = tuple(_stringify_arg(arg) for arg in args)
    flags = _serialize_kwargs(kwargs)
    return positional + flags
