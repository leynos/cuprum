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

    Notes
    -----
    This is the single public entry point for the extension-availability
    question. It delegates to :func:`cuprum._backend._check_rust_available`,
    the same cached, override-aware resolver that stream-backend dispatch uses
    when it needs to ask whether Rust is available. Backend selection can still
    differ from this availability answer: for example, forced Python mode
    selects ``StreamBackend.PYTHON`` even when this function returns ``True``.
    The raw, uncached import probe lives in
    :func:`cuprum._rust_backend.is_available`; prefer this wrapper unless a
    deliberately uncached check is required.
    """
    return _check_rust_available()


__all__ = ["is_rust_available"]
