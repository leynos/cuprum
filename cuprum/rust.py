"""Public Rust-availability entry points for Cuprum.

This module intentionally exposes only the cached, dispatch-aligned probe via
``is_rust_available()``.
"""

from __future__ import annotations

from cuprum._backend import _check_rust_available


def is_rust_available() -> bool:
    """Report whether the optional Rust extension is available.

    Returns
    -------
    bool
        True when the native Rust extension can be imported and reports
        availability.

    Raises
    ------
    TypeError
        If the canonical backend resolver violates its boolean return
        contract.

    Notes
    -----
    This public boundary uses the same cached resolver as stream-backend
    dispatch and validates that resolver's runtime contract. Testing overrides
    installed through the backend clear its caches, so the new answer is
    visible immediately.
    """
    availability = _check_rust_available()
    if not isinstance(availability, bool):
        msg = "Rust availability resolver must return bool"
        raise TypeError(msg)
    return availability


__all__ = ["is_rust_available"]
