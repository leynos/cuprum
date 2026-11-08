"""Bound the PyPy transfer by a byte ceiling and a transfer deadline.

The unit cases drive a fake response through `_download`, including the header
parse, a late EOF, and a redirect hop; the property case generates chunk
partitions against generated ceilings. The opt-in cases that run
`checked_download` and `install` against a controlled loopback HTTPS server live
in `test_boundary_pypy_https.py`, which needs a socket and the `openssl` binary.
"""

from __future__ import annotations

import http.client
import io
import socket
import threading
import typing as typ
from unittest import mock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from scripts import pypy_download as downloader

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: The deadline every unit case allows, so the clock is the only variable.
_DEADLINE = 1.0


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
        downloader.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    if clock is None:
        downloader._download("https://example.invalid/asset", destination)
    else:
        downloader._download("https://example.invalid/asset", destination, clock=clock)


def _fixed_clock() -> float:
    """Return a clock that never advances, so only bytes can stop a copy."""
    return 0.0


def _manual_clock() -> mock.Mock:
    """Return a callable clock the test moves by setting `return_value`."""
    return mock.Mock(return_value=0.0)


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
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 8)
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
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 8)
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
        monkeypatch.setattr(downloader, "_MAX_BODY_BYTES", 10)
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
        clock = _manual_clock()
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
            _download_response(response, destination, monkeypatch, clock=clock)
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
        response = _streaming_response(b"x" * 64)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            _download_response(response, destination, monkeypatch)
        assert response.read1.call_count == 0, "no chunk may be read past the deadline"

    def test_an_expired_transfer_is_refused_before_the_headers_are_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deadline that has already passed stops the header parse."""
        monkeypatch.setattr(downloader, "_TRANSFER_DEADLINE_SECONDS", -1.0)
        destination = tmp_path / "binary"
        connection = mock.Mock()
        monkeypatch.setattr(
            downloader.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download("https://example.invalid/asset", destination)
        assert connection.getresponse.call_count == 0, (
            "an expired transfer must not start reading headers"
        )
        assert not destination.exists(), "the destination must not be created"

    def test_a_late_eof_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that ends after the deadline is not accepted as complete."""
        clock = _manual_clock()
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
            _download_response(response, destination, monkeypatch, clock=clock)
        assert destination.read_bytes() == b"tail", (
            "the bytes read before the deadline must be kept"
        )

    def test_a_header_parse_stopped_by_the_deadline_is_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A status line the watchdog truncates reports the deadline."""
        clock = _manual_clock()
        connection = mock.Mock()

        def getresponse() -> mock.MagicMock:
            """Fail the header parse once the deadline has passed."""
            clock.return_value = 10_000.0
            raise http.client.BadStatusLine("HTTP/1")

        connection.getresponse.side_effect = getresponse
        monkeypatch.setattr(
            downloader.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/asset", tmp_path / "binary", clock=clock
            )

    def test_a_body_parse_stopped_by_the_deadline_is_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk frame the watchdog truncates reports the deadline."""
        clock = _manual_clock()
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
            _download_response(response, tmp_path / "binary", monkeypatch, clock=clock)

    def test_a_body_parse_failure_before_the_deadline_is_propagated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parser failure inside the deadline stays the server's error."""
        response = _streaming_response(b"x")
        response.read1.side_effect = http.client.IncompleteRead(b"partial")
        with pytest.raises(http.client.IncompleteRead):
            _download_response(response, tmp_path / "binary", monkeypatch)

    def test_the_deadline_spans_every_redirect_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect hop does not restart the deadline for the body."""
        clock = _manual_clock()
        redirect = mock.MagicMock()
        redirect.__enter__.return_value = redirect
        redirect.status = 302
        redirect.getheader.return_value = "https://example.invalid/asset"
        body = _response(None, [b"payload"])

        def getresponse() -> mock.MagicMock:
            """Answer the first hop, then move the clock past the deadline."""
            clock.return_value = 10_000.0
            return redirect

        connection = mock.Mock()
        connection.getresponse.side_effect = getresponse
        monkeypatch.setattr(
            downloader.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader._download(
                "https://example.invalid/start",
                tmp_path / "binary",
                clock=clock,
            )
        assert body.read1.call_count == 0, (
            "the deadline set before the redirect must still stop the body"
        )


class TestDeadlineWatchdog:
    """The watchdog must stop the socket a close-delimited response owns."""

    def test_the_socket_is_captured_before_the_response_takes_it(self) -> None:
        """The socket is armed before `getresponse` can clear the reference."""
        stopped = threading.Event()
        sock = mock.Mock()
        sock.shutdown.side_effect = lambda _how: stopped.set()
        connection = mock.Mock()
        connection.sock = sock
        watchdog = downloader._DeadlineWatchdog(
            connection, downloader._Transfer(0.0, _fixed_clock)
        )
        # A close-delimited response leaves the socket with the response and
        # clears the connection's reference, as `getresponse` does.
        connection.sock = None
        with watchdog:
            # Exiting immediately would signal `_stop` before the watchdog
            # thread runs, which legitimately skips the shutdown.
            assert stopped.wait(5.0), "the watchdog must shut the socket down"
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)


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
            _response(None, chunks),
            output,
            downloader._Transfer(_DEADLINE, _fixed_clock),
        )
        assert output.getvalue() == body, (
            "a body inside the ceiling must be copied unchanged"
        )
        return
    with pytest.raises(ValueError, match=f"exceeded the {limit}-byte limit"):
        downloader._copy_body(
            _response(None, chunks),
            output,
            downloader._Transfer(_DEADLINE, _fixed_clock),
        )
    written = output.getvalue()
    assert len(written) <= limit, "no write may take the output past the ceiling"
    assert body.startswith(written), "only a prefix of the body may be written"
