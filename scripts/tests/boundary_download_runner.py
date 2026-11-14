"""Drive the pinned downloader in an isolated environment, one run at a time.

The integration tests need a real `rust-prover-tools` revision, so the download
runs in a separate interpreter that only that revision is installed into. This
module owns that process boundary and the paths around it; the tests own the
scenarios and the assertions.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed uv and driver argv.
import typing as typ
from pathlib import Path

from scripts.tests import boundary_download_scenarios as scenarios
from scripts.tests import boundary_download_support as support

#: The driver runs one download and reports the outcome as JSON.
DRIVER = Path(__file__).with_name("boundary_download_driver.py")

#: How long a scenario may take end to end before the driver gives up.
DEFAULT_DEADLINE_SECONDS = 1.5

#: The ceiling the driver applies to a downloaded body by default.
DEFAULT_MAX_BYTES = 4096

_DRIVER_TIMEOUT_SECONDS = 300


@dc.dataclass(frozen=True, slots=True)
class Case:
    """The knobs one integration scenario sets on the shared downloader."""

    scenario: str
    sha256: str
    max_bytes: int = DEFAULT_MAX_BYTES
    deadline: float = DEFAULT_DEADLINE_SECONDS
    max_redirects: int = 3
    path: str = "/asset.bin"


@dc.dataclass(frozen=True, slots=True)
class DownloadRun:
    """One driver run with the server state and the paths it touched."""

    result: dict[str, typ.Any]
    server: support.LoopbackServer
    destination: Path
    pending: Path

    @property
    def outcome(self) -> str:
        """The driver's outcome, `ok` or `error`."""
        return str(self.result["outcome"])

    @property
    def message(self) -> str:
        """The failure message, empty after a successful run."""
        return str(self.result.get("message", ""))

    @property
    def elapsed(self) -> float:
        """The wall-clock duration the driver measured."""
        return float(self.result["elapsed_seconds"])

    def __enter__(self) -> DownloadRun:
        """Keep the loopback server running for the assertions that follow."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Stop the loopback server once the test has read its records."""
        self.server.close()


def digest(body: bytes) -> str:
    """Return the lowercase SHA-256 digest of `body`."""
    return hashlib.sha256(body).hexdigest()


def _require_uv() -> str:
    """Return the uv executable, or fail with a message the test shows."""
    uv = shutil.which("uv")
    if uv is None:
        msg = "the bounded-download integration tests require uv"
        raise RuntimeError(msg)
    return uv


def run_driver(request: dict[str, object], source: str) -> dict[str, typ.Any]:
    """Run the driver once in an isolated environment and parse its result."""
    command = [
        _require_uv(),
        "run",
        "--no-project",
        "--python",
        "3.14",
        "--with",
        source,
        "python",
        "-I",
        str(DRIVER),
        json.dumps(request),
    ]
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        command,
        capture_output=True,
        text=True,
        timeout=_DRIVER_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        msg = (
            "the isolated downloader failed to run:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        raise RuntimeError(msg)
    return typ.cast("dict[str, typ.Any]", json.loads(completed.stdout))


def run_download(
    destination: Path,
    certificates: support.Certificates,
    source: str,
    case: Case,
) -> DownloadRun:
    """Drive one scenario and return the result with the server's records."""
    server = support.LoopbackServer(certificates, scenarios.SCENARIOS[case.scenario])
    try:
        request = _driver_request(destination, server, case, certificates)
        result = run_driver(request, source)
    except BaseException:
        # `DownloadRun` is the only thing that closes the server, and it is not
        # built until both calls return. A failed setup must not leak the
        # listener or its accept thread into the rest of the session.
        server.close()
        raise
    return DownloadRun(
        result=result,
        server=server,
        destination=destination,
        pending=destination.with_name(f"{destination.name}.download"),
    )


def _driver_request(
    destination: Path,
    server: support.LoopbackServer,
    case: Case,
    certificates: support.Certificates,
) -> dict[str, object]:
    """Describe one download for the driver's JSON interface."""
    return {
        "name": destination.name,
        # The driver trusts this CA for its own connection only. Setting
        # `SSL_CERT_FILE` here would also apply it to uv's package fetch.
        "ca": str(certificates.ca),
        "url": f"{server.url}{case.path}",
        "sha256": case.sha256,
        "max_bytes": case.max_bytes,
        # Keep the per-receive timeout above the deadline so the deadline
        # watchdog, not a single stalled read, is what ends these scenarios.
        "connect_timeout_seconds": 5.0,
        "transfer_timeout_seconds": case.deadline,
        "max_redirects": case.max_redirects,
        "destination": str(destination),
    }
