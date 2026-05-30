"""Unit tests for maturin pin synchronisation and wheel build output."""

from __future__ import annotations

import importlib.metadata as im
import shutil
import sys
import typing as typ

import pytest

from tests.helpers.maturin import (
    build_native_wheel,
    collect_maturin_pins,
    expected_maturin_version,
    toolchain_available,
    wheel_build_snapshot,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


def test_maturin_pins_are_synchronised() -> None:
    """Maturin version pins stay aligned across CI and dev dependencies."""
    pins = collect_maturin_pins()
    assert len(set(pins.values())) == 1, f"Expected one maturin pin, found {pins!r}"


def test_installed_maturin_matches_expected_pin() -> None:
    """The active maturin CLI matches the pinned development dependency."""
    if shutil.which("maturin") is None:
        pytest.skip("maturin is not installed.")
    expected = expected_maturin_version()
    installed = im.version("maturin")
    assert installed == expected, (
        f"Expected maturin {expected}, but {installed} is installed"
    )


try:
    _MATURIN_VERSION = expected_maturin_version()
except OSError:
    _MATURIN_VERSION = None
_MATURIN_SUPPORTS_PYTHON = sys.version_info < (3, 15)


@pytest.mark.skipif(not toolchain_available(), reason="Rust toolchain unavailable.")
@pytest.mark.skipif(
    not _MATURIN_SUPPORTS_PYTHON,
    reason=f"maturin {_MATURIN_VERSION} does not support this Python version.",
)
@pytest.mark.timeout(0)
def test_maturin_wheel_build_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """Native wheel metadata and layout match the expected maturin output."""
    wheel_path = build_native_wheel(tmp_path / "wheelhouse")
    snapshot_payload = wheel_build_snapshot(wheel_path)
    assert snapshot_payload["generator"] == expected_maturin_version()
    assert snapshot_payload == snapshot
