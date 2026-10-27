"""Both source distributions must retain the optional native workspace."""

from __future__ import annotations

import tarfile
import typing as typ
from pathlib import Path

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from tests.helpers.distribution_artefacts import DistributionArtefactFactory


@pytest.mark.timeout(240, func_only=True)
@pytest.mark.parametrize("backend", ["uv", "maturin"])
def test_source_distribution_retains_native_workspace(
    backend: typ.Literal["uv", "maturin"],
    distribution_artefact_factory: DistributionArtefactFactory,
) -> None:
    """Every crate source and lockfile survives packaging without build output."""
    with tarfile.open(distribution_artefact_factory(backend, "sdist")) as archive:
        members = {Path(name).parts[1:] for name in archive.getnames()}
    workspace = repo_root() / "rust"
    expected: set[tuple[str, ...]] = {
        ("rust", name)
        for name in (
            "Cargo.toml",
            "Cargo.lock",
            "clippy.toml",
            "rust-toolchain.toml",
            "dylint.toml",
        )
    }
    for crate in ("cuprum-rust", "cuprum-streams", "cuprum-native-io"):
        expected.add(("rust", crate, "Cargo.toml"))
        for source in (workspace / crate / "src").rglob("*"):
            if source.is_file():
                expected.add(("rust", *source.relative_to(workspace).parts))
    assert expected <= members, f"source archive omitted: {sorted(expected - members)}"
    assert not any("target" in member for member in members), (
        "source archive must not contain Rust build output"
    )
