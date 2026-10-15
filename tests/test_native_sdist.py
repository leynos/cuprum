"""Both source distributions must retain the optional native workspace."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh
from tests.helpers.docs import repo_root

# Maturin invokes Cargo metadata, which can queue on the shared package cache.
_MATURIN_SDIST_TIMEOUT_SECONDS = 600


def _build_source_archive(backend: str, destination: Path) -> Path:
    """Build one real source archive using the repository's packaging frontend."""
    program = Program("uv")
    catalogue = ProgramCatalogue(
        projects=(
            ProjectSettings(
                name="sdist-test",
                programs=(program,),
                documentation_locations=("docs/rust-boundary-verification.md",),
                noise_rules=(),
            ),
        )
    )
    command = sh.make(program, catalogue=catalogue)
    arguments = (
        ("build", "--sdist", "--out-dir", str(destination))
        if backend == "uv"
        else (
            "run",
            "maturin",
            "sdist",
            "--manifest-path",
            "rust/cuprum-rust/Cargo.toml",
            "--out",
            str(destination),
        )
    )
    timeout = _MATURIN_SDIST_TIMEOUT_SECONDS if backend == "maturin" else 180
    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        result = command(*arguments).run_sync(
            context=sh.ExecutionContext(cwd=repo_root(), timeout=timeout)
        )
    assert result.exit_code == 0, f"source build failed: {result.stderr}"
    archives = list(destination.glob("*.tar.gz"))
    assert len(archives) == 1, "source build must produce exactly one archive"
    return archives[0]


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("uv", marks=pytest.mark.timeout(240)),
        pytest.param(
            "maturin",
            marks=pytest.mark.timeout(_MATURIN_SDIST_TIMEOUT_SECONDS + 60),
        ),
    ],
)
def test_source_distribution_retains_native_workspace(
    backend: str,
    tmp_path: Path,
) -> None:
    """Every crate source and lockfile survives packaging without build output."""
    with tarfile.open(_build_source_archive(backend, tmp_path)) as archive:
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
