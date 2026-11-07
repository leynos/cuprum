"""Drive the PyPy download and install over a controlled loopback HTTPS server.

These cases are opt-in: the default suite needs neither a socket nor the
`openssl` binary. `make boundary-download-integration` sets the variable they
require. Each case asserts on both the published artefact and the absence of a
pending or partial file, so a refused transfer is shown to fail closed.

The unit cases that drive a fake response through the same bounds live in
`test_boundary_pypy_transfer.py`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import platform
import tarfile
import typing as typ

import pytest

from scripts import install_pypy312 as installer
from scripts import pypy_download as downloader
from scripts.tests import boundary_download_scenarios as scenarios
from scripts.tests import boundary_download_support as support

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import ssl
    from pathlib import Path

RUN_ENV = "CUPRUM_RUN_DOWNLOAD_INTEGRATION"

#: The ceiling the loopback overflow case applies to the streamed body.
MAX_BYTES = 64 * 1024

#: The pinned PyPy archive is published for Linux x86-64 only.
LINUX_X86_64 = platform.system() == "Linux" and platform.machine() == "x86_64"

NO_DOWNLOAD_INTEGRATION = pytest.mark.skipif(
    os.environ.get(RUN_ENV) != "1",
    reason=f"set {RUN_ENV}=1, or run `make boundary-download-integration`",
)


class TestControlledHttpsTransfer:
    """`checked_download` must fail closed over a real TLS connection."""

    @pytest.fixture(scope="class")
    def certificates(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> support.Certificates:
        """Create one test CA and server certificate for the class."""
        return support.create_certificates(tmp_path_factory.mktemp("certificates"))

    @pytest.fixture
    def trust_test_ca(
        self,
        certificates: support.Certificates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Trust the test CA only for the connection this test opens."""
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates.ca))

    @NO_DOWNLOAD_INTEGRATION
    def test_a_verified_body_is_published_over_https(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
    ) -> None:
        """A verified body crosses a real TLS connection and is published."""
        destination = tmp_path / "archive.tar.gz"
        digest = hashlib.sha256(scenarios.FIXED_BODY).hexdigest()
        with _serving("fixed", certificates) as server:
            downloader.checked_download(f"{server.url}/asset.bin", destination, digest)
        assert destination.read_bytes() == scenarios.FIXED_BODY, (
            "the verified body must be published unchanged"
        )
        assert not destination.with_suffix(".download").exists(), (
            "no pending file may survive publication"
        )
        assert [request.path for request in server.requests] == ["/asset.bin"], (
            "a verified download must make exactly one request"
        )

    @NO_DOWNLOAD_INTEGRATION
    def test_an_oversized_body_leaves_no_partial_archive(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A body past the ceiling leaves neither a pending nor a final file."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", MAX_BYTES)
        destination = tmp_path / "archive.tar.gz"
        pending = destination.with_suffix(".download")
        with (
            _serving("stream_overflow", certificates) as server,
            pytest.raises(ValueError, match=f"exceeded the {MAX_BYTES}-byte limit"),
        ):
            downloader.checked_download(
                f"{server.url}/asset.bin", destination, "0" * 64
            )
        emitted = server.emitted
        assert emitted > MAX_BYTES, (
            "the server must exceed the ceiling, so the streamed limit stops it"
        )
        assert not destination.exists(), "the destination must not be created"
        assert not pending.exists(), "the pending file must be discarded"

    @NO_DOWNLOAD_INTEGRATION
    def test_a_redirected_overflow_leaves_no_partial_archive(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A redirected body past the ceiling leaves neither pending nor final."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", MAX_BYTES)
        destination = tmp_path / "archive.tar.gz"
        pending = destination.with_suffix(".download")
        with (
            _serving("redirect_overflow", certificates) as server,
            pytest.raises(ValueError, match=f"exceeded the {MAX_BYTES}-byte limit"),
        ):
            downloader.checked_download(f"{server.url}/start", destination, "0" * 64)
        emitted = server.emitted
        assert emitted > MAX_BYTES, (
            "the redirected server must exceed the ceiling before the limit stops it"
        )
        assert [request.path for request in server.requests] == [
            "/start",
            "/asset.bin",
        ], "the redirect hop must be followed before the ceiling is applied"
        assert not destination.exists(), "the destination must not be created"
        assert not pending.exists(), "the pending file must be discarded"

    @NO_DOWNLOAD_INTEGRATION
    def test_a_trickled_header_is_stopped_by_the_deadline(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A server that trickles header bytes cannot outlive the deadline."""
        monkeypatch.setattr(downloader, "_TRANSFER_DEADLINE_SECONDS", 1.0)
        destination = tmp_path / "archive.tar.gz"
        pending = destination.with_suffix(".download")
        with (
            _serving("trickle_headers", certificates) as server,
            pytest.raises(TimeoutError, match="transfer deadline"),
        ):
            downloader.checked_download(
                f"{server.url}/asset.bin", destination, "0" * 64
            )
        assert not destination.exists(), "the destination must not be created"
        assert not pending.exists(), "the pending file must be discarded"

    @NO_DOWNLOAD_INTEGRATION
    def test_a_late_eof_is_stopped_by_the_deadline(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A body that ends after the deadline is refused, not published."""
        monkeypatch.setattr(downloader, "_TRANSFER_DEADLINE_SECONDS", 1.0)
        destination = tmp_path / "archive.tar.gz"
        pending = destination.with_suffix(".download")
        with (
            _serving("late_eof", certificates) as server,
            pytest.raises(TimeoutError, match="transfer deadline"),
        ):
            downloader.checked_download(
                f"{server.url}/asset.bin", destination, "0" * 64
            )
        assert not destination.exists(), "the destination must not be created"
        assert not pending.exists(), "the pending file must be discarded"


class TestControlledHttpsInstall:
    """`install` must publish only a verified interpreter over real TLS."""

    @pytest.fixture(scope="class")
    def certificates(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> support.Certificates:
        """Create one test CA and server certificate for the class."""
        return support.create_certificates(tmp_path_factory.mktemp("certificates"))

    @pytest.fixture
    def trust_test_ca(
        self,
        certificates: support.Certificates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Trust the test CA only for the connection this test opens."""
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates.ca))

    @NO_DOWNLOAD_INTEGRATION
    @pytest.mark.skipif(
        not LINUX_X86_64, reason="the pinned PyPy archive is Linux x86-64 only"
    )
    def test_install_publishes_the_verified_interpreter(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
    ) -> None:
        """A verified archive downloaded over TLS supplies the interpreter."""
        archive = _archive(
            "pypy3.12-v8.0.0-linux64/bin/pypy3.12", b"synthetic pypy executable"
        )
        digest = hashlib.sha256(archive).hexdigest()
        with _serving_handler(_body_handler(archive), certificates) as server:
            installation = _installation(server, tmp_path, digest)
            installer.install(installation)
        assert installation.python.read_bytes() == b"synthetic pypy executable", (
            "the extracted interpreter bytes differ from the verified archive"
        )
        assert installation.python.stat().st_mode & 0o111, (
            "the extracted interpreter is not executable"
        )
        assert not installation.archive.with_suffix(".download").exists(), (
            "no pending file may survive a successful install"
        )

    @NO_DOWNLOAD_INTEGRATION
    @pytest.mark.skipif(
        not LINUX_X86_64, reason="the pinned PyPy archive is Linux x86-64 only"
    )
    def test_install_leaves_nothing_behind_when_the_download_fails(
        self,
        tmp_path: Path,
        certificates: support.Certificates,
        trust_test_ca: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused download through `install` leaves no archive or root."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 64)
        archive = _archive(
            "pypy3.12-v8.0.0-linux64/bin/pypy3.12", b"synthetic pypy executable"
        )
        with _serving_handler(_body_handler(archive), certificates) as server:
            installation = _installation(server, tmp_path, "0" * 64)
            with pytest.raises(ValueError, match="limit"):
                installer.install(installation)
        assert not installation.archive.exists(), "a failed install must not cache"
        assert not installation.archive.with_suffix(".download").exists(), (
            "a failed install must not leave a pending file"
        )
        assert not installation.root.exists(), "a failed install must not extract"


def _archive(member: str, content: bytes) -> bytes:
    """Build an executable-bearing gzip archive for one synthetic release."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as release:
        info = tarfile.TarInfo(member)
        info.mode = 0o755
        info.size = len(content)
        release.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _installation(
    server: support.LoopbackServer,
    tmp_path: Path,
    digest: str,
) -> installer.PyPyInstallation:
    """Create an isolated request matching the PyPy release archive layout."""
    root = tmp_path / "pypy3.12-v8.0.0-linux64"
    return installer.PyPyInstallation(
        url=f"{server.url}/asset.bin",
        digest=digest,
        archive=tmp_path / "pypy3.12-v8.0.0-linux64.tar.gz",
        root=root,
        python=root / "bin/pypy3.12",
    )


def _body_handler(body: bytes) -> support.Handler:
    """Serve one complete, correctly sized body for every request."""

    def handler(connection: ssl.SSLSocket, server: support.LoopbackServer) -> None:
        """Answer one request with the drawn archive body."""
        server.requests.append(support.read_request(connection))
        support.respond_with_body(connection, server, body)

    return handler


@contextlib.contextmanager
def _serving(
    scenario: str,
    certificates: support.Certificates,
) -> cabc.Iterator[support.LoopbackServer]:
    """Serve one named scenario until the caller is finished with the server."""
    with _serving_handler(scenarios.SCENARIOS[scenario], certificates) as server:
        yield server


@contextlib.contextmanager
def _serving_handler(
    handler: support.Handler,
    certificates: support.Certificates,
) -> cabc.Iterator[support.LoopbackServer]:
    """Serve one handler until the caller is finished with the server."""
    server = support.LoopbackServer(certificates, handler)
    try:
        yield server
    finally:
        server.close()
