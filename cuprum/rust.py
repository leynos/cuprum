"""Public helpers for the optional Rust extension."""

from __future__ import annotations

from . import _rust_backend


def is_rust_available() -> bool:
    """Report whether the optional Rust extension is available.

    Returns
    -------
    bool
        True when the native Rust extension can be imported and reports
        availability.

    Notes
    -----
    This function is a public wrapper around
    :func:`cuprum._rust_backend.is_available`, which performs the underlying
    import probe.
    """
    return _rust_backend.is_available()


__all__ = ["is_rust_available"]
