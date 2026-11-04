"""Build and cache distribution artefacts for one pytest session.

The factory is split into a read-only query and an explicit command. Asking
where an artefact lives, :meth:`DistributionArtefactFactory.artefact_path`,
touches no directory, no clock, and no child process; asking for it to exist,
:meth:`DistributionArtefactFactory.build_artefact`, owns every side effect.
Both share one cache, so a second consumer observes the first build's success
or its retained diagnostic instead of rebuilding.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import time
import typing as typ
from pathlib import Path

import pytest

from cuprum import Program
from scripts.cargo_state import cargo_environment
from tests.helpers.distribution_build import (
    ArtefactFileSystem,
    Backend,
    BuildFailure,
    BuildOutcome,
    BuildRequest,
    Format,
    PackagingFrontend,
    PathFileSystem,
    ScopedCommandRunner,
    SubprocessFrontend,
    configured_build_timeouts,
    distribution_test_timeout_seconds,
    failure_from_exception,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.prepared_cargo_run import PreparedCargoRun

__all__ = (
    "ArtefactNotBuiltError",
    "DistributionArtefactFactory",
    "DistributionArtefactSettings",
    "configured_build_timeouts",
    "distribution_test_timeout_seconds",
)

type _BuildKey = tuple[Backend, Format]


class ArtefactNotBuiltError(LookupError):
    """Report a read-only lookup of an artefact this session has not built."""


@dc.dataclass(frozen=True, slots=True)
class DistributionArtefactSettings:
    """Carry the paths and injected collaborators for one artefact session."""

    repository_root: Path
    artefacts_root: Path
    target_root: Path
    environment: cabc.Mapping[str, str]
    timings_path: Path
    frontend: PackagingFrontend
    filesystem: ArtefactFileSystem
    clock: cabc.Callable[[], float] = time.monotonic


class DistributionArtefactFactory:
    """Build each backend and format once, retaining success or failure."""

    def __init__(self, settings: DistributionArtefactSettings) -> None:
        """Configure this invocation-local cache and its injected collaborators."""
        self._settings = settings
        self._environment = dict(settings.environment)
        self._outcomes: dict[_BuildKey, BuildOutcome] = {}

    def artefact_path(self, backend: Backend, archive_format: Format) -> Path:
        """Return a built archive without creating, running, or recording anything.

        Returns
        -------
        Path
            The archive this invocation already built for the key.

        Raises
        ------
        ArtefactNotBuiltError
            If nothing has built this key in the current pytest invocation.

        Notes
        -----
        A cached build failure surfaces as ``RuntimeError`` carrying the
        diagnostic the build first reported.
        """
        outcome = self._outcomes.get((backend, archive_format))
        if outcome is None:
            msg = (
                f"{backend} {archive_format} has not been built in this pytest "
                "invocation; call build_artefact() first"
            )
            raise ArtefactNotBuiltError(msg)
        return require_archive(outcome)

    def build_artefact(self, backend: Backend, archive_format: Format) -> Path:
        """Build one archive when absent and return its cached path.

        Returns
        -------
        Path
            The archive for this key, built now or reused from the cache.

        Notes
        -----
        A failed build raises ``RuntimeError`` carrying the original
        diagnostic. The failure is cached, so a later consumer sees the same
        explanation instead of a fresh attempt.
        """
        key = (backend, archive_format)
        outcome = self._outcomes.get(key)
        if outcome is None:
            outcome = self._build(key)
        return require_archive(outcome)

    def _build(self, key: _BuildKey) -> BuildOutcome:
        """Create the output directories, run the frontend, and cache its outcome."""
        backend, archive_format = key
        destination = self._settings.artefacts_root / backend / archive_format
        self._settings.filesystem.prepare(destination)
        started = self._settings.clock()
        try:
            outcome = self._settings.frontend(
                BuildRequest(
                    backend=backend,
                    archive_format=archive_format,
                    repository_root=self._settings.repository_root,
                    destination=destination,
                    environment=self._build_environment(backend, archive_format),
                )
            )
        except Exception as error:  # ruff: ignore[blind-except] - memoize ordinary build failures and their diagnostics
            outcome = failure_from_exception(backend, archive_format, error)
        self._record_build_duration(
            backend, archive_format, self._settings.clock() - started
        )
        self._outcomes[key] = outcome
        return outcome

    def _build_environment(
        self,
        backend: Backend,
        archive_format: Format,
    ) -> dict[str, str]:
        """Create the target directory and select it for one packaging route."""
        environment = self._environment.copy()
        target = self._settings.target_root / f"distribution-{backend}-{archive_format}"
        self._settings.filesystem.prepare(target)
        environment["CARGO_TARGET_DIR"] = str(target)
        return environment

    def _record_build_duration(
        self,
        backend: Backend,
        archive_format: Format,
        seconds: float,
    ) -> None:
        """Append one non-secret build duration to this pytest invocation."""
        record = {
            "phase": "packaging_build",
            "backend": backend,
            "format": archive_format,
            "seconds": seconds,
        }
        self._settings.filesystem.append_line(
            self._settings.timings_path, json.dumps(record)
        )


def require_archive(outcome: BuildOutcome) -> Path:
    """Return a successful archive or raise its original build diagnostic."""
    match outcome:
        case BuildFailure(diagnostic=diagnostic):
            raise RuntimeError(diagnostic)
        case Path() as archive:
            return archive


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
    environment = cargo_environment(prepared_cargo_run.cargo_home)
    environment["CARGO_NET_OFFLINE"] = "true"
    filesystem = PathFileSystem()
    return DistributionArtefactFactory(
        DistributionArtefactSettings(
            repository_root=repo_root(),
            artefacts_root=tmp_path_factory.mktemp("distribution-artefacts"),
            target_root=prepared_cargo_run.target_root,
            environment=environment,
            timings_path=prepared_cargo_run.timings_path,
            frontend=SubprocessFrontend(
                runner=ScopedCommandRunner(Program("uv")),
                build_timeouts=configured_build_timeouts(os.environ),
                filesystem=filesystem,
            ),
            filesystem=filesystem,
        )
    )
