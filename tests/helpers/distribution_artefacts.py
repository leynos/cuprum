"""Build and cache distribution artefacts for one pytest session."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import time
import typing as typ
from pathlib import Path

import pytest

from cuprum import (
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    scoped,
    sh,
)
from scripts.cargo_state import cargo_environment
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.prepared_cargo_run import PreparedCargoRun

type _Backend = typ.Literal["uv", "maturin"]
type _Format = typ.Literal["wheel", "sdist"]
type _BuildKey = tuple[_Backend, _Format]

_ARCHIVE_SUFFIXES: dict[_Format, str] = {
    "wheel": "*.whl",
    "sdist": "*.tar.gz",
}
_MATURIN_SUBCOMMANDS: dict[_Format, str] = {
    "wheel": "build",
    "sdist": "sdist",
}
_BUILD_TIMEOUT_ENVIRONMENT_VARIABLE = "CUPRUM_PACKAGING_BUILD_TIMEOUT"
# Maturin compiles the native extension from a cold target. A fresh wheel build
# measured 163 s on a loaded developer host on 2026-10-30, but that is workload
# time, not a safe budget: one gate run waited behind 67 and then 123
# compiler-admission requests and overran 600 s. No workload lease exists to
# exclude that queue, so the native frontend keeps the 3600 s end-to-end
# watchdog inherited from its pre-factory contract. The uv frontend only
# packages Python and stays at 180 s. `CUPRUM_PACKAGING_BUILD_TIMEOUT` can raise
# either bound but never lowers a backend below its default.
_DEFAULT_BUILD_TIMEOUT_SECONDS: dict[_Backend, int] = {
    "uv": 180,
    "maturin": 3600,
}
_BUILD_TIMEOUT_MARGIN_SECONDS = 60


def _configured_build_timeout_seconds(value: str | None, *, backend: _Backend) -> int:
    """Return the backend default or a larger positive override.

    The override exists to add queue headroom, so it can only raise a bound.
    Applying a shared value such as ``600`` to both backends would otherwise
    lower the native watchdog that has already expired under compiler-admission
    queueing.

    Returns
    -------
    int
        The effective end-to-end watchdog in seconds.

    Raises
    ------
    ValueError
        If the override is not a positive integer.
    """
    if value is None:
        return _DEFAULT_BUILD_TIMEOUT_SECONDS[backend]
    try:
        timeout_seconds = int(value)
    except ValueError as error:
        msg = f"{_BUILD_TIMEOUT_ENVIRONMENT_VARIABLE} must be a positive integer"
        raise ValueError(msg) from error
    if timeout_seconds <= 0:
        msg = f"{_BUILD_TIMEOUT_ENVIRONMENT_VARIABLE} must be a positive integer"
        raise ValueError(msg)
    return max(_DEFAULT_BUILD_TIMEOUT_SECONDS[backend], timeout_seconds)


DISTRIBUTION_BUILD_TIMEOUT_SECONDS: dict[_Backend, int] = {
    backend: _configured_build_timeout_seconds(
        os.environ.get(_BUILD_TIMEOUT_ENVIRONMENT_VARIABLE), backend=backend
    )
    for backend in ("uv", "maturin")
}
DISTRIBUTION_TEST_TIMEOUT_SECONDS = (
    max(DISTRIBUTION_BUILD_TIMEOUT_SECONDS.values()) + _BUILD_TIMEOUT_MARGIN_SECONDS
)


@dc.dataclass(frozen=True, slots=True)
class _BuildFailure:
    """Preserve a failed build's original diagnostics for later consumers."""

    diagnostic: str


type _BuildOutcome = Path | _BuildFailure


@dc.dataclass(frozen=True, slots=True)
class _FactorySettings:
    """Carry paths and caller environment for one pytest artefact session."""

    repository_root: Path
    artefacts_root: Path
    target_root: Path
    command_environment: cabc.Mapping[str, str]
    timings_path: Path


@dc.dataclass(frozen=True, slots=True)
class _BuildRequest:
    """Describe one frontend invocation and its isolated output locations."""

    backend: _Backend
    archive_format: _Format
    repository_root: Path
    destination: Path
    environment: cabc.Mapping[str, str]


class DistributionArtefactFactory:
    """Build each backend and format once, retaining success or failure."""

    def __init__(self, settings: _FactorySettings) -> None:
        """Configure this invocation-local cache and its scratch directory."""
        self._settings = settings
        self._command_environment = dict(settings.command_environment)
        self._outcomes: dict[_BuildKey, _BuildOutcome] = {}

    def __call__(self, backend: _Backend, archive_format: _Format) -> Path:
        """Return one cached distribution, building it lazily on first use."""
        key = (backend, archive_format)
        cached = self._outcomes.get(key)
        if cached is not None:
            return _require_archive(cached)

        destination = self._settings.artefacts_root / backend / archive_format
        destination.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            outcome = _build_distribution(
                _BuildRequest(
                    backend=backend,
                    archive_format=archive_format,
                    repository_root=self._settings.repository_root,
                    destination=destination,
                    environment=self._build_environment(backend, archive_format),
                )
            )
        except Exception as error:  # ruff: ignore[blind-except] - memoize ordinary build failures and their diagnostics
            outcome = _exception_failure(backend, archive_format, error)
        self._record_build_duration(backend, archive_format, time.monotonic() - started)
        self._outcomes[key] = outcome
        return _require_archive(outcome)

    def _build_environment(
        self,
        backend: _Backend,
        archive_format: _Format,
    ) -> dict[str, str]:
        """Select the target directory for one compatible packaging route."""
        environment = self._command_environment.copy()
        target = self._settings.target_root / f"distribution-{backend}-{archive_format}"
        target.mkdir(parents=True, exist_ok=True)
        environment["CARGO_TARGET_DIR"] = str(target)
        return environment

    def _record_build_duration(
        self,
        backend: _Backend,
        archive_format: _Format,
        seconds: float,
    ) -> None:
        """Append one non-secret build duration to this pytest invocation."""
        record = {
            "phase": "packaging_build",
            "backend": backend,
            "format": archive_format,
            "seconds": seconds,
        }
        with self._settings.timings_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record) + "\n")


def _build_distribution(request: _BuildRequest) -> _BuildOutcome:
    """Run one frontend and require exactly one archive of the requested kind."""
    program = Program("uv")
    catalogue = ProgramCatalogue.from_programs(
        program,
        name="distribution-artefact-tests",
        documentation_locations=("docs/developers-guide.md",),
    )
    command = sh.make(program, catalogue=catalogue)
    arguments = _distribution_arguments(
        request.backend, request.archive_format, request.destination
    )
    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        result = command(*arguments).run_sync(
            context=sh.ExecutionContext(
                cwd=request.repository_root,
                env=dict(request.environment),
                timeout=DISTRIBUTION_BUILD_TIMEOUT_SECONDS[request.backend],
                process_group=ProcessGroupPolicy.OWN_GROUP,
            )
        )

    if result.exit_code != 0:
        diagnostic = (
            f"{request.backend} {request.archive_format} build failed "
            f"with {result.exit_code}; stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        return _BuildFailure(diagnostic)

    archives = sorted(
        request.destination.glob(_ARCHIVE_SUFFIXES[request.archive_format])
    )
    if len(archives) != 1:
        diagnostic = (
            f"{request.backend} {request.archive_format} build must produce exactly "
            f"one {_ARCHIVE_SUFFIXES[request.archive_format]} archive, "
            f"found {[archive.name for archive in archives]}"
        )
        return _BuildFailure(diagnostic)
    return archives[0]


def _exception_failure(
    backend: _Backend,
    archive_format: _Format,
    error: Exception,
) -> _BuildFailure:
    """Retain timeout streams as well as the exception's diagnostic."""
    diagnostic = (
        f"{backend} {archive_format} build raised {type(error).__name__}: {error}"
    )
    stdout = getattr(error, "stdout", None)
    stderr = getattr(error, "stderr", None)
    if stdout:
        diagnostic += f"\nstdout: {_decode_diagnostic(stdout)}"
    if stderr:
        diagnostic += f"\nstderr: {_decode_diagnostic(stderr)}"
    return _BuildFailure(diagnostic)


def _decode_diagnostic(value: str | bytes) -> str:
    """Render one captured stream without assuming its subprocess encoding."""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _distribution_arguments(
    backend: _Backend,
    archive_format: _Format,
    destination: Path,
) -> tuple[str, ...]:
    """Return the pinned frontend's command for one archive format."""
    if backend == "uv":
        return ("build", f"--{archive_format}", "--out-dir", str(destination))
    return (
        "run",
        "maturin",
        _MATURIN_SUBCOMMANDS[archive_format],
        "--manifest-path",
        "rust/cuprum-rust/Cargo.toml",
        "--out",
        str(destination),
    )


@pytest.fixture(scope="session")
def distribution_artefact_factory(
    tmp_path_factory: pytest.TempPathFactory,
    prepared_cargo_run: PreparedCargoRun,
) -> DistributionArtefactFactory:
    """Create the lazy cache for this invocation and xdist worker.

    A session fixture lasts for one pytest process. Separate Makefile pytest
    invocations and separate xdist workers each receive their own factory.

    Returns
    -------
    DistributionArtefactFactory
        The lazy session cache for source archives and wheels.
    """
    repository_root = repo_root()
    artefacts_root = tmp_path_factory.mktemp("distribution-artefacts")
    environment = cargo_environment(prepared_cargo_run.cargo_home)
    environment["CARGO_NET_OFFLINE"] = "true"
    return DistributionArtefactFactory(
        _FactorySettings(
            repository_root=repository_root,
            artefacts_root=artefacts_root,
            target_root=prepared_cargo_run.target_root,
            command_environment=environment,
            timings_path=prepared_cargo_run.timings_path,
        )
    )


def _require_archive(outcome: _BuildOutcome) -> Path:
    """Return a successful archive or raise its original build diagnostic."""
    match outcome:
        case _BuildFailure(diagnostic=diagnostic):
            raise RuntimeError(diagnostic)
        case Path() as archive:
            return archive
