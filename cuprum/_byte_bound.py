"""One validator for the byte bounds a caller configures.

Two public options bound retained bytes: ``BoundedDelivery.max_queued_bytes``
for presentation and ``start_bytes(max_buffered_bytes=...)`` for live
observation. They have the same contract — a strictly positive integer, with
``bool`` rejected even though Python treats it as an ``int`` — and they differ
only in the field name the error reports. Stating the rule once keeps the two
gates from drifting on what a valid bound is; the field name is a parameter so
each caller's message still points at the option the caller set.
"""

from __future__ import annotations

__all__ = ["_validate_positive_byte_bound"]


def _validate_positive_byte_bound(value: object, *, field: str) -> int:
    """Require a strictly positive integer byte bound.

    Parameters
    ----------
    value : object
        The caller-supplied bound to check.
    field : str
        The public option name the error must name.

    Returns
    -------
    int
        The same value, narrowed to ``int``.

    Raises
    ------
    TypeError
        If *value* is not an integer, or is a ``bool``.
    ValueError
        If *value* is not positive.

    Examples
    --------
    >>> _validate_positive_byte_bound(1024, field="max_buffered_bytes")
    1024
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{field} must be an integer"
        raise TypeError(msg)
    if value <= 0:
        msg = f"{field} must be positive"
        raise ValueError(msg)
    return value
