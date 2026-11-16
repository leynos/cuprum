"""Stage each bounded download in a file only that transfer can name.

The transfer writes its body to a staging sibling of the destination and
publishes it only after the digest matches. These cases pin that the staging
name belongs to one transfer, so two installers racing on one cache path cannot
truncate or remove each other's bytes, and that a later run discards only
staging files old enough to belong to a killed transfer.
"""

from __future__ import annotations

import hashlib
import os
import time
import typing as typ
from itertools import starmap

import pytest

from scripts import pypy_download as downloader
from scripts import pypy_download_metrics as metrics
from scripts.tests.boundary_pypy_transfer_support import (
    RecordingMetrics,
    fake_environment,
    staged_siblings,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_BODY = b"verified body"
_DIGEST = hashlib.sha256(_BODY).hexdigest()
#: Comfortably older than any threshold the staging sweep applies, so a run
#: that lists this sibling may remove it.
_STALE_AGE_SECONDS = 4000.0
_DEADLINE_MESSAGE = "release download exceeded the transfer deadline"


def _fetch_writing(
    record: list[Path], body: bytes, *, fail: bool
) -> cabc.Callable[..., None]:
    """Return a `_download` stand-in that records its staging path and writes.

    Returns
    -------
    cabc.Callable[..., None]
        A stand-in that appends its staging path to `record`, writes `body`, and
        raises once `fail` is set.
    """

    def fetch(_url: str, pending: Path, _environment: downloader.Environment) -> None:
        """Record the staging path, write `body`, and fail when asked to."""
        record.append(pending)
        pending.write_bytes(body)
        if fail:
            raise TimeoutError(_DEADLINE_MESSAGE)

    return fetch


def _is_staging_of(archive: Path, candidate: Path) -> bool:
    """Report whether `candidate` carries the staging name this module pins.

    `staged_siblings` looks for this shape on its own, so a test that asserts an
    empty sweep is only meaningful once it has shown the transfer staged a file
    matching it.

    Returns
    -------
    bool
        ``True`` only when `candidate` carries the destination name, a token,
        and the staging suffix.
    """
    return candidate.name.startswith(f"{archive.name}.") and candidate.name.endswith(
        ".download"
    )


def _staging_sibling(archive: Path, *, age_seconds: float) -> Path:
    """Create one staging sibling of `archive`, aged by `age_seconds`."""
    sibling = archive.with_name(f"{archive.name}.deadbeef.download")
    sibling.write_bytes(b"another transfer's bytes")
    when = time.time() - age_seconds
    os.utime(sibling, (when, when))
    return sibling


class TestStagingLifecycle:
    """One transfer's staging file is its own; leftovers expire by age."""

    def test_a_failed_transfer_removes_its_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download that raises after writing leaves no staging or final file."""
        archive = tmp_path / "binary.tar.gz"
        seen: list[Path] = []
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing(seen, b"partial body", fail=True)
        )

        with pytest.raises(TimeoutError, match="transfer deadline"):
            downloader.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )

        assert len(seen) == 1, "the transfer must stage exactly one file"
        assert _is_staging_of(archive, seen[0]), (
            "the staged name must have the shape the sweep and the cleanup use"
        )
        assert staged_siblings(archive) == [], "the staging file must be discarded"
        assert not archive.exists(), "a failed transfer must not publish an archive"

    def test_a_published_transfer_removes_its_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verified bytes replace the cache entry and nothing else remains."""
        archive = tmp_path / "binary.tar.gz"
        seen: list[Path] = []
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing(seen, _BODY, fail=False)
        )

        downloader.checked_download("https://example.invalid/asset", archive, _DIGEST)

        assert _is_staging_of(archive, seen[0]), (
            "the staged name must have the shape the cleanup removes"
        )
        assert archive.read_bytes() == _BODY, "the verified body must be published"
        assert staged_siblings(archive) == [], "no staging file may survive a publish"

    def test_a_cache_hit_does_not_remove_a_live_staging_file(
        self, tmp_path: Path
    ) -> None:
        """One process's cache hit must not delete another process's bytes."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(_BODY)
        sibling = _staging_sibling(archive, age_seconds=0.0)
        recorder = RecordingMetrics()

        downloader.checked_download(
            "https://example.invalid/asset",
            archive,
            _DIGEST,
            environment=fake_environment(sink=recorder),
        )

        assert recorder.outcomes() == [metrics.CACHED], (
            "the entry already matched the pin, so the download is a cache hit"
        )
        assert sibling.exists(), (
            "a cache hit must leave a staging file another transfer is writing"
        )

    def test_a_stale_staging_file_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staging file older than any transfer could live is debris."""
        archive = tmp_path / "binary.tar.gz"
        sibling = _staging_sibling(archive, age_seconds=_STALE_AGE_SECONDS)
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing([], _BODY, fail=False)
        )

        downloader.checked_download("https://example.invalid/asset", archive, _DIGEST)

        assert not sibling.exists(), "a killed transfer's staging file must be swept"

    def test_a_fresh_staging_file_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staging file young enough to be in use must survive the sweep."""
        archive = tmp_path / "binary.tar.gz"
        sibling = _staging_sibling(archive, age_seconds=0.0)
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing([], _BODY, fail=False)
        )

        downloader.checked_download("https://example.invalid/asset", archive, _DIGEST)

        assert sibling.read_bytes() == b"another transfer's bytes", (
            "the sweep must not touch staging files a live transfer may own"
        )

    def test_a_foreign_staging_file_is_never_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the transfer's own verified bytes reach the cache entry."""
        archive = tmp_path / "binary.tar.gz"
        sibling = _staging_sibling(archive, age_seconds=0.0)
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing([], _BODY, fail=False)
        )

        downloader.checked_download("https://example.invalid/asset", archive, _DIGEST)

        assert archive.read_bytes() == _BODY, (
            "the cache entry must hold the bytes this transfer verified"
        )
        assert sibling.read_bytes() == b"another transfer's bytes", (
            "a sibling's bytes must never be published as if this transfer "
            "had verified them"
        )


class TestStagingName:
    """A staging name must belong to one transfer and one asset."""

    def test_each_transfer_reserves_a_distinct_staging_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two transfers for one destination must never share a staging file."""
        archive = tmp_path / "binary.tar.gz"
        seen: list[Path] = []
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing(seen, b"partial body", fail=True)
        )

        for _ in range(2):
            with pytest.raises(TimeoutError, match="transfer deadline"):
                downloader.checked_download(
                    "https://example.invalid/asset", archive, "0" * 64
                )

        assert len(seen) == 2, "each call must stage its own file"
        assert seen[0] != seen[1], (
            "a shared staging name lets one transfer truncate or unlink another's "
            "bytes between that transfer's digest check and its publish"
        )

    def test_a_staged_name_keeps_every_destination_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keeping every suffix stops two sibling assets sharing one staging name."""
        archives = (tmp_path / "binary.tar.gz", tmp_path / "binary.tar.bz2")
        seen: list[Path] = []
        monkeypatch.setattr(
            downloader, "_download", _fetch_writing(seen, b"partial body", fail=True)
        )

        for archive in archives:
            with pytest.raises(TimeoutError, match="transfer deadline"):
                downloader.checked_download(
                    "https://example.invalid/asset", archive, "0" * 64
                )

        assert all(starmap(_is_staging_of, zip(archives, seen, strict=True))), (
            "the staging name must append to the whole filename rather than "
            "replace a suffix, so `binary.tar.gz` and `binary.tar.bz2` cannot "
            "collide"
        )

    def test_a_cache_hit_leaves_the_destination_intact(self, tmp_path: Path) -> None:
        """Reusing a verified archive neither rewrites nor restages it."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(_BODY)
        recorder = RecordingMetrics()
        downloader.checked_download(
            "https://example.invalid/asset",
            archive,
            _DIGEST,
            environment=fake_environment(sink=recorder),
        )
        assert recorder.outcomes() == [metrics.CACHED], (
            "a download of bytes already in the cache must report a cache hit"
        )
        assert archive.read_bytes() == _BODY, (
            "a cache hit must leave the verified archive byte-for-byte intact"
        )
        assert staged_siblings(archive) == [], "a cache hit must stage nothing"

    def test_a_cache_hit_keeps_a_destination_named_download(
        self, tmp_path: Path
    ) -> None:
        """A destination already ending in `.download` is not its own staging file."""
        archive = tmp_path / "asset.download"
        archive.write_bytes(_BODY)
        recorder = RecordingMetrics()
        downloader.checked_download(
            "https://example.invalid/asset",
            archive,
            _DIGEST,
            environment=fake_environment(sink=recorder),
        )
        assert recorder.outcomes() == [metrics.CACHED], (
            "the entry already matched the pin, so the download is a cache hit"
        )
        assert archive.read_bytes() == _BODY, (
            "the cleanup must not delete a cache entry whose name ends in "
            "`.download`, which a suffix-replacing staging name would have made "
            "the destination itself"
        )
