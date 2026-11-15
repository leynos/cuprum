"""Stage a bounded download beside its destination under a distinct name.

The transfer writes its body to a `.download` sibling and publishes that file
only after the digest matches. These cases pin the name the sibling carries, so
two assets that differ only in their final suffix cannot share one staging
path, and a verified cache entry is reused without being rewritten or restaged.
"""

from __future__ import annotations

import hashlib
import typing as typ

import pytest

from scripts import pypy_download as downloader
from scripts import pypy_download_metrics as metrics
from scripts.tests.boundary_pypy_transfer_support import (
    RecordingMetrics,
    fake_environment,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestPendingFile:
    """A refused transfer must not leave a partial archive behind."""

    def test_a_failed_transfer_removes_the_pending_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download that raises after writing leaves no pending or final file."""
        archive = tmp_path / "binary.tar.gz"
        pending = archive.with_name(f"{archive.name}.download")

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


class TestPendingName:
    """A staged download must not shadow its destination or a sibling asset."""

    def test_a_staged_name_appends_to_the_whole_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keeping every suffix stops two siblings sharing one staging path."""
        archive = tmp_path / "binary.tar.gz"
        staged: list[Path] = []

        def fetch(
            _url: str, destination: Path, _environment: downloader.Environment
        ) -> None:
            """Record the staging path, then fail the way a deadline would."""
            staged.append(destination)
            msg = "release download exceeded the transfer deadline"
            raise TimeoutError(msg)

        monkeypatch.setattr(downloader, "_download", fetch)
        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )
        assert staged == [tmp_path / "binary.tar.gz.download"], (
            "the staging path must append `.download` to the whole filename, so "
            "`binary.tar.gz` and `binary.tar.bz2` cannot collide"
        )

    def test_a_cache_hit_leaves_the_destination_intact(self, tmp_path: Path) -> None:
        """Reusing a verified archive neither rewrites nor restages it."""
        archive = tmp_path / "binary.tar.gz"
        body = b"cached body"
        archive.write_bytes(body)
        recorder = RecordingMetrics()
        downloader.checked_download(
            "https://example.invalid/asset",
            archive,
            hashlib.sha256(body).hexdigest(),
            environment=fake_environment(sink=recorder),
        )
        assert recorder.outcomes() == [metrics.CACHED], (
            "a download of bytes already in the cache must report a cache hit"
        )
        assert archive.read_bytes() == body, (
            "a cache hit must leave the verified archive byte-for-byte intact"
        )
        assert not (tmp_path / "binary.tar.gz.download").exists(), (
            "a cache hit must not stage a pending file"
        )
