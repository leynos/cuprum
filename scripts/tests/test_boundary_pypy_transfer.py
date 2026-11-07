"""Bound the PyPy transfer by a byte ceiling and a transfer deadline.

The unit cases drive a fake response through `_copy_body`; the property case
generates chunk partitions against generated ceilings; and the opt-in cases run
`checked_download` against a controlled loopback HTTPS server, so one real
socket and TLS handshake cover the same boundary.

The loopback cases are opt-in because the default suite needs neither a socket
nor the `openssl` binary. `make boundary-download-integration` sets the
variable they require.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import os
import typing as typ
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts import install_pypy312 as installer
from scripts.tests import boundary_download_scenarios as scenarios
from scripts.tests import boundary_download_support as support

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

RUN_ENV = "CUPRUM_RUN_DOWNLOAD_INTEGRATION"

#: The ceiling the loopback overflow case applies to the streamed body.
MAX_BYTES = 64 * 1024

#: The deadline every unit case allows, so the clock is the only variable.
_DEADLINE = 1.0

NO_DOWNLOAD_INTEGRATION = pytest.mark.skipif(
    os.environ.get(RUN_ENV) != "1",
    reason=f"set {RUN_ENV}=1, or run `make boundary-download-integration`",
)


def _response(declared: str | None, chunks: cabc.Iterable[bytes]) -> mock.MagicMock:
    """Build a 200 response whose `read1` yields `chunks` and then EOF."""
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = http.client.OK
    response.getheader.side_effect = lambda name: (
        declared if name == "Content-Length" else None
    )
    response.read1.side_effect = [*chunks, b""]
    return response


def _streaming_response(body: bytes, declared: str | None = None) -> mock.MagicMock:
    """Build a 200 response that streams `body` and reports `declared`."""
    return _response(declared, [body])


def _download_response(
    response: mock.MagicMock,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: cabc.Callable[[], float] | None = None,
) -> None:
    """Run one download against `response` without touching the network."""
    connection = mock.Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        installer.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    if clock is None:
        installer._download("https://example.invalid/asset", destination)
    else:
        installer._download("https://example.invalid/asset", destination, clock=clock)


def _fixed_clock() -> float:
    """Return a clock that never advances, so only bytes can stop a copy."""
    return 0.0


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

    def test_several_small_chunks_that_exceed_the_ceiling_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chunks each below the ceiling are refused once their total passes it."""
        monkeypatch.setattr(installer, "_MAX_BODY_BYTES", 10)
        destination = tmp_path / "binary"
        response = _response(None, [b"abcd", b"abcd", b"abcd"])
        with pytest.raises(ValueError, match="exceeded the 10-byte limit"):
            _download_response(response, destination, monkeypatch)
        assert destination.read_bytes() == b"abcdabcd", (
            "only the chunks accepted before the ceiling may be written"
        )
        assert response.read1.call_count == 3, (
            "the transfer must stop at the chunk that crosses the ceiling"
        )

    def test_a_deadline_advanced_after_a_read_stops_the_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clock that passes the deadline after one chunk stops the next read."""
        # One tick sets the deadline, one admits the first chunk, and the third
        # is far past `_TRANSFER_DEADLINE_SECONDS` however the module sets it.
        ticks = iter([0.0, 0.0, 10_000.0])
        destination = tmp_path / "binary"
        response = _response(None, [b"first", b"second"])
        with pytest.raises(TimeoutError, match="transfer deadline"):
            _download_response(
                response,
                destination,
                monkeypatch,
                clock=lambda: next(ticks),
            )
        assert response.read1.call_count == 1, "no chunk may be read past the deadline"
        assert destination.read_bytes() == b"first", (
            "the chunk accepted before the deadline must be kept"
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

    def test_the_deadline_spans_every_redirect_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect hop does not restart the deadline for the body."""
        # One tick sets the deadline; the next is far past it at the body hop.
        ticks = iter([0.0, 10_000.0])
        redirect = mock.MagicMock()
        redirect.__enter__.return_value = redirect
        redirect.status = 302
        redirect.getheader.return_value = "https://example.invalid/asset"
        body = _response(None, [b"payload"])
        connection = mock.Mock()
        connection.getresponse.side_effect = [redirect, body]
        monkeypatch.setattr(
            installer.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            installer._download(
                "https://example.invalid/start",
                tmp_path / "binary",
                clock=lambda: next(ticks),
            )
        assert body.read1.call_count == 0, (
            "the deadline set before the redirect must still stop the body"
        )


class TestPendingFile:
    """A refused transfer must not leave a partial archive behind."""

    def test_a_failed_transfer_removes_the_pending_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download that raises after writing leaves no pending or final file."""
        archive = tmp_path / "binary.tar.gz"
        pending = archive.with_suffix(".download")

        def fetch(_url: str, destination: Path) -> None:
            """Write partial bytes, then fail the way a deadline would."""
            destination.write_bytes(b"partial body")
            msg = "release download exceeded the transfer deadline"
            raise TimeoutError(msg)

        monkeypatch.setattr(installer, "_download", fetch)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            installer.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )
        assert not pending.exists(), "the pending file must be discarded"
        assert not archive.exists(), "a failed transfer must not publish an archive"


# Monkeypatch is a function-scoped fixture, but it only sets one module constant
# per example, so Hypothesis's fixture-reset warning does not apply here.
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    limit=st.integers(min_value=0, max_value=64),
    chunks=st.lists(st.binary(min_size=1, max_size=8), max_size=12),
)
def test_copy_body_keeps_accepted_bytes_and_refuses_overflow(
    limit: int,
    chunks: list[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body inside the ceiling is copied whole; a longer one is refused."""
    monkeypatch.setattr(installer, "_MAX_BODY_BYTES", limit)
    body = b"".join(chunks)
    output = io.BytesIO()
    if len(body) <= limit:
        installer._copy_body(
            _response(None, chunks),
            output,
            installer._Transfer(_DEADLINE, _fixed_clock),
        )
        assert output.getvalue() == body, (
            "a body inside the ceiling must be copied unchanged"
        )
        return
    with pytest.raises(ValueError, match=f"exceeded the {limit}-byte limit"):
        installer._copy_body(
            _response(None, chunks),
            output,
            installer._Transfer(_DEADLINE, _fixed_clock),
        )
    written = output.getvalue()
    assert len(written) <= limit, "no write may take the output past the ceiling"
    assert body.startswith(written), "only a prefix of the body may be written"


class TestControlledHttpsTransfer:
    """`checked_download` must fail closed over a real loopback TLS server."""

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
            installer.checked_download(f"{server.url}/asset.bin", destination, digest)
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
        monkeypatch.setattr(installer, "_MAX_BODY_BYTES", MAX_BYTES)
        destination = tmp_path / "archive.tar.gz"
        pending = destination.with_suffix(".download")
        with _serving("stream_overflow", certificates) as server:
            with pytest.raises(
                ValueError, match=f"exceeded the {MAX_BYTES}-byte limit"
            ):
                installer.checked_download(
                    f"{server.url}/asset.bin", destination, "0" * 64
                )
            emitted = server.emitted
        assert emitted > MAX_BYTES, (
            "the server must exceed the ceiling, so the streamed limit stops it"
        )
        assert not destination.exists(), "the destination must not be created"
        assert not pending.exists(), "the pending file must be discarded"


@contextlib.contextmanager
def _serving(
    scenario: str,
    certificates: support.Certificates,
) -> cabc.Iterator[support.LoopbackServer]:
    """Serve one scenario until the caller is finished with the server."""
    server = support.LoopbackServer(certificates, scenarios.SCENARIOS[scenario])
    try:
        yield server
    finally:
        server.close()
