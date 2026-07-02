"""Unit tests for maturin pin synchronization and wheel build output."""

from __future__ import annotations

import importlib.metadata as im
import re
import shutil
import subprocess  # noqa: S404 - tests assert trusted maturin command handling.
import sys
import typing as typ

import pytest

from tests.helpers.docs import repo_root
from tests.helpers.maturin import (
    _AARCH64_CONTAINER_PIN_RE,
    _AARCH64_CONTAINER_USAGE_RE,
    MaturinBuildError,
    build_native_wheel_artifact,
    read_expected_maturin_version,
    read_manylinux_aarch64_container_ref,
    read_maturin_pins,
    toolchain_available,
    wheel_build_snapshot,
    workflow_uses_manylinux_aarch64_container_ref,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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


@pytest.mark.parametrize(
    "container_ref",
    [
        "ghcr.io/rust-cross/manylinux_2_28-cross:aarch64",
        "ghcr.io/rust-cross/manylinux_2_28-cross:latest",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:tooshort",
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:" + "g" * 64,
        "",
    ],
)
def test_manylinux_aarch64_container_ref_rejects_mutable_tag(
    container_ref: str,
) -> None:
    """Aarch64 manylinux container refs reject mutable or invalid pins."""
    assert _MANYLINUX_CONTAINER_SHA256_RE.fullmatch(container_ref) is None


def test_manylinux_aarch64_container_pin_regex_rejects_missing_comment() -> None:
    """Aarch64 manylinux container pins require the source-tag comment."""
    yaml_line = (
        "MANYLINUX_AARCH64_CONTAINER: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_PIN_RE.search(yaml_line) is None


def test_manylinux_aarch64_container_usage_regex_rejects_literal_image() -> None:
    """Aarch64 manylinux container usage requires the pinned env variable."""
    yaml_line = (
        "        container: "
        "ghcr.io/rust-cross/manylinux_2_28-cross@sha256:"
        "4864c3e931d790def6dba05cbf133b236b242d0c732f77546c68663c7923116e"
    )

    assert _AARCH64_CONTAINER_USAGE_RE.search(yaml_line) is None

def _build_with_fake_subprocess_run(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run: cabc.Callable[..., subprocess.CompletedProcess[str]],
) -> pth.Path:
    """Build the native wheel while replacing ``subprocess.run``."""
    monkeypatch.setattr(subprocess, "run", fake_run)
    return build_native_wheel_artifact(repo_root(), tmp_path / "wheelhouse")
def test_build_native_wheel_artifact_uses_locked_cargo_deps(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel builds pass ``--locked`` through to maturin."""
    captured_command: list[str] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Record the command and create the expected wheel artifact."""
        captured_command.extend(command)
        (tmp_path / "wheelhouse" / "cuprum-test.whl").touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    wheel_path = _build_with_fake_subprocess_run(tmp_path, monkeypatch, fake_run)

    assert wheel_path.name == "cuprum-test.whl", (
        "native wheel helper should return the wheel created by fake maturin"
    )
    assert "--locked" in captured_command, (
        "native wheel build should pass --locked through to maturin"
    )

def test_build_native_wheel_artifact_reports_maturin_stderr(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native wheel build failures include the command and captured stderr."""

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Raise a deterministic maturin command failure."""
        raise subprocess.CalledProcessError(
            101,
            command,
            output="stdout text",
            stderr="cargo fetch failed",
        )

    with pytest.raises(MaturinBuildError) as exc_info:
        _build_with_fake_subprocess_run(tmp_path, monkeypatch, fake_run)

    error_text = str(exc_info.value)
    assert exc_info.value.stderr == "cargo fetch failed", (
        "maturin build errors should preserve raw captured stderr"
    )
    assert "python" in error_text, (
        "maturin failure diagnostics should include the Python executable"
    )
    assert "maturin build" in error_text, (
        "maturin failure diagnostics should include the build command"
    )
    assert "cargo fetch failed" in error_text, (
        "maturin failure diagnostics should include captured stderr"
    )
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

    wheel_path = build_native_wheel_artifact(root, tmp_path / "wheelhouse")
    snapshot_payload = wheel_build_snapshot(wheel_path)
    assert snapshot_payload["generator"] == expected, (
        f"Expected generator {expected!r}, found {snapshot_payload['generator']!r}"
    )
    assert snapshot_payload == snapshot, (
        "Built wheel metadata, file list, and build settings changed."
    )
