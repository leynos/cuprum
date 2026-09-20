"""Prove the Linux doctest route fails when a doctest emits a warning."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test executes a fixed pinned Cargo command.
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV_FAST_TOOLCHAIN = "nightly-2026-08-23"
DOCTEST_RUSTDOC_FLAGS = (
    "--cfg docsrs -D warnings -Zunstable-options --display-doctest-warnings "
    "--doctest-build-arg=-D --doctest-build-arg=warnings"
)
WARNING_DOCTEST = """//! Warning-bearing doctest fixture.

/// ```
/// #[deprecated(note = "doctest warning contract")]
/// fn legacy() {}
/// legacy();
/// ```
pub struct WarningFixture;
"""


@pytest.mark.skipif(sys.platform != "linux", reason="dev-fast doctests are Linux-only")
def test_pinned_doctest_route_rejects_a_warning(tmp_path: Path) -> None:
    """The nightly rustdoc path exposes and denies a doctest-body warning."""
    crate = tmp_path / "warning-doctest"
    source = crate / "src/lib.rs"
    source.parent.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "warning-doctest"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    source.write_text(WARNING_DOCTEST, encoding="utf-8")
    cargo = shutil.which("cargo")
    assert cargo is not None, "the doctest contract requires Cargo on PATH"
    environment = {
        **os.environ,
        "CARGO_TARGET_DIR": str(ROOT / "rust/target/doctest-warning-contract"),
        "RUSTDOCFLAGS": DOCTEST_RUSTDOC_FLAGS,
        "RUSTFLAGS": "-D warnings -Clink-arg=-fuse-ld=mold",
        "RUSTUP_TOOLCHAIN": DEV_FAST_TOOLCHAIN,
    }
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv and checked-in fragment.
        [
            cargo,
            "--config",
            str(ROOT / "tools/dev-fast/config.toml"),
            "test",
            "--doc",
            "--manifest-path",
            str(crate / "Cargo.toml"),
        ],
        capture_output=True,
        check=False,
        cwd=crate,
        env=environment,
        text=True,
    )

    assert completed.returncode != 0, "a doctest warning must fail the routed gate"
    assert "use of deprecated function" in completed.stdout, (
        "the failure must come from the doctest warning rather than setup"
    )
    assert "-D deprecated" in completed.stdout, (
        "rustdoc must deny the displayed doctest warning"
    )
