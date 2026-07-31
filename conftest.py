"""Shared pytest fixtures for optional Rust stream tests.

Use these fixtures to access optional backends without repeating availability
checks in each test module.

Example
-------
def test_pumps_bytes(rust_streams):
    rust_streams.rust_pump_stream(reader_fd, writer_fd)
"""

from __future__ import annotations

import os
import typing as typ

import pytest

from cuprum import _rust_backend
from cuprum._backend import _check_rust_available, get_stream_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


_REQUIRE_EXTENSION_ENV = "CUPRUM_REQUIRE_RUST_EXTENSION"


def pytest_configure(config: pytest.Config) -> None:
    """Fail the run when the extension is required but absent.

    The extension-gated modules skip when `cuprum._rust_backend_native` cannot
    be imported, which is right locally — most contributors do not build the
    native extension for every change. In CI it is the wrong default: a job
    that never builds the extension reports a green run indistinguishable from
    one that exercised the whole Python/Rust boundary.

    Setting `CUPRUM_REQUIRE_RUST_EXTENSION=1` makes that silence fatal. One
    session-level check covers every gated module regardless of how each one
    gates — fixture, module-level guard, or availability probe — so a new
    module cannot opt out of the requirement by skipping differently.
    """
    del config
    if not os.environ.get(_REQUIRE_EXTENSION_ENV):
        return
    if _rust_backend.is_available():
        return
    msg = (
        f"{_REQUIRE_EXTENSION_ENV} is set, but cuprum._rust_backend_native "
        "could not be imported, so every extension-gated test would skip "
        "silently. Build it with `make develop` before running the suite. "
        "Unset the variable to allow skipping."
    )
    raise pytest.UsageError(msg)


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
