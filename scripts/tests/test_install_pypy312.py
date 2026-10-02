"""Exercise PyPy archive installation without downloading the real release."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from scripts import install_pypy312 as installer

#: The default suite runs these tests only while this module is named in
#: `PYTEST_TARGETS`; the coverage job's bare `pytest` is not a substitute,
#: because a change is expected to fail the run the author invoked.
_REGISTRATION = "scripts/tests/test_install_pypy312.py"
_PYTEST_TARGETS = "PYTEST_TARGETS"


def test_this_module_stays_registered_in_the_default_suite() -> None:
    """Keep the installer tests running in `make test`, not only in coverage.

    `PYTEST_TARGETS` is an explicit allow-list whose entries are matched by
    name and by glob, so neither a rename nor a glob narrowing would be
    reported by any other check: the module would simply stop running. This
    asserts the entry survives verbatim.
    """
    makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(
        encoding="utf-8"
    )
    targets_line = next(
        line
        for line in makefile.splitlines()
        if line.startswith(f"{_PYTEST_TARGETS} ?=")
    )
    # The variable is written as a backslash-continued block, so the entry may
    # sit on any of the following lines rather than the declaration line.
    block = makefile[makefile.index(targets_line) :].split("\n\n", 1)[0]
    assert _REGISTRATION in block, (
        f"{_REGISTRATION} must stay listed in Makefile {_PYTEST_TARGETS}, or "
        "`make test` stops collecting these tests while coverage alone still "
        "runs them"
    )


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


def test_install_reuses_an_existing_executable_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete destination is reused, so no download or extraction happens."""
    installation = _installation(tmp_path)
    installation.python.parent.mkdir(parents=True)
    installation.python.write_bytes(b"already installed")
    installation.python.chmod(0o755)

    def download(_url: str, _destination: Path, _digest: str) -> None:
        """Fail loudly, proving a complete installation is never re-downloaded."""
        pytest.fail("a complete installation must be reused, not re-downloaded")

    monkeypatch.setattr(installer, "checked_download", download)

    installer.install(installation)

    assert not installation.archive.exists(), (
        "reusing an installed interpreter must not fetch the archive"
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported platform is refused before the pinned archive is fetched.

    Exercised through ``install`` rather than the private platform guard: the
    guard is only load-bearing while it sits ahead of the download and
    extraction, so the assertion also pins that order. Nothing may be written
    on the way out.
    """
    installation = _installation(tmp_path)
    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")

    def download(_url: str, _destination: Path, _digest: str) -> None:
        """Fail loudly, proving the platform check runs before any download."""
        pytest.fail("the unsupported platform must be rejected before download")

    monkeypatch.setattr(installer, "checked_download", download)

    with pytest.raises(RuntimeError, match="Linux x86_64"):
        installer.install(installation)

    assert not installation.root.exists(), (
        "a rejected platform must not leave a destination directory behind"
    )
    assert not installation.archive.exists(), (
        "a rejected platform must not leave a downloaded archive behind"
    )
