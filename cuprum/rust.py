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
    the same cached resolver that stream-backend dispatch uses when it needs
    to ask whether Rust is available. Testing overrides installed with
    :func:`cuprum._backend.set_rust_availability_for_testing` short-circuit
    that resolver before the raw import probe runs and clear both backend
    caches, so the new answer is visible immediately. Backend selection can
    still differ from this availability answer: for example, forced Python
    mode selects ``StreamBackend.PYTHON`` even when this function returns
    ``True``. Cached answers only drift if a long-lived interpreter survives a
    wheel swap or another out-of-band import-path or installation-state change.
    The raw, uncached import probe lives in
    :func:`cuprum._rust_backend.is_available`; prefer this wrapper unless a
    deliberately uncached check is required.
    """
    return _check_rust_available()


__all__ = ["is_rust_available"]
