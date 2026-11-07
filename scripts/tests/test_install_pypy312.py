"""Exercise PyPy archive installation without downloading the real release."""

from __future__ import annotations

import hashlib
import http.client
import io
import tarfile
from pathlib import Path
from unittest import mock

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


class TestCheckedDownload:
    """The shared HTTPS fetch must fail closed on every checksum mismatch."""

    def test_cached_binary_must_match_digest(self, tmp_path: Path) -> None:
        """A verified cache is reusable without performing a download."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"verified archive")
        digest = hashlib.sha256(b"verified archive").hexdigest()
        installer.checked_download("https://example.invalid/asset", archive, digest)
        assert archive.read_bytes() == b"verified archive", (
            "verified cache was modified"
        )

    def test_mismatch_preserves_existing_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt downloads cannot replace a cached artefact or leave a partial."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"old cache")

        def fetch(_url: str, destination: Path) -> None:
            """Supply a corrupt release body without network access."""
            destination.write_bytes(b"corrupt body")

        monkeypatch.setattr(installer, "_download", fetch)
        with pytest.raises(ValueError, match="checksum mismatch"):
            installer.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )
        assert archive.read_bytes() == b"old cache", (
            "corrupt download replaced the cache"
        )
        assert not archive.with_suffix(".download").exists(), "partial download leaked"

    def test_an_unreadable_cache_is_refused_rather_than_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache entry that cannot be read must not be answered with a download."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"cached archive")
        downloads: list[str] = []

        def fetch(url: str, _destination: Path) -> None:
            """Record any attempted download so the test can reject it."""
            downloads.append(url)

        def unreadable(_path: Path) -> str:
            """Stand in for a cache entry the process cannot read."""
            raise PermissionError("unreadable")

        monkeypatch.setattr(installer, "_download", fetch)
        monkeypatch.setattr(installer, "_digest", unreadable)

        with pytest.raises(PermissionError):
            installer.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )

        assert not downloads, "an unreadable cache must not start a download"
        assert archive.read_bytes() == b"cached archive", (
            "an unreadable cache must be left in place"
        )

    def test_a_missing_cache_entry_is_a_miss_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent cache entry downloads normally instead of raising."""
        archive = tmp_path / "binary.tar.gz"
        content = b"fresh archive"
        digest = hashlib.sha256(content).hexdigest()

        def fetch(_url: str, destination: Path) -> None:
            """Supply a fresh release body without network access."""
            destination.write_bytes(content)

        monkeypatch.setattr(installer, "_download", fetch)
        installer.checked_download("https://example.invalid/asset", archive, digest)
        assert archive.read_bytes() == content, "the missing entry was not installed"

    def test_plain_http_is_rejected(self, tmp_path: Path) -> None:
        """The installer cannot fetch an artefact over an unauthenticated scheme."""
        with pytest.raises(ValueError, match="HTTPS"):
            installer.checked_download(
                "http://example.invalid/asset", tmp_path / "binary", "0" * 64
            )

    @pytest.mark.parametrize(
        "redirect",
        [
            ("http://example.invalid/asset", "require HTTPS"),
            (None, "no Location"),
            ("/another-redirect", "redirect limit"),
        ],
    )
    def test_release_redirects_fail_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        redirect: tuple[str | None, str],
    ) -> None:
        """Insecure, malformed, and looping redirects cannot install a binary."""
        location, message = redirect

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 302
        response.getheader.return_value = location
        connection = mock.Mock()
        connection.getresponse.return_value = response
        monkeypatch.setattr(
            installer.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(ValueError, match=message):
            installer._download("https://example.invalid/asset", tmp_path / "binary")
        connection.close.assert_called()
        assert not (tmp_path / "binary").exists(), "rejected redirect created a binary"


def _streaming_response(body: bytes, declared: str | None = None) -> mock.MagicMock:
    """Build a 200 response that streams `body` and reports `declared`."""
    stream = io.BytesIO(body)
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = http.client.OK
    response.getheader.side_effect = lambda name: (
        declared if name == "Content-Length" else None
    )
    response.read1.side_effect = stream.read1
    return response


def _download_response(
    response: mock.MagicMock, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run one download against `response` without touching the network."""
    connection = mock.Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        installer.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    installer._download("https://example.invalid/asset", destination)


class TestBoundedBody:
    """The PyPy fetch must not outlive its byte ceiling or transfer deadline."""

    def test_a_bounded_body_is_copied_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body inside both bounds is written unchanged."""
        destination = tmp_path / "binary"
        _download_response(
            _streaming_response(b"archive bytes"), destination, monkeypatch
        )
        assert destination.read_bytes() == b"archive bytes", (
            "a bounded body must be copied in full"
        )

    def test_a_declared_oversize_body_is_refused_before_it_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An oversized Content-Length fails before any byte reaches the disk."""
        monkeypatch.setattr(installer, "_MAX_BODY_BYTES", 8)
        destination = tmp_path / "binary"
        response = _streaming_response(b"x" * 64, declared="9")
        with pytest.raises(ValueError, match="above the 8-byte limit"):
            _download_response(response, destination, monkeypatch)
        assert response.read1.call_count == 0, "the body must not be read"
        assert not destination.exists(), "the destination must not be created"

    def test_a_streamed_overflow_stops_at_the_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared body is abandoned once it passes the ceiling."""
        monkeypatch.setattr(installer, "_MAX_BODY_BYTES", 8)
        destination = tmp_path / "binary"
        with pytest.raises(ValueError, match="exceeded the 8-byte limit"):
            _download_response(_streaming_response(b"x" * 64), destination, monkeypatch)
        assert destination.read_bytes() == b"", (
            "an over-ceiling chunk must not be written"
        )

    def test_a_transfer_that_outlives_the_deadline_is_abandoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired deadline stops the copy before another chunk is read."""
        monkeypatch.setattr(installer, "_TRANSFER_DEADLINE_SECONDS", 0.0)
        destination = tmp_path / "binary"
        response = _streaming_response(b"x" * 64)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            _download_response(response, destination, monkeypatch)
        assert response.read1.call_count == 0, "no chunk may be read past the deadline"
