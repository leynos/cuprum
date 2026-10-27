"""Verify every Cargo route derives snapshots from one cache root."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

from scripts import cargo_state


def test_default_snapshot_root_uses_the_shared_cache_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every route derives its snapshot directory from XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert cargo_state.default_snapshot_root() == (
        tmp_path / "cuprum" / "cargo-snapshots-v3"
    ), "Cargo routes must share the same snapshot-root definition"
