"""Public helpers for the optional Rust extension."""

from __future__ import annotations

from cuprum._backend import _check_rust_available


def is_rust_available() -> bool:
    """Report whether the optional Rust extension is available.

    Returns
    -------
    bool
        True when the native Rust extension can be imported and reports
        availability.

    Notes
    -----
    This is the single public entry point for the extension-availability
    question. It delegates to :func:`cuprum._backend._check_rust_available`,
    the same cached, override-aware resolver that governs stream-backend
    dispatch, so the answer observed through this public API always agrees
    with the backend the dispatcher actually selects — including the
    ``set_rust_availability_for_testing`` override. The raw, uncached import
    probe lives in :func:`cuprum._rust_backend.is_available`; prefer this
    wrapper unless a deliberately uncached check is required.
    """
    return _check_rust_available()


__all__ = ["is_rust_available"]
