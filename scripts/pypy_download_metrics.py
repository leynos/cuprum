"""Bounded-cardinality metrics and a timed span for the PyPy download.

The installer runs under ``uv run --no-project``, so this module imports only
the standard library. `LoggingMetrics` renders every sample as a structured
log record, which is the collection path the isolated bootstrap has: the
standard logging framework needs no third-party exporter. A caller that does
have a metrics backend may pass any object with ``inc_counter`` and
``observe_histogram`` — the shape of cuprum's own ``MetricsCollector`` —
without this module importing cuprum.

The labels carried by every metric are restricted to the fixed outcome
vocabulary below. URLs, filenames, and free-form error text never become
labels or span attributes, so a hostile server cannot inflate cardinality.
"""

from __future__ import annotations

import dataclasses
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

LOGGER = logging.getLogger("cuprum.pypy_download")

# The complete label vocabulary. Every download reports exactly one of these,
# so the number of label values cannot grow with server input.
CACHED = "cached"
SUCCESS = "success"
OVERSIZE = "oversize"
DEADLINE = "deadline"
CHECKSUM_MISMATCH = "checksum_mismatch"
TRANSFER_ERROR = "transfer_error"
REFUSED = "refused"

DOWNLOADS_TOTAL = "cuprum_pypy_download_outcome_total"
DOWNLOAD_BYTES = "cuprum_pypy_download_bytes"
DOWNLOAD_SECONDS = "cuprum_pypy_download_duration_seconds"


class MetricsSink(typ.Protocol):
    """The metric operations the download reports to.

    The names and signature match cuprum's ``MetricsCollector``, so its
    implementations satisfy this protocol without this module importing it.
    """

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Increment a counter metric."""

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record one histogram observation."""


class LoggingMetrics:
    """Render each metric sample as a structured log record."""

    def __init__(self, logger: logging.Logger = LOGGER) -> None:
        """Collect through `logger`, which defaults to this module's logger.

        Parameters
        ----------
        logger : logging.Logger, optional
            Destination for the samples. Supplying the logger a caller has
            already configured keeps the samples beside its own records.
        """
        self._logger = logger

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a counter sample through the logging collection path."""
        self._logger.debug(
            "pypy download counter",
            extra={"metric": name, "value": value, "labels": dict(labels)},
        )

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram sample through the logging collection path."""
        self._logger.debug(
            "pypy download observation",
            extra={"metric": name, "value": value, "labels": dict(labels)},
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Sample:
    """One download's measured byte count and elapsed time."""

    written: int
    seconds: float


def report_download(
    sink: MetricsSink,
    outcome: str,
    sample: Sample,
    *,
    logger: logging.Logger = LOGGER,
) -> None:
    """Report one finished download and close its timed span.

    Reporting is diagnostic, so a backend that raises is logged and discarded
    rather than propagated: it must neither fail a download whose archive was
    already verified and published, nor replace the transfer error that is on
    its way to the caller. The span record is written whatever the backend
    does, because it is the isolated bootstrap's only collection path.

    Parameters
    ----------
    sink : MetricsSink
        Backend that receives the counter and both histogram samples.
    outcome : str
        One value from the bounded outcome vocabulary defined here.
    sample : Sample
        Bytes the transfer wrote and the seconds it took, measured on a
        monotonic clock.
    logger : logging.Logger, optional
        Destination for the span record and the backend failure warning.
        Defaults to this module's logger, which is the collection path the
        isolated bootstrap has.
    """
    labels = {"outcome": outcome}
    try:
        sink.inc_counter(DOWNLOADS_TOTAL, 1.0, labels)
        sink.observe_histogram(DOWNLOAD_BYTES, float(sample.written), labels)
        sink.observe_histogram(DOWNLOAD_SECONDS, sample.seconds, labels)
    except Exception as exc:
        logger.warning(
            "pypy download metrics could not be recorded: %s",
            type(exc).__name__,
            exc_info=True,
        )
    logger.info(
        "pypy download finished: %s, %d bytes in %.3f s",
        outcome,
        sample.written,
        sample.seconds,
        extra={
            "span": "pypy_download",
            "outcome": outcome,
            "bytes": sample.written,
            "duration_seconds": sample.seconds,
        },
    )
