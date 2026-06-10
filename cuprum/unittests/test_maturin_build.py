"""Unit tests for maturin pin synchronization and wheel build output."""

from __future__ import annotations

import importlib.metadata as im
import re
import shutil
import sys
import typing as typ

import pytest

from tests.helpers.docs import repo_root
from tests.helpers.maturin import (
    build_native_wheel_artifact,
    read_expected_maturin_version,
    read_manylinux_aarch64_container_ref,
    read_maturin_pins,
    toolchain_available,
    wheel_build_snapshot,
    workflow_uses_manylinux_aarch64_container_ref,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


_MANYLINUX_CONTAINER_SHA256_RE = re.compile(
    r"^ghcr\.io/rust-cross/manylinux_2_28-cross@sha256:[0-9a-f]{64}$"
)


def test_maturin_pins_are_synchronized() -> None:
    """Maturin version pins stay aligned across CI and dev dependencies."""
    pins = read_maturin_pins(repo_root())
    assert len(set(pins.values())) == 1, f"Expected one maturin pin, found {pins!r}"


def test_installed_maturin_matches_expected_pin() -> None:
    """The active maturin CLI matches the pinned development dependency."""
    if shutil.which("maturin") is None:
        pytest.skip("maturin is not installed.")
    expected = read_expected_maturin_version(repo_root())
    installed = im.version("maturin")
    assert installed == expected, (
        f"Expected maturin {expected}, but {installed} is installed"
    )


def test_manylinux_aarch64_container_is_pinned_to_sha256() -> None:
    """Aarch64 manylinux container pin must be immutable."""
    container_ref = read_manylinux_aarch64_container_ref(repo_root())
    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref), (
        f"Expected SHA-256 pinned container ref, found {container_ref!r}"
    )


def test_manylinux_aarch64_container_is_referenced_by_build_step() -> None:
    """The build job should consume the pinned aarch64 container variable."""
    assert workflow_uses_manylinux_aarch64_container_ref(repo_root()), (
        "Expected build-wheels.yml to reference env.MANYLINUX_AARCH64_CONTAINER"
    )


# pytest-timeout arms SIGALRM in the *parent* process; pytest-forked blocks the
# parent in os.waitpid() while the child runs the maturin build. When SIGALRM
# fires there, pytest.fail() is raised outside any test handler → INTERNALERROR.
@pytest.mark.timeout(0)
def test_maturin_wheel_build_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """Native wheel metadata and layout match the expected maturin output."""
    root = repo_root()
    expected = read_expected_maturin_version(root)
    if not toolchain_available():
        pytest.skip("Rust toolchain unavailable.")
    if sys.version_info >= (3, 15):
        pytest.skip(f"maturin {expected} does not support this Python version.")

    wheel_path = build_native_wheel_artifact(root, tmp_path / "wheelhouse")
    snapshot_payload = wheel_build_snapshot(wheel_path)
    assert snapshot_payload["generator"] == expected, (
        f"Expected generator {expected!r}, found {snapshot_payload['generator']!r}"
    )
    assert snapshot_payload == snapshot, (
        "Built wheel metadata, file list, and build settings changed."
    )
