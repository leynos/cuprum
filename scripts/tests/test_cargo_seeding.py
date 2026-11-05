"""Check that seeding copies the caller Cargo cache into a staging home."""

from __future__ import annotations

import typing as typ

from scripts import cargo_state
from scripts.cargo_cache_tree import seed_cargo_home, selected_registry_archives
from scripts.tests.cargo_state_support import create_snapshot_inputs

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_seeding_without_a_selection_copies_every_archive(tmp_path: Path) -> None:
    """A caller without a lockfile retains the whole archive cache."""
    inputs = create_snapshot_inputs(tmp_path)
    staging = tmp_path / "unfiltered"
    staging.mkdir()

    seed_cargo_home(
        inputs.source_home,
        staging,
        cargo_state.CargoStatePreparationError,
    )

    cache = staging / "registry/cache/crates-io"
    assert {archive.name for archive in cache.glob("*.crate")} == {
        "registry-1.0.0.crate",
        "sparse-2.0.0.crate",
        "stale-9.9.9.crate",
        "git-source-3.0.0.crate",
    }, "an absent selection must not silently drop cached archives"


def test_seeding_skips_unselected_archives_before_the_fetch(tmp_path: Path) -> None:
    """Staging never copies the caller's whole registry archive history."""
    inputs = create_snapshot_inputs(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    selected = selected_registry_archives(inputs.repository / "rust/Cargo.lock")

    seed_cargo_home(
        inputs.source_home,
        staging,
        cargo_state.CargoStatePreparationError,
        selected_archives=selected,
    )

    cache = staging / "registry/cache/crates-io"
    assert {archive.name for archive in cache.glob("*.crate")} == {
        "registry-1.0.0.crate",
        "sparse-2.0.0.crate",
    }, "seeding must copy only lockfile-selected registry archives"
    assert (staging / "git/db/source.git/config").is_file(), (
        "Git databases remain cache metadata because the lockfile exposes no "
        "stable entry name for them"
    )
    assert (
        inputs.source_home / "registry/cache/crates-io/stale-9.9.9.crate"
    ).is_file(), "seeding must not modify the caller's archive cache"
