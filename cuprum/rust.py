"""Public Rust-availability entry points for Cuprum.

This module intentionally exposes only the cached, dispatch-aligned probe via
``is_rust_available()``, re-exported from the backend resolver so the public
boundary and stream-backend dispatch always agree. Testing overrides installed
through the backend clear its caches, so a new answer is visible immediately.
"""

from __future__ import annotations

from cuprum._backend import _check_rust_available as is_rust_available

__all__ = ["is_rust_available"]
