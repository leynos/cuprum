"""Prove the Linux doctest route fails when a doctest emits a warning."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test executes a fixed pinned Cargo command.
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from pathlib import Path

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


def _run_doctests(
    crate: Path, cargo: str, rustdoc_flags: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the pinned nightly with the standard backend and stated Rustdoc flags."""
    environment: dict[str, str] = dict(os.environ)
    environment.update({
        "CARGO_TARGET_DIR": str(crate / "target"),
        "RUSTFLAGS": "-D warnings",
        "RUSTUP_TOOLCHAIN": DEV_FAST_TOOLCHAIN,
    })
    if rustdoc_flags is not None:
        environment["RUSTDOCFLAGS"] = rustdoc_flags
    else:
        environment.pop("RUSTDOCFLAGS", None)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv and pinned toolchain.
        [
            cargo,
            "--color",
            "never",
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


def _diagnostics(completed: subprocess.CompletedProcess[str]) -> str:
    """Return every Cargo diagnostic stream for setup and failure assertions."""
    return completed.stdout + completed.stderr


@pytest.mark.skipif(
    sys.platform != "linux", reason="the production doctest warning route is Linux-only"
)
def test_pinned_doctest_route_rejects_a_warning(tmp_path: Path) -> None:
    """Only the corrected nightly Rustdoc route rejects a doctest warning."""
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
    former_route = _run_doctests(crate, cargo, rustdoc_flags=None)
    former_diagnostics = _diagnostics(former_route)
    assert former_route.returncode == 0, (
        "the former RUSTFLAGS-only route must pass the warning-bearing doctest:\n"
        f"{former_diagnostics}"
    )
    assert "test result: ok" in former_diagnostics, (
        "the former route must execute the doctest rather than only build setup"
    )

    corrected_route = _run_doctests(crate, cargo, DOCTEST_RUSTDOC_FLAGS)
    corrected_diagnostics = _diagnostics(corrected_route)
    assert corrected_route.returncode != 0, (
        "the corrected Rustdoc route must reject a doctest warning:\n"
        f"{corrected_diagnostics}"
    )
    assert "\x1b" not in corrected_diagnostics, (
        "the synthetic Cargo route must disable colour for deterministic diagnostics"
    )
    assert "error: use of deprecated function" in corrected_diagnostics, (
        "the corrected route must fail on the doctest warning rather than setup"
    )
    assert "-D deprecated" in corrected_diagnostics, (
        "rustdoc must deny the displayed doctest warning"
    )
