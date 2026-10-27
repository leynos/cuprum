"""Shared fixture inputs for Cargo snapshot and run-home contracts."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

SEEDED_CARGO_FILES = {
    "registry/cache/crates-io/registry-1.0.0.crate": "registry bytes",
    "registry/cache/crates-io/sparse-2.0.0.crate": "sparse registry bytes",
    "registry/src/registry-1.0.0/Cargo.toml": "stale extracted source",
    "git/db/source.git/config": "git bytes",
    "git/checkouts/stale-source/checkout/Cargo.toml": "stale git checkout",
    "config.toml": "[net]\noffline = false\n",
    "credentials.toml": "token = 'private-token'\n",
}


@dc.dataclass(frozen=True, slots=True)
class SnapshotInputs:
    """Hold inputs used to verify one staged Cargo snapshot preparation."""

    repository: Path
    source_home: Path
    snapshot_root: Path
    original_lock: bytes
    caller_environment: dict[str, str]


def create_repository(root: Path) -> Path:
    """Create a minimal locked Rust workspace and its toolchain input."""
    rust = root / "rust"
    rust.mkdir(parents=True)
    (rust / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (rust / "Cargo.toml").write_text("[workspace]\nmembers = []\n", encoding="utf-8")
    (rust / "rust-toolchain.toml").write_text(
        "[toolchain]\nchannel = '1.85.0'\n", encoding="utf-8"
    )
    return root


def create_snapshot_inputs(tmp_path: Path) -> SnapshotInputs:
    """Create a locked workspace and seeded caller cache for snapshot tests."""
    repository = create_repository(tmp_path / "repo")
    lockfile = repository / "rust/Cargo.lock"
    lockfile.write_text(
        """version = 4

[[package]]
name = "registry"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "sparse"
version = "2.0.0"
source = "sparse+https://index.crates.io/"

[[package]]
name = "git-source"
version = "3.0.0"
source = "git+https://example.test/git-source#0123456789abcdef"
""",
        encoding="utf-8",
    )
    source_home = tmp_path / "caller-cargo"
    for relative, contents in SEEDED_CARGO_FILES.items():
        path = source_home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    stale_archives = (
        "registry/cache/crates-io/stale-9.9.9.crate",
        "registry/cache/crates-io/git-source-3.0.0.crate",
    )
    for relative in stale_archives:
        path = source_home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unselected archive", encoding="utf-8")
    return SnapshotInputs(
        repository=repository,
        source_home=source_home,
        snapshot_root=tmp_path / "snapshots",
        original_lock=lockfile.read_bytes(),
        caller_environment={
            "CARGO_HOME": str(source_home),
            "RUSTUP_HOME": str(tmp_path / "rustup-home"),
            "CARGO_BUILD_JOBS": "7",
            "CARGO_NET_OFFLINE": "true",
        },
    )
