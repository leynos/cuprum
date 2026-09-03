"""Rolling window of main-branch benchmark samples for the ratchet.

The ratchet used to compare each pull request against the latest `main`
measurement. One sample makes its noise the bar's noise; publishing only
passing measurements also biases the bar towards the low tail of that noise.

This module holds the window that replaces it. The bar is the median of the
last `DEFAULT_WINDOW_SIZE` main-branch samples, which a single outlier cannot
move, and the noise band is estimated from those same samples rather than
guessed: a candidate must exceed both the configured flat threshold and
`DEFAULT_NOISE_SIGMAS` standard deviations of the observed spread before it
counts as a regression.

The median absolute deviation, not the standard deviation, estimates that
spread. The outlier this window exists to tolerate would inflate a standard
deviation — widening the band in proportion to the very sample it should
ignore — whereas it moves the median absolute deviation barely at all.

Samples are appended by every main-branch run, passing or failing, so the
window is not biased towards low-tail measurements.
"""

from __future__ import annotations

import dataclasses as dc
import json
import logging
import os
import pathlib as pth
import statistics
import tempfile
import typing as typ
from types import MappingProxyType

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_non_negative_float,
    _require_positive_float,
)
from benchmarks.benchmark_profile import require_worker_iterations
from benchmarks.errors import BenchmarkError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_logger = logging.getLogger(__name__)

#: How many recent main-branch runs the window keeps. Seven lets one bad runner
#: not dominate the median while admitting deliberate changes within a few merges.
DEFAULT_WINDOW_SIZE = 7

#: Estimated standard deviations of observed spread a candidate must exceed.
#: Three is a conventional outlier bound wider than the issue #219 runner swings.
DEFAULT_NOISE_SIGMAS = 3.0

#: Scale factor that makes the median absolute deviation a consistent
#: estimator of the standard deviation for normally distributed samples.
_MAD_TO_SIGMA = 1.4826

#: Widest band the observed spread may open. A window noisier than this is
#: telling you the benchmark cannot measure what it gates on, and a band that
#: kept widening with the noise would disable the ratchet silently rather
#: than say so. At the cap a candidate exactly twice the median passes; only
#: a candidate strictly slower than twice the median fails.
MAX_NOISE_TOLERANCE = 1.0

#: Schema version of the history payload, so a later shape change is rejected
#: rather than misread as an empty history and silently degrading the ratchet.
HISTORY_SCHEMA = 1

#: Fewest samples that can exhibit a spread. One measurement has none.
_MIN_SPREAD_SAMPLES = 2


class BaselineHistoryReadError(ValueError, BenchmarkError):
    """A persisted baseline-history payload could not be read or validated.

    Parameters
    ----------
    path : pathlib.Path | None
        History file involved in the failure, when one was being read.
    reason : str
        Stable explanation of the failed read or validation operation.
    """

    def __init__(self, reason: str, *, path: pth.Path | None = None) -> None:
        self.path = path
        self.reason = reason
        subject = f"baseline history {path}" if path is not None else "baseline history"
        super().__init__(f"{subject}: {reason}")


class BaselineHistoryNotFoundError(BaselineHistoryReadError):
    """The optional baseline-history file was not present."""

    def __init__(self, path: pth.Path) -> None:
        super().__init__("does not exist", path=path)


@dc.dataclass(frozen=True, slots=True)
class RatchetPolicy:
    """Thresholds for how much slower than recent `main` a change may measure.

    The ratchet calls measurements beyond the combined thresholds regressions.
    """

    max_regression: float = 0.30
    noise_sigmas: float = DEFAULT_NOISE_SIGMAS
    window_size: int = DEFAULT_WINDOW_SIZE

    def __post_init__(self) -> None:
        """Reject thresholds that cannot describe a comparison.

        A NaN threshold compares false against everything, so validating it
        by ``< 0`` would accept it and then silently pass every candidate.

        Raises
        ------
        ValueError
            If a threshold is negative, non-finite, or the window is empty.
        """
        _require_non_negative_float(self.max_regression, name="max_regression")
        _require_non_negative_float(self.noise_sigmas, name="noise_sigmas")
        if self.window_size < 1:
            msg = f"window_size must be >= 1, got {self.window_size}"
            raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class HistorySample:
    """One main-branch run's within-run Rust/Python ratios.

    `commit` and `run_id` are provenance only: they make a surprising bar
    traceable to the run that set it. Comparisons never read them.
    """

    commit: str
    run_id: str
    benchmark_profile_version: str
    worker_iterations: int
    ratios: cabc.Mapping[str, float]

    def __post_init__(self) -> None:
        """Freeze a private copy so later caller mutation cannot change a sample."""
        object.__setattr__(self, "ratios", MappingProxyType(dict(self.ratios)))

    def __hash__(self) -> int:
        """Hash the immutable mapping by its stable items rather than its proxy."""
        return hash((
            self.commit,
            self.run_id,
            self.benchmark_profile_version,
            self.worker_iterations,
            tuple(sorted(self.ratios.items())),
        ))

    def as_dict(self) -> dict[str, object]:
        """Serialize the sample for JSON output."""
        return {
            "commit": self.commit,
            "run_id": self.run_id,
            "benchmark_profile_version": self.benchmark_profile_version,
            "worker_iterations": self.worker_iterations,
            "ratios": dict(sorted(self.ratios.items())),
        }


def _sample_from_payload(value: object, *, index: int) -> HistorySample:
    """Build one `HistorySample` from its JSON payload."""
    payload = _require_mapping(value, name=f"samples[{index}]")
    ratios_payload = _require_mapping(
        payload.get("ratios"), name=f"samples[{index}].ratios"
    )
    ratios = {
        _require_non_empty_string(name, name=f"samples[{index}].ratios key"): (
            _require_positive_float(ratio, name=f"samples[{index}].ratios[{name!r}]")
        )
        for name, ratio in ratios_payload.items()
    }
    return HistorySample(
        commit=_require_non_empty_string(
            payload.get("commit"), name=f"samples[{index}].commit"
        ),
        run_id=_require_non_empty_string(
            payload.get("run_id"), name=f"samples[{index}].run_id"
        ),
        benchmark_profile_version=_require_non_empty_string(
            payload.get("benchmark_profile_version"),
            name=f"samples[{index}].benchmark_profile_version",
        ),
        worker_iterations=require_worker_iterations(payload),
        ratios=ratios,
    )


@dc.dataclass(frozen=True, slots=True)
class BaselineHistory:
    """The last N main-branch samples, oldest first."""

    samples: tuple[HistorySample, ...] = ()

    @property
    def scenarios(self) -> frozenset[str]:
        """Scenarios the newest sample measured.

        The newest sample defines the expected shape: a scenario added last
        week should be compared as soon as it has samples, and one removed
        should stop being required even though older samples still carry it.
        """
        if not self.samples:
            return frozenset()
        return frozenset(self.samples[-1].ratios)

    def ratios_for(self, scenario_name: str) -> tuple[float, ...]:
        """Return every recorded ratio for one scenario, oldest first."""
        return tuple(
            sample.ratios[scenario_name]
            for sample in self.samples
            if scenario_name in sample.ratios
        )

    def compatible_with(
        self,
        *,
        benchmark_profile_version: str,
        worker_iterations: int,
    ) -> BaselineHistory:
        """Return the samples that are comparable with the current profile.

        Different sampling protocols produce different ratios for unchanged
        code, so a sample from an older profile is not a smaller amount of
        evidence — it is evidence about a different question.

        Returns
        -------
        BaselineHistory
            Only samples measured with the candidate's profile and iterations.
        """
        kept = tuple(
            sample
            for sample in self.samples
            if sample.benchmark_profile_version == benchmark_profile_version
            and sample.worker_iterations == worker_iterations
        )
        if len(kept) != len(self.samples):
            _logger.info(
                "pruned %d baseline sample(s) recorded under an older benchmark "
                "profile",
                len(self.samples) - len(kept),
            )
        return BaselineHistory(samples=kept)

    def appended(
        self,
        sample: HistorySample,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> BaselineHistory:
        """Return this history with *sample* added and pruned to the window."""
        if window_size < 1:
            msg = f"window_size must be >= 1, got {window_size}"
            raise ValueError(msg)
        return BaselineHistory(samples=(*self.samples, sample)[-window_size:])

    def as_dict(self) -> dict[str, object]:
        """Serialize the history for JSON output."""
        return {
            "schema": HISTORY_SCHEMA,
            "samples": [sample.as_dict() for sample in self.samples],
        }


def history_from_payload(payload: cabc.Mapping[str, object]) -> BaselineHistory:
    """Build a `BaselineHistory` from a recognized, validated JSON payload."""
    schema = payload.get("schema")
    if schema != HISTORY_SCHEMA:
        msg = f"baseline history schema must be {HISTORY_SCHEMA!r}, got {schema!r}"
        raise BaselineHistoryReadError(msg)
    samples = _require_list(payload.get("samples"), name="samples")
    return BaselineHistory(
        samples=tuple(
            _sample_from_payload(value, index=index)
            for index, value in enumerate(samples)
        )
    )


def _read_history_payload(path: pth.Path) -> object:
    """Read and JSON-decode a persisted history payload."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineHistoryNotFoundError(path) from exc
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not read: {exc}"
        raise BaselineHistoryReadError(msg, path=path) from exc


def _history_from_payload_at_path(
    *, payload: object, path: pth.Path
) -> BaselineHistory:
    """Validate a decoded history payload while retaining its source path."""
    if not isinstance(payload, dict):
        msg = "must contain a JSON object"
        raise BaselineHistoryReadError(msg, path=path)
    try:
        return history_from_payload(typ.cast("dict[str, object]", payload))
    except BaselineHistoryReadError as exc:
        raise BaselineHistoryReadError(exc.reason, path=path) from exc
    except (TypeError, ValueError) as exc:
        msg = f"invalid payload: {exc}"
        raise BaselineHistoryReadError(msg, path=path) from exc


def load_history(path: pth.Path) -> BaselineHistory:
    """Load history, distinguishing an absent file from unreadable content.

    Returns
    -------
    BaselineHistory
        The validated persisted history.

    Raises
    ------
    BaselineHistoryNotFoundError
        If ``path`` does not exist.
    BaselineHistoryReadError
        If the path cannot be read or its JSON payload is invalid.
    """  # ruff: ignore[docstring-extraneous-exception] - helpers raise the documented typed errors.
    payload = _read_history_payload(path)
    return _history_from_payload_at_path(payload=payload, path=path)


def write_history(*, history: BaselineHistory, output_path: pth.Path) -> None:
    """Atomically replace ``output_path`` with the serialized history payload."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pth.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = pth.Path(temporary.name)
            temporary.write(json.dumps(history.as_dict(), indent=2, sort_keys=True))
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def median_ratio(values: cabc.Sequence[float]) -> float:
    """Return the median of *values*, which must not be empty."""
    if not values:
        msg = "at least one baseline sample is required"
        raise ValueError(msg)
    return statistics.median(values)


def noise_tolerance(
    values: cabc.Sequence[float],
    *,
    sigmas: float = DEFAULT_NOISE_SIGMAS,
) -> float:
    """Return the observed noise band as a relative tolerance.

    Expressed relative to the median so it composes with the flat threshold:
    the effective bar is whichever of the two is wider. Fewer than two
    samples, or a window with no spread, yields zero — the flat threshold
    then decides alone, which is exactly the pre-window behaviour.

    Returns
    -------
    float
        The capped relative tolerance derived from the median deviation.
    """
    if len(values) < _MIN_SPREAD_SAMPLES:
        return 0.0
    median = median_ratio(values)
    if median <= 0.0:
        return 0.0
    deviations = [abs(value - median) for value in values]
    sigma = _MAD_TO_SIGMA * statistics.median(deviations)
    tolerance = sigmas * sigma / median
    if tolerance > MAX_NOISE_TOLERANCE:
        _logger.warning(
            "baseline window spread implies a %.2f noise band, capped at %.2f; "
            "the benchmark is too noisy to gate on at this scale",
            tolerance,
            MAX_NOISE_TOLERANCE,
        )
        return MAX_NOISE_TOLERANCE
    return tolerance
