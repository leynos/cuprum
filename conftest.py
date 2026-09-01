"""Shared pytest fixtures for workflow contract and optional Rust stream tests.

The ``workflow_data`` fixture parses the checked-in
``.github/workflows/ci.yml`` model, while ``filter_path_patterns`` exposes its
benchmark filter paths for workflow contract and behaviour tests.

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
from tests.helpers.extension_requirement import (
    REQUIRE_EXTENSION_ENV,
    missing_extension_message,
)
from tests.helpers.workflow import (
    Workflow,
    filter_paths,
    parse_workflow,
    read_workflow_source,
)

if typ.TYPE_CHECKING:
    from types import ModuleType


def pytest_configure(config: pytest.Config) -> None:
    """Fail the run when the extension is required but absent.

    Parameters
    ----------
    config : pytest.Config
        The session configuration. Unused; the decision depends only on the
        environment and on whether the extension reports availability.

    Raises
    ------
    pytest.UsageError
        If ``CUPRUM_REQUIRE_RUST_EXTENSION`` is set to a non-empty value and
        the native extension is unavailable.
    ImportError
        If ``_rust_backend.is_available()`` fails to import the native module
        for a reason other than the module being absent.

    Notes
    -----
    The extension-gated modules skip when the native extension is unavailable,
    which is right locally — most contributors do not build it for every
    change. In CI it is the wrong default: a job that never builds the
    extension reports a green run indistinguishable from one that exercised
    the whole Python/Rust boundary.

    Setting the variable makes that silence fatal. One session-level check
    covers every gated module regardless of how each one gates — fixture,
    module-level guard, or availability probe — so a new module cannot opt out
    of the requirement by skipping differently.
    """  # ruff: ignore[docstring-extraneous-exception] - ImportError propagates from is_available()
    del config
    message = missing_extension_message(
        required=bool(os.environ.get(REQUIRE_EXTENSION_ENV)),
        available=_rust_backend.is_available(),
    )
    if message is not None:
        raise pytest.UsageError(message)


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

@pytest.fixture(scope="session")
def workflow_data() -> Workflow:
    """Provide the checked-in Continuous Integration workflow model.

    Returns
    -------
    Workflow
        Parsed ``ci.yml`` model shared by workflow contract tests.
    """
    return parse_workflow(read_workflow_source())

@pytest.fixture(scope="session")
def filter_path_patterns(workflow_data: Workflow) -> frozenset[str]:
    """Provide the performance-relevant paths declared in ``ci.yml``.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed Continuous Integration workflow model.

    Returns
    -------
    frozenset[str]
        Performance-relevant filter patterns in declaration order.
    """
    return filter_paths(workflow_data)
