"""Decide whether a missing native extension should fail the test session.

The extension-gated test modules skip when `cuprum._rust_backend_native` is
unavailable. That is right locally — most contributors do not build the native
extension for every change — but wrong in CI, where a job that never built it
reports a green run indistinguishable from one that exercised the whole
Python/Rust boundary.

The decision lives here, apart from the root ``conftest.py`` that applies it,
so it can be tested directly: a root conftest is shadowed by the per-package
one and cannot be imported by name from a test module.
"""

from __future__ import annotations

import typing as typ

REQUIRE_EXTENSION_ENV: typ.Final[str] = "CUPRUM_REQUIRE_RUST_EXTENSION"


def missing_extension_message(*, required: bool, available: bool) -> str | None:
    """Return the failure message when the extension is required but absent.

    Parameters
    ----------
    required : bool
        Whether ``CUPRUM_REQUIRE_RUST_EXTENSION`` is set to a non-empty value.
    available : bool
        Whether the native Rust extension is usable, per
        ``cuprum._rust_backend.is_available``.

    Returns
    -------
    str or None
        The message explaining the failure, or ``None`` when the run may
        proceed — either because the extension was not required, or because it
        is present.

    Examples
    --------
    >>> missing_extension_message(required=False, available=False) is None
    True
    >>> missing_extension_message(required=True, available=True) is None
    True
    >>> "make develop" in missing_extension_message(
    ...     required=True, available=False
    ... )
    True
    """
    if not required or available:
        return None
    return (
        f"{REQUIRE_EXTENSION_ENV} is set, but the native Rust extension "
        "(cuprum._rust_backend_native) is unavailable, so every "
        "extension-gated test would skip silently. Build it with "
        "`make develop` before running the suite. Unset the variable to allow "
        "skipping."
    )
