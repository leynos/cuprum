"""Controlled HTTPS transport support for benchmark baseline integration tests."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import http.server
import json
import ssl
import subprocess  # ruff: ignore[suspicious-subprocess-import] - creates a short-lived self-signed certificate for a local integration server.
import threading
import typing as typ
import urllib.parse

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


@dc.dataclass(frozen=True, slots=True)
class _RecordedRequest:
    """A request received by the controlled baseline server."""

    path: str
    authorization: str | None
    accept: str | None


@dc.dataclass(slots=True)
class _BaselineServerState:
    """Mutable request state shared by a controlled baseline server."""

    archive_bytes: bytes
    requests: list[_RecordedRequest] = dc.field(default_factory=list)
    workflow_attempts: int = 0
    _lock: threading.Lock = dc.field(default_factory=threading.Lock, repr=False)

    def record_request(self, request: _RecordedRequest) -> None:
        """Append one request under the server-state lock."""
        with self._lock:
            self.requests.append(request)

    def is_initial_workflow_attempt(self) -> bool:
        """Allocate and report whether this is the first workflow request."""
        with self._lock:
            self.workflow_attempts += 1
            return self.workflow_attempts == 1


@dc.dataclass(frozen=True, slots=True)
class _HttpsBaselineServer:
    """Controlled GitHub Actions endpoint and its received requests."""

    api_base_url: str
    requests: list[_RecordedRequest]


def _create_self_signed_certificate(tmp_path: pth.Path) -> tuple[pth.Path, pth.Path]:
    """Create the localhost certificate required by the controlled HTTPS server."""
    certificate_path = tmp_path / "localhost-cert.pem"
    private_key_path = tmp_path / "localhost-key.pem"
    # The fixed executable operates only on this test's temporary certificate paths.
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key_path),
            "-out",
            str(certificate_path),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    )
    return certificate_path, private_key_path


@contextlib.contextmanager
def local_https_baseline_server(
    *,
    tmp_path: pth.Path,
    archive_bytes: bytes,
) -> cabc.Iterator[_HttpsBaselineServer]:
    """Run a controlled HTTPS GitHub Actions endpoint for one test.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Test-owned directory in which to create the temporary certificate.
    archive_bytes : bytes
        Archive response body served after the workflow and artefact requests.

    Yields
    ------
    _HttpsBaselineServer
        The HTTPS API base URL and record of received requests.
    """
    certificate_path, private_key_path = _create_self_signed_certificate(tmp_path)
    state = _BaselineServerState(archive_bytes=archive_bytes)

    class _RequestHandler(http.server.BaseHTTPRequestHandler):
        """Serve the small workflow, artefact, and archive fixture sequence."""

        def do_GET(self) -> None:
            """Record and respond to one authenticated baseline request."""
            state.record_request(
                _RecordedRequest(
                    path=self.path,
                    authorization=self.headers.get("Authorization"),
                    accept=self.headers.get("Accept"),
                )
            )
            path = urllib.parse.urlsplit(self.path).path
            if path.endswith("/actions/workflows/ci.yml/runs"):
                if state.is_initial_workflow_attempt():
                    self.send_error(http.HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self._send_json({"workflow_runs": [{"id": 42}]})
                return
            if path.endswith("/actions/runs/42/artifacts"):
                self._send_json({
                    "artifacts": [
                        {
                            "name": "benchmark-ratchet-main-baseline",
                            "expired": False,
                            "archive_download_url": (
                                f"https://{self.headers['Host']}/archives/42.zip"
                            ),
                        }
                    ]
                })
                return
            if path == "/archives/42.zip":
                self.send_response(http.HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(state.archive_bytes)))
                self.end_headers()
                self.wfile.write(state.archive_bytes)
                return
            self.send_error(http.HTTPStatus.NOT_FOUND)

        def log_message(
            self,
            format: str,  # ruff: ignore[builtin-argument-shadowing] - BaseHTTPRequestHandler override signature.
            *args: object,
        ) -> None:
            """Suppress expected local-server request logs during the test."""
            del format, args

        def _send_json(self, payload: dict[str, object]) -> None:
            """Write one JSON response with explicit byte framing."""
            body = json.dumps(payload).encode("utf-8")
            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, private_key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = typ.cast("tuple[str, int]", server.server_address)
    try:
        yield _HttpsBaselineServer(
            api_base_url=f"https://{host}:{port}",
            requests=state.requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
