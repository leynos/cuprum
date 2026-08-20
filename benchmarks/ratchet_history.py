"""Rolling window of main-branch benchmark samples for the ratchet.

The ratchet used to compare each pull request against the single most recent
`main` measurement. Two properties of that arrangement made a noisy run
poison every subsequent pull request:

- one sample is the whole estimate, so its noise is the bar's noise; and
- the sample was only published when its own run passed, and a run passes
  whenever it is *faster* than the bar. An anomalously fast measurement was
  therefore always accepted, while the corrective slower measurements that
  followed were rejected — a bar biased towards the low tail of the noise
  distribution, and sticky once it got there.

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

Samples are appended by every main-branch run, passing or failing. That is
the half of the fix the statistics cannot supply: a window fed only by
passing runs is a window of low-biased samples.
"""

from __future__ import annotations

import dataclasses as dc
import json
import logging
import statistics
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_non_negative_float,
    _require_positive_float,
)
from benchmarks.benchmark_profile import require_worker_iterations

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_logger = logging.getLogger(__name__)

#: How many recent main-branch runs the window keeps. Seven spans roughly a
#: week of merges at this repository's rate: long enough that one bad runner
#: cannot dominate the median, short enough that a deliberate performance
#: change reaches the bar within a few merges rather than being averaged away.
DEFAULT_WINDOW_SIZE = 7

#: How many estimated standard deviations of observed spread a candidate must
#: exceed before its regression counts. Three is the conventional outlier
#: bound; with the spread this benchmark exhibits it is also comfortably wider
#: than the runner-to-runner swings recorded in issue #219.
DEFAULT_NOISE_SIGMAS = 3.0

#: Scale factor that makes the median absolute deviation a consistent
#: estimator of the standard deviation for normally distributed samples.
_MAD_TO_SIGMA = 1.4826

#: Widest band the observed spread may open. A window noisier than this is
#: telling you the benchmark cannot measure what it gates on, and a band that
#: kept widening with the noise would disable the ratchet silently rather
#: than say so. At the cap a candidate twice the median still fails.
MAX_NOISE_TOLERANCE = 1.0

#: Schema version of the history payload, so a later shape change can be
#: detected rather than misread. An unrecognized schema is treated as no
#: history at all, which degrades to the previous single-sample behaviour.
HISTORY_SCHEMA = 1

#: Fewest samples that can exhibit a spread. One measurement has none.
_MIN_SPREAD_SAMPLES = 2


@dc.dataclass(frozen=True, slots=True)
class RatchetPolicy:
    """The thresholds a candidate is judged against.

    Grouped rather than passed separately because they are one decision:
    how much slower than recent `main` a change may measure before the
    ratchet calls it a regression. The defaults are the ones CI uses.
    """

    max_regression: float = 0.30
    noise_sigmas: float = DEFAULT_NOISE_SIGMAS
    window_size: int = DEFAULT_WINDOW_SIZE

    def __post_init__(self) -> None:
        """Reject thresholds that cannot describe a comparison.

        A NaN threshold compares false against everything, so validating it
        by ``< 0`` would accept it and then silently pass every candidate.
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
        """Return the scenarios the newest sample measured.

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
    """Build a `BaselineHistory` from its JSON payload.

    An unrecognized schema yields an empty history rather than an error: the
    ratchet then falls back to the single-sample baseline, which is a
    degraded bar but still a working one.
    """
    schema = payload.get("schema")
    if schema != HISTORY_SCHEMA:
        _logger.warning(
            "ignoring baseline history with unrecognized schema %r (expected %r)",
            schema,
            HISTORY_SCHEMA,
        )
        return BaselineHistory()
    samples = _require_list(payload.get("samples"), name="samples")
    return BaselineHistory(
        samples=tuple(
            _sample_from_payload(value, index=index)
            for index, value in enumerate(samples)
        )
    )


def load_history(path: pth.Path | None) -> BaselineHistory:
    """Load a history file, treating an absent or unreadable one as empty.

    A missing history is the ordinary state on the first run after this
    landed, and on any run whose predecessor's artefact has expired. Neither
    should fail the job.
    """
    if path is None or not path.is_file():
        return BaselineHistory()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("ignoring unreadable baseline history %s: %s", path, exc)
        return BaselineHistory()
    if not isinstance(payload, dict):
        _logger.warning("ignoring baseline history %s: not a JSON object", path)
        return BaselineHistory()
    return history_from_payload(typ.cast("dict[str, object]", payload))


def write_history(*, history: BaselineHistory, output_path: pth.Path) -> None:
    """Write the history payload to ``output_path``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(history.as_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
