"""Prepare one pytest invocation's private writable Cargo state."""

from __future__ import annotations

import hashlib
import shutil
import time
import typing as typ

import pytest

from scripts.cargo_state import (
    cargo_environment,
    create_run_cargo_home,
    default_snapshot_root,
    prepare_cargo_snapshot,
    record_phase,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class PreparedCargoRun:
    """Hold one run-local home, target root, and phase timing stream."""

    def __init__(
        self,
        repository_root: Path,
        run_root: Path,
        cargo_home: Path,
        preparation_seconds: float,
    ) -> None:
        """Store the prepared paths and initialize dependency timing."""
        self.repository_root = repository_root
        self.run_root = run_root
        self.cargo_home = cargo_home
        run_key = hashlib.sha256(str(run_root).encode()).hexdigest()[:12]
        self.target_root = repository_root / "rust/target" / f"pytest-{run_key}"
        self.target_root.mkdir(parents=True, exist_ok=True)
        self.timings_path = run_root / "timings.jsonl"
        self.record_phase("dependency_preparation", preparation_seconds)

    def target_path(self, configuration: str) -> Path:
        """Return a build configuration's target path without creating it."""
        return self.target_root / configuration

    def prepare_target_directory(self, configuration: str) -> Path:
        """Create and return a build configuration's invocation-local target."""
        target = self.target_path(configuration)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def command_environment(
        self,
        *,
        target_configuration: str,
        offline: bool = True,
    ) -> dict[str, str]:
        """Copy caller settings and select this run's home and target directory."""
        environment = cargo_environment(self.cargo_home)
        environment["CARGO_TARGET_DIR"] = str(
            self.prepare_target_directory(target_configuration)
        )
        if offline:
            environment["CARGO_NET_OFFLINE"] = "true"
        return environment

    def record_phase(self, phase: str, seconds: float) -> None:
        """Append one monotonic duration without recording environment values."""
        record_phase(self.timings_path, phase, seconds)


@pytest.fixture(scope="session")
def prepared_cargo_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> cabc.Iterator[PreparedCargoRun]:
    """Prepare a copied Cargo home before any timed test build begins."""
    repository_root = repo_root()
    run_root = tmp_path_factory.mktemp("cargo-run")
    preparation_started = time.monotonic()
    snapshot = prepare_cargo_snapshot(repository_root, default_snapshot_root())
    cargo_home = create_run_cargo_home(
        snapshot.path,
        run_root,
        source_cargo_home=snapshot.source_cargo_home,
    )
    preparation_seconds = time.monotonic() - preparation_started
    run = PreparedCargoRun(
        repository_root,
        run_root,
        cargo_home,
        preparation_seconds,
    )
    try:
        yield run
    finally:
        shutil.rmtree(run.target_root, ignore_errors=True)
        shutil.rmtree(run.cargo_home, ignore_errors=True)
