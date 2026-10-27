"""Test explicit target containment and byte-safe probe restoration."""

from __future__ import annotations

import typing as typ

import pytest

from scripts import check_boundary_contract as contract
from scripts.boundary_probe import probe_appended
from scripts.boundary_workspace import copy_workspace
from scripts.tests.boundary_harness_support import copy_boundary_repository

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("target_path", "expected_error"),
    [
        ("../../outside/target.rs", "escapes the Rust workspace"),
        ("/unused", "escapes the Rust workspace"),
        ("tools/target.rs", "symlinked parent directory"),
    ],
)
def test_main_rejects_explicit_targets_that_escape_the_copy(
    target_path: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit paths cannot reach original sources through escapes or directories."""
    root = copy_boundary_repository(tmp_path)
    crate = root / "rust/cuprum-streams"
    external = root / "outside/target.rs"
    external.parent.mkdir()
    external.write_text(
        "//! External target.\n#![forbid(unsafe_code)]\n",
        encoding="utf-8",
    )
    if target_path == "/unused":
        target_path = str(external)
    if target_path.startswith("tools/"):
        (crate / "tools").symlink_to("../../outside", target_is_directory=True)
    manifest = crate / "Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f'\n[[bin]]\nname = "escaped-target"\npath = "{target_path}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(contract, "ROOT", root)

    with pytest.raises(ValueError, match=expected_error):
        contract.main()


def test_copy_workspace_rejects_a_target_outside_the_source_tree(
    tmp_path: Path,
) -> None:
    """The copy helper refuses an external explicit target before copying."""
    source = tmp_path / "source"
    source.mkdir()
    outside_target = tmp_path / "outside.rs"
    outside_target.write_text("//! External target.\n", encoding="utf-8")
    destination = tmp_path / "copy"

    with pytest.raises(ValueError, match="escapes the Rust workspace"):
        copy_workspace(source, destination, (outside_target,))

    assert not destination.exists(), "an escaping target must fail before the tree copy"


def test_probe_appended_preserves_crlf_bytes_after_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe restoration preserves raw bytes even when its first write fails."""
    target = tmp_path / "target.rs"
    original = b"//! CRLF source.\r\n#![forbid(unsafe_code)]\r\n"
    target.write_bytes(original)
    probe = "pub fn unsafe_probe() { unsafe {} }"
    with probe_appended(target, probe):
        assert target.read_bytes() == original + b"\n" + probe.encode() + b"\n", (
            "the probe must preserve the original CRLF bytes"
        )
    assert target.read_bytes() == original, "the normal probe path must restore bytes"
    original_write = type(target).write_bytes

    def write_bytes(path: Path, data: bytes) -> int:
        """Simulate a partial first write while allowing the restoration write."""
        if path == target and data != original:
            original_write(path, data[:1])
            msg = "injected probe write failure"
            raise OSError(msg)
        return original_write(path, data)

    monkeypatch.setattr(type(target), "write_bytes", write_bytes)
    with (
        pytest.raises(OSError, match="injected probe write failure"),
        probe_appended(target, probe),
    ):
        pass
    assert target.read_bytes() == original, (
        "the failed first probe write must still restore the original raw bytes"
    )
