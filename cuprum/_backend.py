"""Stream backend dispatcher with environment variable support.

Resolves which stream backend (Rust or pure Python) to use at runtime based
on the ``CUPRUM_STREAM_BACKEND`` environment variable and the availability of
the optional Rust extension.  The resolved backend is cached for the lifetime
of the process.

Example
-------
backend = get_stream_backend()
if backend is StreamBackend.RUST:
    # use Rust stream operations
    ...
"""

from __future__ import annotations

import enum
import functools
import os

from cuprum import _rust_backend

_ENV_VAR = "CUPRUM_STREAM_BACKEND"
_RUST_AVAILABILITY_FOR_TESTING: bool | None = None


class StreamBackend(enum.StrEnum):
    """Identifiers for the available stream backend implementations.

    Members
    -------
    AUTO
        Automatically select the best available backend.
    RUST
        Force the Rust extension backend.
    PYTHON
        Force the pure Python backend.
    """

    AUTO = "auto"
    RUST = "rust"
    PYTHON = "python"


def _read_backend_env() -> StreamBackend:
    """Read and validate the stream backend from the environment.

    Returns
    -------
    StreamBackend
        The requested backend parsed from ``CUPRUM_STREAM_BACKEND``, or
        ``StreamBackend.AUTO`` when the variable is unset or empty.

    Raises
    ------
    ValueError
        If the environment variable contains an unrecognized value.
    """
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    if not raw:
        return StreamBackend.AUTO
    try:
        return StreamBackend(raw)
    except ValueError:
        valid = ", ".join(sorted(v.value for v in StreamBackend))
        msg = f"invalid {_ENV_VAR} value {raw!r}; expected one of: {valid}"
        raise ValueError(msg) from None


@functools.lru_cache(maxsize=1)
def _check_rust_available() -> bool:
    """Return whether the Rust extension is available, with caching.

    Returns
    -------
    bool
        ``True`` when the native Rust extension is importable and reports
        availability.

    Notes
    -----
    The result is cached for the lifetime of the process.  Call
    ``_check_rust_available.cache_clear()`` to force a re-check (useful in
    tests).
    """
    if _RUST_AVAILABILITY_FOR_TESTING is not None:
        return _RUST_AVAILABILITY_FOR_TESTING
    return _rust_backend.is_available()


def set_rust_availability_for_testing(
    *,
    is_available: bool | None,
) -> None:
    """Override Rust availability checks for tests.

    Parameters
    ----------
    is_available : bool | None
        ``True`` forces Rust-available behaviour, ``False`` forces
        unavailable behaviour, and ``None`` restores normal probing.
    """
    global _RUST_AVAILABILITY_FOR_TESTING
    _RUST_AVAILABILITY_FOR_TESTING = is_available
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


@functools.lru_cache(maxsize=1)
def get_stream_backend() -> StreamBackend:
    """Resolve the active stream backend.

    The resolution algorithm follows the precedence defined in the design
    document (Section 13.4):

    1. Read ``CUPRUM_STREAM_BACKEND`` from the environment.
    2. If ``python``, return ``StreamBackend.PYTHON`` immediately.
    3. If ``rust``, check availability and raise ``ImportError`` when the
       extension is missing.
    4. If ``auto`` (the default), return ``StreamBackend.RUST`` when the
       extension is available, otherwise ``StreamBackend.PYTHON``.

    Returns
    -------
    StreamBackend
        The resolved backend — either ``StreamBackend.RUST`` or
        ``StreamBackend.PYTHON``.  ``StreamBackend.AUTO`` is never returned;
        it is always resolved to a concrete backend.

    Raises
    ------
    ImportError
        If the backend is forced to ``rust`` but the Rust extension is
        unavailable.
    ValueError
        If ``CUPRUM_STREAM_BACKEND`` contains an unrecognized value.

    Notes
    -----
    The resolved backend is cached for the lifetime of the process.  Call
    ``get_stream_backend.cache_clear()`` (and
    ``_check_rust_available.cache_clear()``) to force re-resolution (useful
    in tests).
    """
    requested = _read_backend_env()

    match requested:
        case StreamBackend.PYTHON:
            return StreamBackend.PYTHON
        case StreamBackend.RUST:
            if _check_rust_available():
                return StreamBackend.RUST
            msg = (
                f"Rust stream backend requested via {_ENV_VAR}=rust "
                "but the Rust extension is not available"
            )
            raise ImportError(msg)
        case StreamBackend.AUTO:
            try:
                if _check_rust_available():
                    return StreamBackend.RUST
            except ImportError:
                pass
            return StreamBackend.PYTHON


__all__ = ["StreamBackend", "get_stream_backend"]
