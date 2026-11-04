"""Prepare one run-local Cargo home for a Make workload."""

from __future__ import annotations

import json
import time
from pathlib import Path

from cyclopts import App

from scripts.cargo_diagnostics import DIAGNOSTICS_FILE_NAME
from scripts.cargo_state import (
    PreparationContext,
    create_run_cargo_home,
    default_snapshot_root,
    prepare_cargo_snapshot,
    record_phase,
)

ROOT = Path(__file__).resolve().parents[1]
app = App()


@app.default
def main(
    run_root: Path,
    repository_root: Path = ROOT,
    snapshot_root: Path | None = None,
) -> None:
    """Prepare one snapshot and private run home, emitting non-secret metadata."""
    preparation_started = time.monotonic()
    cache_root = snapshot_root or default_snapshot_root()
    prepared = prepare_cargo_snapshot(
        repository_root,
        cache_root,
        context=PreparationContext(
            diagnostics_path=run_root / DIAGNOSTICS_FILE_NAME,
        ),
    )
    run_home = create_run_cargo_home(
        prepared.path,
        run_root,
        source_cargo_home=prepared.source_cargo_home,
    )
    preparation_seconds = time.monotonic() - preparation_started
    record_phase(
        run_root / "timings.jsonl",
        "dependency_preparation",
        preparation_seconds,
    )
    print(
        json.dumps(
            {
                "cargo_home": str(run_home),
                "snapshot_key": prepared.key,
                "cargo_version": prepared.cargo_version,
                "preparation_seconds": preparation_seconds,
                "snapshot_reused": prepared.reused,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    app()
