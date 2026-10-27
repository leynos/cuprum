"""Shared fixture inputs for Cargo snapshot and run-home contracts."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path


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
