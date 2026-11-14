"""Run one bounded download against a loopback HTTPS server.

The integration test executes this module in an isolated Python 3.14
environment that installs the pinned `rust-prover-tools` revision, so it must
not import anything from the repository. One JSON request arrives on the
command line and one JSON result leaves on standard output.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from rust_prover_tools.download_transport import DownloadLimits
from rust_prover_tools.downloads import PinnedAsset, download_pinned


def _run(request: dict[str, object]) -> dict[str, object]:
    """Perform the requested download and describe its outcome."""
    # The downloader builds its TLS context from the default verify paths, so
    # the test CA is trusted here, in the isolated process, and nowhere else.
    os.environ["SSL_CERT_FILE"] = str(request["ca"])
    destination = Path(str(request["destination"]))
    asset = PinnedAsset(
        name=str(request["name"]),
        url=str(request["url"]),
        sha256=str(request["sha256"]),
        max_bytes=int(request["max_bytes"]),
    )
    limits = DownloadLimits(
        connect_timeout_seconds=float(request["connect_timeout_seconds"]),
        transfer_timeout_seconds=float(request["transfer_timeout_seconds"]),
        max_redirects=int(request["max_redirects"]),
    )
    started = time.monotonic()
    result: dict[str, object] = {"outcome": "ok"}
    try:
        download_pinned(asset, destination, limits)
    except Exception as exc:  # ruff: ignore[blind-except] - the test inspects the failure.
        result = {
            "outcome": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    result["elapsed_seconds"] = time.monotonic() - started
    result["destination_exists"] = destination.exists()
    result["destination_size"] = (
        destination.stat().st_size if destination.exists() else None
    )
    result["pending_exists"] = destination.with_name(
        f"{destination.name}.download"
    ).exists()
    return result


def main() -> int:
    """Run the request and print its result as one JSON object."""
    request = json.loads(sys.argv[1])
    print(json.dumps(_run(request)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
