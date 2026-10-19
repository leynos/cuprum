"""The construction-time rules that police ``RunOutputOptions`` fields.

``cuprum.sh.output`` owns the option vocabulary: which fields a run takes,
what each one means, and how the convenience flags synthesize a presentation
sink. This module owns the other half, the checks that a field carries the
*kind* of value the field's type promises: a decoded-character bound that is a
positive integer or ``None``, and flags that are real ``bool`` values rather
than truthy objects.

Each rule reads either one field value or the assembled options object, and
none of them needs to know what any other field means.
``RunOutputOptions.__post_init__`` calls them before it spawns anything, so a
malformed option fails at construction rather than at the first recorded line
or the first framed run. Keeping them beside the options --- but out of
``output.py``, which is at the module ceiling --- is the same split
``stdio_rules`` makes for the stdio targets: the vocabulary stays in one module
and the policing in a sibling.

Both rules are re-exported by ``cuprum.sh.output``.
``_validate_convenience_flags`` moved from ``output.py`` and keeps its original
name, so existing importers resolve the same function object;
``_validate_max_line_chars`` is new in this module.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.sh.output import RunOutputOptions

__all__ = [
    "_validate_convenience_flags",
    "_validate_max_line_chars",
]


def _validate_max_line_chars(value: int | None) -> None:
    """Reject a line-retention bound that is not a positive integer or ``None``.

    ``bool`` is an ``int`` to ``isinstance``, so ``True`` would otherwise pass
    as a bound of one character; it is rejected here exactly as it is for
    ``max_echo_line_bytes``.

    Parameters
    ----------
    value : int | None
        The configured ``max_line_chars`` bound.

    Raises
    ------
    ValueError
        If the value is neither a positive integer nor ``None``.
    """
    if value is None:
        return
    is_positive_int = isinstance(value, int) and not isinstance(value, bool)
    if is_positive_int and value > 0:
        return
    msg = (
        "RunOutputOptions max_line_chars must be a positive integer or None, "
        f"got {value!r}"
    )
    raise ValueError(msg)


def _validate_convenience_flags(options: RunOutputOptions) -> None:
    """Reject non-``bool`` values for the convenience opt-in flags.

    Both flags gate whether workflow commands are written, so a value that
    merely coerces to a truthy bool --- the integer ``1``, say --- would frame
    a run on the strength of something the caller never documented as a flag.
    ``max_echo_line_bytes`` draws the same line for the same reason.

    Parameters
    ----------
    options : RunOutputOptions
        The options whose ``group`` and ``annotate_failure`` are checked.

    Raises
    ------
    ValueError
        If either flag is not a ``bool``.
    """
    for name, value in (
        ("group", options.group),
        ("annotate_failure", options.annotate_failure),
    ):
        if not isinstance(value, bool):
            msg = f"RunOutputOptions {name} must be a bool, got {value!r}"
            # Issue #375 expressly specifies ValueError for these flags.
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
