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
