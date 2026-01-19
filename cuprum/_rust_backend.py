"""Optional Rust backend availability probe."""

from __future__ import annotations

import importlib


def is_available() -> bool:
    """Return True when the native Rust extension can be imported."""
    try:
        native = importlib.import_module("cuprum._rust_backend_native")
    except ImportError:
        return False
    return bool(native.is_available())


__all__ = ["is_available"]
