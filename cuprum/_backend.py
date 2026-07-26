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
import logging
import os

from cuprum import _rust_backend

_ENV_VAR = "CUPRUM_STREAM_BACKEND"
_LOGGER = logging.getLogger(__name__)
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


def _parse_backend_value(raw: str) -> StreamBackend:
    """Parse a raw ``CUPRUM_STREAM_BACKEND`` value into a backend.

    This is the pure parsing core behind :func:`_read_backend_env`: it takes the
    raw string directly rather than reading the environment, so it can be
    property tested by injecting values without mutating ``os.environ``.

    Parameters
    ----------
    raw:
        The raw value (e.g. from the environment). Leading/trailing whitespace
        is stripped and the value is lower-cased before matching.

    Returns
    -------
    StreamBackend
        The parsed backend, or ``StreamBackend.AUTO`` when ``raw`` is empty or
        whitespace-only.

    Raises
    ------
    ValueError
        If ``raw`` is a non-empty, unrecognized value.
    """
    normalised = raw.strip().lower()
    if not normalised:
        return StreamBackend.AUTO
    try:
        return StreamBackend(normalised)
    except ValueError:
        valid = ", ".join(sorted(v.value for v in StreamBackend))
        msg = f"invalid {_ENV_VAR} value {normalised!r}; expected one of: {valid}"
        raise ValueError(msg) from None


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
    return _parse_backend_value(os.environ.get(_ENV_VAR, ""))


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
        _LOGGER.debug(
            "resolved Rust availability from testing override",
            extra={
                "event": "cuprum.rust_availability_resolved",
                "rust_available": _RUST_AVAILABILITY_FOR_TESTING,
                "source": "testing_override",
            },
        )
        return _RUST_AVAILABILITY_FOR_TESTING
    is_available = _rust_backend.is_available()
    _LOGGER.debug(
        "resolved Rust availability from raw probe",
        extra={
            "event": "cuprum.rust_availability_resolved",
            "rust_available": is_available,
            "source": "raw_probe",
        },
    )
    return is_available


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
    _LOGGER.debug(
        "updated Rust availability testing override and cleared resolver caches",
        extra={
            "event": "cuprum.rust_availability_override_updated",
            "override_active": is_available is not None,
            "rust_availability_override": is_available,
        },
    )


def _log_stream_backend_resolution(
    requested: StreamBackend,
    resolved: StreamBackend,
    *,
    rust_available: bool | None,
) -> None:
    """Log the outcome of stream backend resolution."""
    _LOGGER.debug(
        "resolved stream backend",
        extra={
            "event": "cuprum.stream_backend_resolved",
            "requested_backend": requested.value,
            "resolved_backend": resolved.value,
            "rust_available": rust_available,
        },
    )


def _resolve_backend(
    requested: StreamBackend,
    *,
    rust_available: bool | None,
) -> StreamBackend:
    """Map a requested backend and probe outcome to a concrete backend.

    This is the pure resolution core: it performs no probing, logging, or
    caching, so its decision can be reasoned about (and property tested) in
    isolation from the environment.

    Parameters
    ----------
    requested:
        The backend requested via ``CUPRUM_STREAM_BACKEND`` (``AUTO``,
        ``RUST``, or ``PYTHON``).
    rust_available:
        ``True``/``False`` for a resolved availability probe, or ``None`` when
        the probe was skipped (``PYTHON``) or failed (``AUTO``).

    Returns
    -------
    StreamBackend
        Always a concrete backend — ``StreamBackend.RUST`` or
        ``StreamBackend.PYTHON``. ``StreamBackend.AUTO`` is never returned.

    Raises
    ------
    ImportError
        If ``RUST`` is forced but ``rust_available`` is not truthy.

    Examples
    --------
    >>> _resolve_backend(StreamBackend.AUTO, rust_available=False)
    <StreamBackend.PYTHON: 'python'>
    """
    match requested:
        case StreamBackend.PYTHON:
            return StreamBackend.PYTHON
        case StreamBackend.RUST:
            if rust_available:
                return StreamBackend.RUST
            msg = (
                f"Rust stream backend requested via {_ENV_VAR}=rust "
                "but the Rust extension is not available"
            )
            raise ImportError(msg)
        case StreamBackend.AUTO:
            return StreamBackend.RUST if rust_available else StreamBackend.PYTHON
        case _:
            # A new StreamBackend member must be handled explicitly above;
            # falling off the end would silently return None.
            msg = f"unreachable backend {requested!r}"
            raise AssertionError(msg)


def _probe_rust_availability(requested: StreamBackend) -> bool | None:
    """Probe Rust availability for ``requested``, honouring its failure policy.

    ``PYTHON`` never probes (returns ``None``). ``AUTO`` tolerates a probe
    ``ImportError`` and falls back to ``None``. ``RUST`` lets a probe
    ``ImportError`` propagate, matching the forced-backend contract.
    """
    if requested is StreamBackend.PYTHON:
        return None
    try:
        return _check_rust_available()
    except ImportError:
        if requested is not StreamBackend.AUTO:
            raise
        _LOGGER.debug(
            "Rust availability probe failed in auto mode; falling back to Python",
            extra={
                "event": "cuprum.stream_backend_auto_probe_failed",
                "requested_backend": requested.value,
            },
        )
        return None


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
    rust_available = _probe_rust_availability(requested)
    try:
        resolved = _resolve_backend(requested, rust_available=rust_available)
    except ImportError:
        _LOGGER.warning(
            "Rust stream backend requested but unavailable",
            extra={
                "event": "cuprum.stream_backend_unavailable",
                "requested_backend": requested.value,
                "rust_available": rust_available,
            },
        )
        raise
    _log_stream_backend_resolution(requested, resolved, rust_available=rust_available)
    return resolved


__all__ = ["StreamBackend", "get_stream_backend"]
