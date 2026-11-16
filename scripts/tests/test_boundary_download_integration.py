"""Exercise the pinned downloader against controlled HTTPS failures.

These tests run the real `rust-prover-tools` revision named by the Makefile in
an isolated Python 3.14 environment. They are opt-in: installing that revision
needs the network and every scenario binds a loopback socket, so the default
suite collects this module but skips it. `make boundary-download-integration`
sets both variables the module requires.
"""

from __future__ import annotations

import dataclasses as dc
import os
from pathlib import Path

import pytest

from scripts.tests import boundary_download_runner as runner
from scripts.tests import boundary_download_scenarios as scenarios
from scripts.tests import boundary_download_support as support

RUN_ENV = "CUPRUM_RUN_DOWNLOAD_INTEGRATION"
SOURCE_ENV = "CUPRUM_PROVER_TOOLS_SOURCE"
MAX_BYTES = 64 * 1024
DESTINATION = Path("downloads", "asset.bin")

pytestmark = [
    pytest.mark.skipif(
        os.environ.get(RUN_ENV) != "1",
        reason=(
            f"set {RUN_ENV}=1 and {SOURCE_ENV} to the pinned revision, or run "
            "`make boundary-download-integration`"
        ),
    ),
    pytest.mark.timeout(600),
]


@dc.dataclass(frozen=True, slots=True)
class Refusal:
    """A redirect failure the downloader must report without writing a file."""

    case: runner.Case
    message: str
    requests: int


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> support.Certificates:
    """Create one test CA and server certificate for the whole module."""
    return support.create_certificates(tmp_path_factory.mktemp("certificates"))


@pytest.fixture(scope="module")
def source() -> str:
    """Return the pinned dependency revision the Make target supplied."""
    value = os.environ.get(SOURCE_ENV, "")
    if not value:
        pytest.fail(f"{SOURCE_ENV} must name the pinned rust-prover-tools revision")
    return value


def test_a_verified_download_is_published_atomically(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A pinned body is stored once, and no pending file survives."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(
            scenario="fixed",
            sha256=runner.digest(scenarios.FIXED_BODY),
        ),
    ) as run:
        assert run.outcome == "ok", run.message
        assert run.destination.read_bytes() == scenarios.FIXED_BODY, (
            "the verified body must be published unchanged"
        )
        assert run.result["destination_size"] == len(scenarios.FIXED_BODY), (
            "the published file must hold exactly the verified body"
        )
        assert not run.pending.exists(), "the pending file must not survive publication"
        assert [request.path for request in run.server.requests] == ["/asset.bin"], (
            "a verified download must make exactly one request"
        )
        assert not run.server.errors, "the test server must not record an error"


def test_a_declared_oversize_body_is_rejected_before_it_is_read(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """An oversized Content-Length fails before a byte reaches the disk."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(scenario="declared_oversize", sha256="0" * 64),
    ) as run:
        expected = (
            f"Content-Length {scenarios.OVERSIZE_CONTENT_LENGTH} "
            f"exceeds the {runner.DEFAULT_MAX_BYTES}-byte limit"
        )
        assert run.outcome == "error", run.message
        assert expected in run.message, (
            "the declared length must be rejected against the asset ceiling"
        )
        assert run.server.emitted == 0, "no body byte may be sent after the rejection"
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must not be created"


def test_a_streamed_overflow_stops_at_the_byte_ceiling(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """An unlengthened body is abandoned once it passes the ceiling."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(
            scenario="stream_overflow",
            sha256="0" * 64,
            max_bytes=MAX_BYTES,
        ),
    ) as run:
        assert run.outcome == "error", run.message
        assert f"streamed size exceeded the {MAX_BYTES}-byte limit" in run.message, (
            "an unlengthened body must be stopped by the streamed ceiling"
        )
        assert run.server.emitted > MAX_BYTES, (
            "the server must exceed the ceiling, so the streamed limit stops it"
        )
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must be discarded"
        assert not list(run.destination.parent.iterdir()), (
            "no partial file may survive the abandoned stream"
        )


def test_a_redirect_to_an_oversized_body_stops_at_the_byte_ceiling(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A redirect target that overruns the ceiling leaves no file behind."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(
            scenario="redirect_overflow",
            sha256="0" * 64,
            max_bytes=MAX_BYTES,
            path="/start",
        ),
    ) as run:
        assert run.outcome == "error", run.message
        assert f"streamed size exceeded the {MAX_BYTES}-byte limit" in run.message, (
            "the redirected body must be stopped by the streamed ceiling"
        )
        assert [request.path for request in run.server.requests] == [
            "/start",
            "/asset.bin",
        ], "the downloader must follow the redirect before the ceiling stops it"
        assert run.server.emitted > MAX_BYTES, (
            "the redirected server must exceed the ceiling, so the streamed limit "
            "stops it"
        )
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must be discarded"
        assert not list(run.destination.parent.iterdir()), (
            "no partial file may survive the abandoned redirect target"
        )


@pytest.mark.parametrize(
    "scenario",
    ["stall", "trickle_body", "trickle_headers", "trickle_chunks", "late_eof"],
)
def test_a_slow_response_cannot_outlive_the_transfer_deadline(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
    scenario: str,
) -> None:
    """A stalled, trickling, or never-ending body fails at the deadline."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(scenario=scenario, sha256="0" * 64, max_bytes=2_000_000),
    ) as run:
        low = runner.DEFAULT_DEADLINE_SECONDS * 0.8
        high = runner.DEFAULT_DEADLINE_SECONDS + 5.0
        assert run.outcome == "error", scenario
        assert "exceeded the transfer deadline" in run.message, scenario
        assert low <= run.elapsed < high, (
            f"{scenario} did not fail near the deadline: {run.elapsed:.2f}s"
        )
        assert not run.destination.exists(), scenario
        assert not run.pending.exists(), scenario


def test_a_truncated_body_is_rejected(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A body shorter than its declared length never reaches the final path."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(scenario="truncated", sha256="0" * 64),
    ) as run:
        assert run.outcome == "error", run.message
        assert "incomplete download of asset.bin" in run.message, (
            "a short body must surface as an incomplete download"
        )
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must be discarded"


def test_a_digest_mismatch_never_publishes_the_asset(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A complete body with the wrong digest is discarded."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(scenario="fixed", sha256="0" * 64),
    ) as run:
        assert run.outcome == "error", run.message
        assert "SHA-256 mismatch for asset.bin" in run.message, (
            "the digest check must name the rejected asset"
        )
        assert run.result["destination_exists"] is False, (
            "a mismatched body must not reach the destination"
        )
        assert run.result["pending_exists"] is False, (
            "a mismatched body must not leave a pending file"
        )
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must be discarded"


def test_an_https_redirect_is_followed(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A redirect that stays on HTTPS is followed and verified."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(
            scenario="https_redirect",
            sha256=runner.digest(scenarios.FIXED_BODY),
            path="/start",
        ),
    ) as run:
        assert run.outcome == "ok", run.message
        assert [request.path for request in run.server.requests] == [
            "/start",
            "/asset.bin",
        ], "the redirect must be followed exactly once"
        assert run.destination.read_bytes() == scenarios.FIXED_BODY, (
            "the redirected body must be verified and published"
        )


def test_an_insecure_redirect_is_refused_before_it_is_requested(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
) -> None:
    """A redirect to plain HTTP never reaches the network."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        runner.Case(scenario="insecure_redirect", sha256="0" * 64, path="/start"),
    ) as run:
        assert run.outcome == "error", run.message
        assert "must use https" in run.message, (
            "a plain-HTTP redirect target must be refused"
        )
        assert len(run.server.requests) == 1, (
            "the refused target must never be requested"
        )
        assert not run.destination.exists(), "the destination must not be created"


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            Refusal(
                runner.Case(
                    scenario="redirect_loop",
                    sha256="0" * 64,
                    max_redirects=2,
                    path="/start",
                ),
                "too many redirects",
                3,
            ),
            id="hop-limit",
        ),
        pytest.param(
            Refusal(
                runner.Case(
                    scenario="redirect_without_location",
                    sha256="0" * 64,
                    path="/start",
                ),
                "has no Location header",
                1,
            ),
            id="missing-location",
        ),
    ],
)
def test_a_refused_redirect_leaves_no_trace(
    tmp_path: Path,
    certificates: support.Certificates,
    source: str,
    refusal: Refusal,
) -> None:
    """A redirect loop or a targetless redirect fails without writing a file."""
    with runner.run_download(
        tmp_path / DESTINATION,
        certificates,
        source,
        refusal.case,
    ) as run:
        assert run.outcome == "error", run.message
        assert refusal.message in run.message, (
            f"the failure must report {refusal.message!r}"
        )
        assert len(run.server.requests) == refusal.requests, (
            f"the downloader must make exactly {refusal.requests} request(s)"
        )
        assert not run.destination.exists(), "the destination must not be created"
        assert not run.pending.exists(), "the pending file must be discarded"
