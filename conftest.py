"""Shared pytest fixtures for workflow contract and optional Rust stream tests.

The ``workflow_data`` fixture parses the checked-in
``.github/workflows/ci.yml`` model, while ``filter_path_patterns`` exposes its
benchmark filter paths for workflow contract and behaviour tests.

Use these fixtures to access optional backends without repeating availability
checks in each test module.

Importing this module must not import ``cuprum``: the mutation sandbox runs
its probe with the mutated package armed to raise on the first trampolined
call, and a conftest import failure is reported as a pytest usage error that
mutmut refuses. Imports of the package under test therefore stay inside the
fixtures that need them, and the plugin modules are declared below rather than
imported.

Example
-------
def test_pumps_bytes(rust_streams):
    rust_streams.rust_pump_stream(reader_fd, writer_fd)
"""

from __future__ import annotations

import os
import typing as typ

import pytest
from hypothesis import HealthCheck, settings

from tests.helpers.extension_requirement import (
    REQUIRE_EXTENSION_ENV,
    missing_extension_message,
)

# Property tests assert behaviour, while pytest-timeout retains the suite-wide
# bound for tests that hang. Hypothesis's per-example runtime is host-sensitive.
settings.register_profile("cuprum-test-suite", deadline=None)
settings.load_profile("cuprum-test-suite")

# mutmut executes the suite more than once in a single process: statistics
# collection, then the clean baseline, then a rerun per mutant in a forked
# worker. A class-based Hypothesis test is therefore observed from several
# executors even though nothing is wrong with the test, and the suite-profile
# registration above is already in place, so this profile inherits it and
# relaxes only that harness-specific check. ``[tool.mutmut]`` selects the
# profile through pytest_add_cli_args; ordinary runs keep the check enabled.
settings.register_profile(
    "mutmut-sandbox",
    suppress_health_check=[HealthCheck.differing_executors],
)

if typ.TYPE_CHECKING:
    from types import ModuleType

    from tests.helpers.catalogue import PythonCatalogue
    from tests.helpers.workflow import Workflow


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
if typ.TYPE_CHECKING:
    from types import ModuleType


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
if typ.TYPE_CHECKING:
    from types import ModuleType
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
if typ.TYPE_CHECKING:
    from types import ModuleType


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
if typ.TYPE_CHECKING:
    from types import ModuleType
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
if typ.TYPE_CHECKING:
    from types import ModuleType
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
if typ.TYPE_CHECKING:
    from types import ModuleType
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
if typ.TYPE_CHECKING:
    from types import ModuleType


# Pytest 9 only accepts plugin declarations in the repository-root conftest.
#
# These modules are imported as pytest plugins rather than into this module so
# that importing the root conftest never imports the package under test. The
# mutation sandbox forces the mutated ``cuprum`` to raise from its first
# trampolined call, and mutmut treats an exception escaping the run as the
# probe's success; a conftest import failure, by contrast, is a pytest usage
# error that mutmut refuses and aborts on.
pytest_plugins = (
    "tests.helpers.prepared_cargo_run",
    "cuprum.unittests._safe_cmd_lines_support",
    "cuprum.unittests._safe_cmd_lines_authorization_support",
)


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
    from cuprum import _rust_backend

    message = missing_extension_message(
        required=bool(os.environ.get(REQUIRE_EXTENSION_ENV)),
        available=_rust_backend.is_available(),
    )
    if message is not None:
        raise pytest.UsageError(message)


def _rust_extension_is_available() -> bool:
    """Report whether the native Rust extension can be imported.

    The import lives here rather than at module scope so importing this
    conftest never imports ``cuprum``; see ``pytest_plugins`` above.

    Returns
    -------
    bool
        ``True`` when ``cuprum._rust_backend.is_available`` reports the native
        extension as usable.
    """
    from cuprum import _rust_backend

    return _rust_backend.is_available()


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
    if not _rust_extension_is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs


@pytest.fixture
def python_catalogue_env() -> PythonCatalogue:
    """Provide the interpreter catalogue, its allowlisted program, and a builder.

    Function-scoped so each test gets a catalogue built from the running
    interpreter rather than sharing mutable catalogue state across tests.

    Returns
    -------
    PythonCatalogue
        The catalogue, the allowlisted ``sys.executable`` program, and a
        builder bound to both.
    """
    from tests.helpers.catalogue import build_python_catalogue_env

    return build_python_catalogue_env()


@pytest.fixture(
    params=[
        pytest.param("python", id="python-backend"),
        pytest.param("rust", id="rust-backend"),
    ],
)
def stream_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Parametrize tests to run against both stream backends.

    Sets the ``CUPRUM_STREAM_BACKEND`` environment variable so the
    dispatcher routes inter-stage pumping to the requested backend.
    The Rust variant is skipped during setup when the extension is
    unavailable, because the availability probe imports ``cuprum`` and so
    cannot run while this module is being imported.

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
    if backend == "rust" and not _rust_extension_is_available():
        pytest.skip("Rust extension is not installed")
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend)
    return backend


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> None:
    """Clear the cached backend dispatcher results between tests.

    Prevents cross-test pollution from ``lru_cache`` on
    ``_check_rust_available`` and ``get_stream_backend``.
    """
    from cuprum._backend import _check_rust_available, get_stream_backend

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
    from tests.helpers.workflow import parse_workflow, read_workflow_source

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
    from tests.helpers.workflow import filter_paths

    return filter_paths(workflow_data)
