"""Response behaviours the bounded-download integration tests drive.

Each handler serves one connection and leaves the interesting condition in
place: an oversized declared length, an unbounded body, a stall, a trickle, a
truncation, or a redirect. The names match the cases in
`test_boundary_download_integration.py`.
"""

from __future__ import annotations

import typing as typ

from scripts.tests import boundary_download_support as support

if typ.TYPE_CHECKING:
    import ssl

    from scripts.tests.boundary_download_support import LoopbackServer

#: The body every successful scenario serves. Tests hash it to build the pin.
FIXED_BODY = b"cuprum bounded download\n"

#: The declared length the oversize scenario advertises.
OVERSIZE_CONTENT_LENGTH = 10_000_000

#: Stop an overflow stream that the client never stops reading.
_OVERFLOW_CAP_BYTES = 8 * 1024 * 1024

_OVERFLOW_CHUNK = b"y" * 4096


def _head(status: str, headers: str = "") -> bytes:
    """Build a response head with the given status line and extra headers."""
    return f"HTTP/1.1 {status}\r\n{headers}\r\n".encode("ascii")


def _body_response(body: bytes) -> bytes:
    """Build a complete 200 response for `body`."""
    return _head("200 OK", f"Content-Length: {len(body)}\r\n") + body


def fixed(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Serve one complete, correctly sized body."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_body_response(FIXED_BODY))
    server.emitted += len(FIXED_BODY)


def declared_oversize(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Advertise a body far above the ceiling and send none of it."""
    server.requests.append(support.read_request(connection))
    connection.sendall(
        _head("200 OK", f"Content-Length: {OVERSIZE_CONTENT_LENGTH}\r\n"),
    )
    server.wait(10.0)


def _stream_past_the_ceiling(
    connection: ssl.SSLSocket,
    server: LoopbackServer,
) -> None:
    """Send an unlengthened body until the client stops reading."""
    connection.sendall(_head("200 OK"))
    # The cap only stops a client that never stops reading; the client's own
    # ceiling is far below it, and a client that closes the socket ends the
    # loop with a failed send.
    while server.emitted < _OVERFLOW_CAP_BYTES:
        support.emit(connection, server, _OVERFLOW_CHUNK)
        if not server.wait(0.002):
            return


def stream_overflow(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Stream an unlengthened body until the client stops reading."""
    server.requests.append(support.read_request(connection))
    _stream_past_the_ceiling(connection, server)


def stall(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Send headers and then no body at all."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("200 OK", "Content-Length: 1000000\r\n"))
    server.wait(10.0)


def trickle_body(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Send one body byte at a time, slowly enough to outlive the deadline."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("200 OK", "Content-Length: 1000000\r\n"))
    for _ in range(100):
        support.emit(connection, server, b"w")
        if not server.wait(0.2):
            return


def trickle_headers(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Send the response head one byte at a time."""
    server.requests.append(support.read_request(connection))
    head = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 40 + b"\r\n\r\n"
    support.send_slowly(connection, head, server, 0.2)


def trickle_chunks(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Send one one-byte chunk at a time, slowly enough to outlive the deadline."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("200 OK", "Transfer-Encoding: chunked\r\n"))
    for _ in range(100):
        support.emit(connection, server, b"1\r\nx\r\n")
        if not server.wait(0.2):
            return


def late_eof(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Send a short unlengthened body and hold the connection open."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("200 OK"))
    support.emit(connection, server, b"q" * 32)
    server.wait(10.0)


def truncated(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Close the connection short of the declared Content-Length."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("200 OK", "Content-Length: 100\r\n"))
    support.emit(connection, server, b"t" * 20)


def _redirect_to_asset(
    connection: ssl.SSLSocket,
    server: LoopbackServer,
) -> bool:
    """Answer `/start` with a redirect to `/asset.bin`; report whether it did."""
    request = support.read_request(connection)
    server.requests.append(request)
    if request.path != "/start":
        return False
    connection.sendall(
        _head("302 Found", f"Location: {server.url}/asset.bin\r\n"),
    )
    return True


def https_redirect(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Redirect `/start` to a complete body at `/asset.bin`."""
    if _redirect_to_asset(connection, server):
        return
    connection.sendall(_body_response(FIXED_BODY))
    server.emitted += len(FIXED_BODY)


def redirect_overflow(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Redirect `/start` to an unlengthened body that overruns the ceiling."""
    if _redirect_to_asset(connection, server):
        return
    _stream_past_the_ceiling(connection, server)


def insecure_redirect(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Redirect to a plain-HTTP URL the downloader must refuse."""
    server.requests.append(support.read_request(connection))
    connection.sendall(
        _head("302 Found", f"Location: http://localhost:{server.port}/asset.bin\r\n"),
    )


def redirect_loop(connection: ssl.SSLSocket, server: LoopbackServer) -> None:
    """Redirect every request to the same path."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("302 Found", f"Location: {server.url}/loop\r\n"))


def redirect_without_location(
    connection: ssl.SSLSocket,
    server: LoopbackServer,
) -> None:
    """Return a redirect status with no Location header."""
    server.requests.append(support.read_request(connection))
    connection.sendall(_head("302 Found"))


#: Every scenario the integration tests can drive, by name.
SCENARIOS: dict[str, support.Handler] = {
    "fixed": fixed,
    "declared_oversize": declared_oversize,
    "stream_overflow": stream_overflow,
    "stall": stall,
    "trickle_body": trickle_body,
    "trickle_headers": trickle_headers,
    "trickle_chunks": trickle_chunks,
    "late_eof": late_eof,
    "truncated": truncated,
    "https_redirect": https_redirect,
    "redirect_overflow": redirect_overflow,
    "insecure_redirect": insecure_redirect,
    "redirect_loop": redirect_loop,
    "redirect_without_location": redirect_without_location,
}
