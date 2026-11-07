"""A controlled loopback HTTPS server for bounded-download integration tests.

The server speaks just enough HTTP/1.1 to drive one download per scenario. It
records the request paths and the number of body bytes it emitted, so a test
can show that the client stopped reading rather than merely reporting an
error. Certificates are generated with OpenSSL into a private directory and
trusted only by the isolated driver process through `SSL_CERT_FILE`.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import shutil
import socket
import ssl
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed OpenSSL argv.
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    type Handler = cabc.Callable[[ssl.SSLSocket, LoopbackServer], None]

_OPENSSL_TIMEOUT_SECONDS = 60
_CONNECTION_TIMEOUT_SECONDS = 5.0
_READ_LIMIT_BYTES = 64 * 1024
_HEAD_END = b"\r\n\r\n"
_SERVER_EXTENSIONS = (
    "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
    "basicConstraints=critical,CA:FALSE\n"
    "keyUsage=critical,digitalSignature,keyEncipherment\n"
    "extendedKeyUsage=serverAuth\n"
)


@dc.dataclass(frozen=True, slots=True)
class Certificates:
    """Paths to a test CA and a server certificate it signed."""

    ca: Path
    server_certificate: Path
    server_key: Path


@dc.dataclass(frozen=True, slots=True)
class Request:
    """One request head the server received."""

    path: str
    headers: dict[str, str]


def _openssl_binary() -> str:
    """Return the OpenSSL executable, or fail with a clear message."""
    binary = shutil.which("openssl")
    if binary is None:
        msg = "the bounded-download integration tests require the openssl binary"
        raise RuntimeError(msg)
    return binary


def _openssl(*arguments: str) -> None:
    """Run one fixed OpenSSL command, reporting its stderr on failure."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        (_openssl_binary(), *arguments),
        capture_output=True,
        text=True,
        timeout=_OPENSSL_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        msg = f"openssl {' '.join(arguments)} failed: {completed.stderr.strip()}"
        raise RuntimeError(msg)


def create_certificates(directory: Path) -> Certificates:
    """Create a private CA and a localhost server certificate below `directory`.

    Parameters
    ----------
    directory : Path
        Empty directory that receives the generated keys and certificates.

    Returns
    -------
    Certificates
        Paths the server and the isolated driver need.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ca_key, ca_certificate = _create_ca(directory)
    server_key, server_request = _create_server_request(directory)
    server_certificate = _sign_server_certificate(
        directory, (ca_key, ca_certificate), server_request
    )
    return Certificates(ca_certificate, server_certificate, server_key)


def _create_ca(directory: Path) -> tuple[Path, Path]:
    """Create the test CA's private key and self-signed certificate."""
    key = directory / "ca.key"
    certificate = directory / "ca.crt"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(certificate),
        "-days",
        "1",
        "-subj",
        "/CN=Cuprum Test CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    return key, certificate


def _create_server_request(directory: Path) -> tuple[Path, Path]:
    """Create the server's private key and certificate signing request."""
    key = directory / "server.key"
    request = directory / "server.csr"
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(request),
        "-subj",
        "/CN=localhost",
    )
    return key, request


def _sign_server_certificate(
    directory: Path,
    ca: tuple[Path, Path],
    request: Path,
) -> Path:
    """Sign the server request with the test CA and return its certificate."""
    extensions = directory / "server.ext"
    extensions.write_text(_SERVER_EXTENSIONS, encoding="utf-8")
    certificate = directory / "server.crt"
    ca_key, ca_certificate = ca
    _openssl(
        "x509",
        "-req",
        "-in",
        str(request),
        "-CA",
        str(ca_certificate),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(certificate),
        "-days",
        "1",
        "-extfile",
        str(extensions),
    )
    return certificate


class LoopbackServer:
    """A TLS server that runs one scenario handler per connection."""

    def __init__(self, certificates: Certificates, handler: Handler) -> None:
        """Bind a loopback listener and prepare the TLS context."""
        self._handler = handler
        self._stop = threading.Event()
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(
            str(certificates.server_certificate),
            str(certificates.server_key),
        )
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self.requests: list[Request] = []
        self.emitted = 0
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The HTTPS base URL for this server."""
        return f"https://localhost:{self.port}"

    def close(self) -> None:
        """Stop accepting and wake any handler waiting on the stop event."""
        self._stop.set()
        # `_serve` blocks in `accept()` with no timeout, and on Linux closing a
        # listening socket from another thread does not wake that call.
        # `shutdown()` does, so the accept thread returns instead of outliving
        # the test that owns it.
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=_CONNECTION_TIMEOUT_SECONDS + 1.0)

    def wait(self, seconds: float) -> bool:
        """Wait up to `seconds`, returning False when the server is closing."""
        return not self._stop.wait(seconds)

    def _serve(self) -> None:
        """Accept connections until the server is closed."""
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            try:
                connection = self._context.wrap_socket(raw, server_side=True)
            except (OSError, ssl.SSLError):
                raw.close()
                continue
            connection.settimeout(_CONNECTION_TIMEOUT_SECONDS)
            try:
                self._handler(connection, self)
            except OSError:
                # A client that stops reading closes the connection, which is
                # what the unbounded scenarios provoke; it is not a server
                # fault, so it is not recorded as one.
                return
            except Exception as exc:  # ruff: ignore[blind-except] - recorded for the test.
                self.errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                with contextlib.suppress(OSError):
                    connection.close()


def read_request(connection: ssl.SSLSocket) -> Request:
    """Read one request head and return its path and lower-cased headers."""
    data = bytearray()
    while _HEAD_END not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > _READ_LIMIT_BYTES:
            msg = "request head exceeded the test server's read limit"
            raise RuntimeError(msg)
    head = bytes(data).split(_HEAD_END, 1)[0].decode("iso-8859-1")
    return _parse_head(head)


def _parse_head(head: str) -> Request:
    """Parse a request head into its path and lower-cased headers."""
    lines = head.split("\r\n")
    parts = lines[0].split(" ") if lines else []
    path = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return Request(path, headers)


def send_slowly(
    connection: ssl.SSLSocket,
    data: bytes,
    server: LoopbackServer,
    delay: float,
) -> None:
    """Send `data` one byte at a time, stopping when the server closes."""
    for byte in data:
        connection.sendall(bytes((byte,)))
        if not server.wait(delay):
            return


def emit(connection: ssl.SSLSocket, server: LoopbackServer, data: bytes) -> None:
    """Send body bytes and count them as emitted."""
    connection.sendall(data)
    server.emitted += len(data)


def respond_with_body(
    connection: ssl.SSLSocket,
    server: LoopbackServer,
    body: bytes,
) -> None:
    """Send one complete, correctly sized 200 response for `body`."""
    head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii")
    connection.sendall(head + body)
    server.emitted += len(body)
