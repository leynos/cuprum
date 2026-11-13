"""The bounded download must report collectable, bounded-cardinality metrics.

The bootstrap runs without the project environment, so its default collection
path is the standard logging framework. These cases drive both that path and an
injected recording sink, and prove that no URL, filename, or free-form error
text can reach a label or span attribute.
"""

from __future__ import annotations

import hashlib
import logging
import typing as typ
from unittest import mock

import pytest

from scripts import pypy_download as downloader
from scripts import pypy_download_metrics as metrics
from scripts.tests.boundary_pypy_transfer_support import streaming_response

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_OUTCOMES = frozenset({
    metrics.CACHED,
    metrics.SUCCESS,
    metrics.OVERSIZE,
    metrics.DEADLINE,
    metrics.CHECKSUM_MISMATCH,
    metrics.TRANSFER_ERROR,
    metrics.REFUSED,
})


class _Recorder:
    """Collect every metric sample a download reports."""

    def __init__(self) -> None:
        """Start with no samples."""
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def inc_counter(
        self, name: str, value: float, labels: cabc.Mapping[str, str]
    ) -> None:
        """Record one counter sample."""
        self.counters.append((name, value, dict(labels)))

    def observe_histogram(
        self, name: str, value: float, labels: cabc.Mapping[str, str]
    ) -> None:
        """Record one histogram sample."""
        self.histograms.append((name, value, dict(labels)))

    def outcomes(self) -> list[str]:
        """Return the outcome label of every counter sample."""
        return [labels["outcome"] for _, _, labels in self.counters]

    def observed(self, name: str) -> list[float]:
        """Return every value observed for `name`."""
        return [value for metric, value, _ in self.histograms if metric == name]


def _serve_once(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    """Serve `body` once as the response of the next download."""
    connection = mock.Mock()
    connection.getresponse.return_value = streaming_response(body)
    monkeypatch.setattr(
        downloader.http.client,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )


def test_a_successful_download_reports_bytes_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed transfer reports success and the bytes it wrote."""
    body = b"pypy archive bytes"
    _serve_once(monkeypatch, body)
    recorder = _Recorder()
    downloader.checked_download(
        "https://example.invalid/asset",
        tmp_path / "archive.tar.gz",
        hashlib.sha256(body).hexdigest(),
        metrics=recorder,
    )
    assert recorder.outcomes() == [metrics.SUCCESS], (
        "a completed transfer must report exactly one success outcome"
    )
    assert recorder.observed(metrics.DOWNLOAD_BYTES) == [pytest.approx(len(body))], (
        "the byte sample must equal the bytes the transfer wrote"
    )
    (duration,) = recorder.observed(metrics.DOWNLOAD_SECONDS)
    assert duration >= 0.0, "the duration must be a non-negative number of seconds"


def test_a_cache_hit_reports_the_cached_outcome(tmp_path: Path) -> None:
    """Reusing a matching cache entry reports the cached outcome and size."""
    body = b"cached archive"
    destination = tmp_path / "archive.tar.gz"
    destination.write_bytes(body)
    recorder = _Recorder()
    downloader.checked_download(
        "https://example.invalid/asset",
        destination,
        hashlib.sha256(body).hexdigest(),
        metrics=recorder,
    )
    assert recorder.outcomes() == [metrics.CACHED], (
        "a matching cache entry must report the cached outcome"
    )
    assert recorder.observed(metrics.DOWNLOAD_BYTES) == [pytest.approx(len(body))], (
        "a cache hit must still report the bytes it reused"
    )


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (downloader._OversizeError("too large"), metrics.OVERSIZE),
        (downloader._ChecksumMismatchError("mismatch"), metrics.CHECKSUM_MISMATCH),
        (TimeoutError("late"), metrics.DEADLINE),
        (ValueError("refused"), metrics.REFUSED),
        (OSError("reset"), metrics.TRANSFER_ERROR),
        # An exception the downloader does not anticipate is still a failed
        # transfer, never a success the vocabulary would have to lie about.
        (RuntimeError("unexpected"), metrics.TRANSFER_ERROR),
    ],
)
def test_a_failed_download_reports_a_bounded_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    outcome: str,
) -> None:
    """Every failure mode maps onto one value of the outcome vocabulary."""

    def fail(*_args: object, **_kwargs: object) -> None:
        """Fail the transfer with the case's error."""
        raise error

    monkeypatch.setattr(downloader, "_download", fail)
    recorder = _Recorder()
    with pytest.raises(type(error)):
        downloader.checked_download(
            "https://example.invalid/asset",
            tmp_path / "archive.tar.gz",
            "0" * 64,
            metrics=recorder,
        )
    assert recorder.outcomes() == [outcome], (
        "each failure mode must report its own bounded outcome"
    )


def test_metric_labels_carry_only_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No URL, filename, or error text may reach a metric label."""
    body = b"pypy archive bytes"
    destination = tmp_path / "secret-archive.tar.gz"
    _serve_once(monkeypatch, body)
    recorder = _Recorder()
    downloader.checked_download(
        "https://example.invalid/private/asset",
        destination,
        hashlib.sha256(body).hexdigest(),
        metrics=recorder,
    )
    samples = [labels for _, _, labels in recorder.counters + recorder.histograms]
    assert samples, "a completed download must report at least one sample"
    for sample in samples:
        assert set(sample) == {"outcome"}, (
            "no label beyond the outcome may reach a metric sample"
        )
        assert sample["outcome"] in _OUTCOMES, (
            "every outcome label must come from the fixed vocabulary"
        )
        values = " ".join(sample.values())
        assert "example.invalid" not in values, "the URL must not reach a metric label"
        assert destination.name not in values, (
            "the filename must not reach a metric label"
        )


def test_the_timed_span_logs_its_outcome_without_server_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The span's finish record names the outcome but never the URL or file."""
    caplog.set_level(logging.INFO, logger="cuprum.pypy_download")
    body = b"pypy archive bytes"
    destination = tmp_path / "secret-archive.tar.gz"
    _serve_once(monkeypatch, body)
    downloader.checked_download(
        "https://example.invalid/private/asset",
        destination,
        hashlib.sha256(body).hexdigest(),
        metrics=_Recorder(),
    )
    records = [r for r in caplog.records if r.name == "cuprum.pypy_download"]
    assert len(records) == 1, "one finish record must close the span"
    (record,) = records
    assert record.__dict__["span"] == "pypy_download", (
        "the finish record must name the download span"
    )
    assert record.__dict__["outcome"] == metrics.SUCCESS, (
        "the finish record must name the outcome"
    )
    assert record.__dict__["bytes"] == len(body), (
        "the finish record must report the bytes written"
    )
    assert record.__dict__["duration_seconds"] >= 0.0, (
        "the span must report a non-negative duration"
    )
    assert "example.invalid" not in record.getMessage(), (
        "the URL must not reach the span record"
    )
    assert destination.name not in record.getMessage(), (
        "the filename must not reach the span record"
    )
