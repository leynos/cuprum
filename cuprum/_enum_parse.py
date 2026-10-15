"""Coerce a raw caller value to an enumerated policy member.

Several public entry points accept a policy either as its enum member or as
the string spelling of one, and each must reject an unrecognized spelling
rather than letting it resolve to a different policy or fail later inside the
subprocess machinery. The validation is identical every time — construct the
member, and on refusal name the field and the accepted values — so it lives
here rather than being restated per policy.

This module is a leaf: it imports nothing from ``cuprum``, so a policy module
may depend on it without adding a layer to the package's import graph.
"""

from __future__ import annotations

import enum


def parse_enum_member[E: enum.Enum](
    policy: type[E],
    raw: E | str,
    *,
    field: str,
) -> E:
    """Return ``raw`` as a member of ``policy``, rejecting unknown spellings.

    Parameters
    ----------
    policy : type[enum.Enum]
        The enumerated policy to resolve ``raw`` against.
    raw : enum.Enum | str
        A member of ``policy``, or its string value.
    field : str
        Name the refusal reports the value under, as the caller's own
        configuration key spells it.

    Returns
    -------
    E
        The resolved member, unchanged when ``raw`` already is one.

    Raises
    ------
    ValueError
        If ``raw`` is a string that names no member of ``policy``.

    Examples
    --------
    >>> import enum
    >>> class Mode(enum.Enum):
    ...     FAST = "fast"
    >>> parse_enum_member(Mode, "fast", field="mode")
    <Mode.FAST: 'fast'>
    >>> parse_enum_member(Mode, "slow", field="mode")
    Traceback (most recent call last):
    ...
    ValueError: invalid mode 'slow'; expected one of: fast
    """
    try:
        return policy(raw)
    except ValueError:
        valid = ", ".join(member.value for member in policy)
        msg = f"invalid {field} {raw!r}; expected one of: {valid}"
        raise ValueError(msg) from None
