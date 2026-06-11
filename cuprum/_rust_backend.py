"""Optional Rust backend availability probe."""

from __future__ import annotations

import importlib


def is_available() -> bool:
    """Report whether the native Rust extension is available.

    Returns
    -------
    bool
        True when the native module can be imported and reports availability.

    Notes
    -----
    This is the raw, uncached import probe. It does *not* honour the backend
    availability cache or the ``set_rust_availability_for_testing`` override;
    callers that need the value governing dispatch must use
    :func:`cuprum.rust.is_rust_available` (or
    :func:`cuprum._backend._check_rust_available`) instead.

    Only missing-module errors for ``cuprum._rust_backend_native`` are treated
    as a signal that the extension is unavailable. Other import failures are
    re-raised so extension load errors remain visible.
    """
    try:
        native = importlib.import_module("cuprum._rust_backend_native")
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == (
            "cuprum._rust_backend_native"
        ):
            return False
        raise
    return bool(native.is_available())


__all__ = ["is_available"]
