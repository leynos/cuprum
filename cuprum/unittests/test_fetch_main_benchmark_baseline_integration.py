"""Integration coverage for the benchmark baseline HTTPS fetch workflow."""

from __future__ import annotations

import io
import ssl
import typing as typ
import zipfile

from benchmarks.fetch_main_benchmark_baseline import (
    ArtefactQuery,
    _download_bytes,
    extract_artefact_archive,
    find_latest_artefact_download_url,
)
from cuprum.unittests._https_test_support import local_https_baseline_server

if typ.TYPE_CHECKING:
    import pathlib as pth

    import pytest


def test_baseline_fetch_uses_authenticated_bounded_https_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pth.Path,
) -> None:
    """The baseline workflow retries, authenticates, and extracts a live archive."""
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w") as archive:
        archive.writestr("main-plan.json", '{"dry_run": true}')

    def _unverified_https_context(*_args: object, **_kwargs: object) -> ssl.SSLContext:
        """Trust the controlled server's test-only self-signed certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    monkeypatch.setattr(ssl, "_create_default_https_context", _unverified_https_context)
    delays: list[float] = []
    monkeypatch.setattr("benchmarks._github_http.time.sleep", delays.append)
    auth_value = "".join(("integ", "ration-", "token"))
    with local_https_baseline_server(
        tmp_path=tmp_path,
        archive_bytes=archive_buffer.getvalue(),
    ) as server:
        download_url = find_latest_artefact_download_url(
            query=ArtefactQuery(
                repository="leynos/cuprum",
                workflow="ci.yml",
                branch="main",
                event="push",
                artefact_name="benchmark-ratchet-main-baseline",
                api_base_url=server.api_base_url,
            ),
            token=auth_value,
        )
        assert download_url is not None
        extracted = extract_artefact_archive(
            archive_bytes=_download_bytes(url=download_url, token=auth_value),
            output_dir=tmp_path / "baseline",
        )

    assert delays == [0.5]
    assert len(server.requests) == 4
    assert all(
        request.authorization == f"Bearer {auth_value}" for request in server.requests
    )
    assert all(
        request.accept == "application/vnd.github+json" for request in server.requests
    )
    assert server.requests[0].path.endswith(
        "/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=20&status=success"
    )
    assert server.requests[2].path.endswith("/actions/runs/42/artifacts?per_page=100")
    assert extracted == (tmp_path / "baseline" / "main-plan.json",)
