"""Exercise PyPy archive installation without downloading the real release."""

from __future__ import annotations

import io
import tarfile
import typing as typ

import pytest

from scripts import install_pypy312 as installer

if typ.TYPE_CHECKING:
    from pathlib import Path


def _archive(member: str, content: bytes) -> bytes:
    """Build an executable-bearing gzip archive for one synthetic release."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as release:
        info = tarfile.TarInfo(member)
        info.mode = 0o755
        info.size = len(content)
        release.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _installation(tmp_path: Path) -> installer.PyPyInstallation:
    """Create an isolated request matching the PyPy release archive layout."""
    root = tmp_path / "pypy3.12-v8.0.0-linux64"
    archive = tmp_path / "pypy3.12-v8.0.0-linux64.tar.gz"
    return installer.PyPyInstallation(
        url="https://example.invalid/pypy.tar.gz",
        digest="0" * 64,
        archive=archive,
        root=root,
        python=root / "bin/pypy3.12",
    )


def test_install_extracts_the_verified_expected_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified release archive supplies the configured executable path."""
    installation = _installation(tmp_path)
    archive = _archive(
        "pypy3.12-v8.0.0-linux64/bin/pypy3.12", b"synthetic pypy executable"
    )
    requested: list[tuple[str, Path, str]] = []

    def download(url: str, destination: Path, digest: str) -> None:
        """Record and supply the release archive after checking its pin."""
        requested.append((url, destination, digest))
        destination.write_bytes(archive)

    monkeypatch.setattr(installer, "checked_download", download)

    installer.install(installation)

    assert requested == [
        (installation.url, installation.archive, installation.digest)
    ], "the installer did not request the pinned PyPy archive"
    assert installation.python.read_bytes() == b"synthetic pypy executable", (
        "the extracted interpreter bytes differ from the verified archive"
    )
    assert installation.python.stat().st_mode & 0o111, (
        "the extracted interpreter is not executable"
    )


def test_install_rejects_an_incomplete_existing_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing root without the expected executable fails without extraction."""
    installation = _installation(tmp_path)
    installation.root.mkdir()
    archive = b"verified archive"

    def download(_url: str, destination: Path, _digest: str) -> None:
        """Supply a cache file as the Makefile recipe did before extraction."""
        destination.write_bytes(archive)

    monkeypatch.setattr(installer, "checked_download", download)

    with pytest.raises(RuntimeError, match="extraction is incomplete"):
        installer.install(installation)
    assert installation.archive.read_bytes() == archive, (
        "the verified archive should remain after an incomplete extraction"
    )


def test_install_rejects_a_platform_without_the_pinned_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive remains Linux x86_64-only instead of selecting another PyPy."""
    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")

    with pytest.raises(RuntimeError, match="Linux x86_64"):
        installer._require_linux_x86_64()
