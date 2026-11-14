"""Bound the PyPy transfer by a byte ceiling and a transfer deadline.

The unit cases drive a fake response through `_download`, including the header
parse, a late EOF, and a redirect hop; the property case generates chunk
partitions against generated ceilings. The opt-in cases that run
`checked_download` and `install` against a controlled loopback HTTPS server live
in `test_boundary_pypy_https.py`, which needs a socket and the `openssl` binary.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import typing as typ
from unittest import mock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from scripts import pypy_download as downloader
from scripts import pypy_download_deadline as deadline
from scripts.tests.boundary_pypy_transfer_support import (
    download_response,
    fake_environment,
    fake_response,
    fixed_clock,
    manual_clock,
    redirect_response,
    streaming_response,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The deadline every unit case allows, so the clock is the only variable.
_DEADLINE = 1.0


class TestBoundedBody:
    """The PyPy fetch must not outlive its byte ceiling or transfer deadline."""

    def test_a_bounded_body_is_copied_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body inside both bounds is written unchanged."""
        destination = tmp_path / "binary"
        download_response(streaming_response(b"archive bytes"), destination)
        assert destination.read_bytes() == b"archive bytes", (
            "a bounded body must be copied in full"
        )

    def test_a_declared_oversize_body_is_refused_before_it_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An oversized Content-Length fails before any byte reaches the disk."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 8)
        destination = tmp_path / "binary"
        response = streaming_response(b"x" * 64, declared="9")
        with pytest.raises(ValueError, match="above the 8-byte limit"):
            download_response(response, destination)
        assert response.read1.call_count == 0, "the body must not be read"
        assert not destination.exists(), "the destination must not be created"

    def test_a_streamed_overflow_stops_at_the_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared body is abandoned once it passes the ceiling."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 8)
        destination = tmp_path / "binary"
        with pytest.raises(ValueError, match="exceeded the 8-byte limit"):
            download_response(streaming_response(b"x" * 64), destination)
        assert destination.read_bytes() == b"", (
            "an over-ceiling chunk must not be written"
        )

    def test_several_small_chunks_that_exceed_the_ceiling_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chunks each below the ceiling are refused once their total passes it."""
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 10)
        destination = tmp_path / "binary"
        response = fake_response(None, [b"abcd", b"abcd", b"abcd"])
        with pytest.raises(ValueError, match="exceeded the 10-byte limit"):
            download_response(response, destination)
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
        clock = manual_clock()
        destination = tmp_path / "binary"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = http.client.OK
        response.getheader.return_value = None

        def read1(_size: int) -> bytes:
            """Hand back one chunk and move the clock past the deadline."""
            clock.return_value = 10_000.0
            return b"first"

        response.read1.side_effect = read1
        with pytest.raises(TimeoutError, match="transfer deadline"):
            download_response(response, destination, clock=clock)
        assert response.read1.call_count == 1, "no chunk may be read past the deadline"
        assert destination.read_bytes() == b"first", (
            "the chunk accepted before the deadline must be kept"
        )

    def test_a_transfer_that_outlives_the_deadline_is_abandoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired deadline stops the copy before another chunk is read."""
        monkeypatch.setattr(downloader, "_TRANSFER_DEADLINE_SECONDS", 0.0)
        destination = tmp_path / "binary"
        response = streaming_response(b"x" * 64)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            download_response(response, destination)
        assert response.read1.call_count == 0, "no chunk may be read past the deadline"

    def test_an_expired_transfer_is_refused_before_the_headers_are_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deadline that has already passed stops the header parse."""
        monkeypatch.setattr(downloader, "_TRANSFER_DEADLINE_SECONDS", -1.0)
        destination = tmp_path / "binary"
        connection = mock.Mock()
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset",
                destination,
                fake_environment(connection),
            )
        assert connection.getresponse.call_count == 0, (
            "an expired transfer must not start reading headers"
        )
        assert not destination.exists(), "the destination must not be created"

    def test_a_late_eof_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that ends after the deadline is not accepted as complete."""
        clock = manual_clock()
        destination = tmp_path / "binary"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = http.client.OK
        response.getheader.return_value = None
        chunks = iter([b"tail", b""])

        def read1(_size: int) -> bytes:
            """Hand back the body, then reach EOF after the deadline."""
            chunk = next(chunks)
            if not chunk:
                clock.return_value = 10_000.0
            return chunk

        response.read1.side_effect = read1
        with pytest.raises(TimeoutError, match="transfer deadline"):
            download_response(response, destination, clock=clock)
        assert destination.read_bytes() == b"tail", (
            "the bytes read before the deadline must be kept"
        )

    def test_a_header_parse_stopped_by_the_deadline_is_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A status line the watchdog truncates reports the deadline."""
        clock = manual_clock()
        connection = mock.Mock()

        def getresponse() -> mock.MagicMock:
            """Fail the header parse once the deadline has passed."""
            clock.return_value = 10_000.0
            raise http.client.BadStatusLine("HTTP/1")

        connection.getresponse.side_effect = getresponse
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset",
                tmp_path / "binary",
                fake_environment(connection, clock=clock),
            )

    def test_a_body_parse_stopped_by_the_deadline_is_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk frame the watchdog truncates reports the deadline."""
        clock = manual_clock()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = http.client.OK
        response.getheader.return_value = None

        def read1(_size: int) -> bytes:
            """Fail the chunk parse once the deadline has passed."""
            clock.return_value = 10_000.0
            raise http.client.IncompleteRead(b"partial")

        response.read1.side_effect = read1
        with pytest.raises(TimeoutError, match="transfer deadline"):
            download_response(response, tmp_path / "binary", clock=clock)

    def test_a_body_parse_failure_before_the_deadline_is_propagated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parser failure inside the deadline stays the server's error."""
        response = streaming_response(b"x")
        response.read1.side_effect = http.client.IncompleteRead(b"partial")
        with pytest.raises(http.client.IncompleteRead):
            download_response(response, tmp_path / "binary")

    def test_the_deadline_spans_every_redirect_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two hops inside the deadline still fail once their total exceeds it."""
        clock = manual_clock()
        redirect = redirect_response("https://example.invalid/asset")

        def redirect_header(name: str) -> str | None:
            """Answer the header, ending the first hop at 600 s elapsed."""
            clock.return_value = 600.0
            return "https://example.invalid/asset" if name == "Location" else None

        redirect.getheader.side_effect = redirect_header
        body = fake_response(None, [b"payload"])
        reads = iter([b"payload", b""])

        def read1(_size: int) -> bytes:
            """Serve the body, then reach EOF only past the shared deadline."""
            chunk = next(reads)
            clock.return_value = 800.0 if chunk else 1_000.0
            return chunk

        body.read1.side_effect = read1
        first, second = mock.Mock(), mock.Mock()
        first.getresponse.return_value = redirect
        second.getresponse.return_value = body
        environment = fake_environment(first, second, clock=clock)
        archive = tmp_path / "binary"
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader.checked_download(
                "https://example.invalid/start",
                archive,
                "0" * 64,
                environment=environment,
            )
        assert body.read1.call_count == 2, "the final hop must serve its body"
        assert not archive.exists(), "a timed-out transfer must publish nothing"
        assert not archive.with_suffix(".download").exists(), (
            "a timed-out transfer must leave no pending file"
        )

    def test_a_redirected_download_inside_the_deadline_publishes_the_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect followed by a body inside the deadline publishes the asset."""
        first, second = mock.Mock(), mock.Mock()
        first.getresponse.return_value = redirect_response(
            "https://example.invalid/asset"
        )
        second.getresponse.return_value = streaming_response(b"payload")
        environment = fake_environment(first, second, clock=manual_clock())
        archive = tmp_path / "binary"
        downloader.checked_download(
            "https://example.invalid/start",
            archive,
            hashlib.sha256(b"payload").hexdigest(),
            environment=environment,
        )
        assert archive.read_bytes() == b"payload", (
            "a redirected download must publish the verified body"
        )


class TestPendingFile:
    """A refused transfer must not leave a partial archive behind."""

    def test_a_failed_transfer_removes_the_pending_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download that raises after writing leaves no pending or final file."""
        archive = tmp_path / "binary.tar.gz"
        pending = archive.with_suffix(".download")

        def fetch(
            _url: str, destination: Path, _environment: downloader.Environment
        ) -> None:
            """Write partial bytes, then fail the way a deadline would."""
            destination.write_bytes(b"partial body")
            msg = "release download exceeded the transfer deadline"
            raise TimeoutError(msg)

        monkeypatch.setattr(downloader, "_download", fetch)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader.checked_download(
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
@example(limit=4, chunks=[b"ab", b"cd"])
@example(limit=4, chunks=[b"ab", b"cd", b"e"])
def test_copy_body_keeps_accepted_bytes_and_refuses_overflow(
    limit: int,
    chunks: list[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body inside the ceiling is copied whole; a longer one is refused."""
    monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", limit)
    body = b"".join(chunks)
    output = io.BytesIO()
    if len(body) <= limit:
        downloader._copy_body(
            fake_response(None, chunks),
            output,
            deadline._Transfer(_DEADLINE, fixed_clock),
        )
        assert output.getvalue() == body, (
            "a body inside the ceiling must be copied unchanged"
        )
        return
    with pytest.raises(ValueError, match=f"exceeded the {limit}-byte limit"):
        downloader._copy_body(
            fake_response(None, chunks),
            output,
            deadline._Transfer(_DEADLINE, fixed_clock),
        )
    written = output.getvalue()
    assert len(written) <= limit, "no write may take the output past the ceiling"
    assert body.startswith(written), "only a prefix of the body may be written"
