"""Shared pytest fixtures for optional Rust stream tests.

Use these fixtures to access optional backends without repeating availability
checks in each test module.

Example
-------
def test_pumps_bytes(rust_streams):
    rust_streams.rust_pump_stream(reader_fd, writer_fd)
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _rust_backend
from cuprum._backend import _check_rust_available, get_stream_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture(name="rust_streams")
def fixture_rust_streams() -> ModuleType:
    """Provide the Rust streams module when available.

    Parameters
    ----------
    None

    Returns
    -------
    ModuleType
        The imported ``cuprum._streams_rs`` module.

    Raises
    ------
    pytest.Skip
        If the Rust extension is not installed.
    """
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs


@pytest.fixture(
    params=[
        pytest.param("python", id="python-backend"),
        pytest.param(
            "rust",
            id="rust-backend",
            marks=pytest.mark.skipif(
                not _rust_backend.is_available(),
                reason="Rust extension is not installed",
            ),
        ),
    ],
)
def stream_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Parametrize tests to run against both stream backends.

    Sets the ``CUPRUM_STREAM_BACKEND`` environment variable so the
    dispatcher routes inter-stage pumping to the requested backend.
    The Rust variant is automatically skipped when the extension is
    unavailable.

    Parameters
    ----------
    request : pytest.FixtureRequest
        Pytest request providing the parametrized backend value.
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatch for environment variable isolation.

    Returns
    -------
    str
        The backend identifier (``"python"`` or ``"rust"``).
    """
    backend: str = request.param
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend)
    return backend


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> None:
    """Clear the cached backend dispatcher results between tests.

    Prevents cross-test pollution from ``lru_cache`` on
    ``_check_rust_available`` and ``get_stream_backend``.
    """
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
