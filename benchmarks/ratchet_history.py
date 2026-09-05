"""Pure rolling-window history and statistics for benchmark ratcheting.

The median resists one outlier. A MAD-derived noise band and the flat threshold
must both be exceeded before a candidate regresses. Persistence lives in
``benchmarks.ratchet_history_persistence`` so these domain values remain
independent of JSON and filesystems.
"""

from __future__ import annotations

import dataclasses as dc
import statistics
import typing as typ
from types import MappingProxyType

from benchmarks._validation import _require_non_negative_float

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Recent main-branch runs retained; seven resists one outlier.
DEFAULT_WINDOW_SIZE = 7

#: MAD-derived standard deviations a candidate must exceed.
DEFAULT_NOISE_SIGMAS = 3.0

#: MAD-to-standard-deviation scale for normally distributed samples.
_MAD_TO_SIGMA = 1.4826

#: Widest noise band; at the cap, only a candidate slower than twice the median fails.
MAX_NOISE_TOLERANCE = 1.0

#: Fewest samples that can exhibit a spread. One measurement has none.
_MIN_SPREAD_SAMPLES = 2


@dc.dataclass(frozen=True, slots=True)
class RatchetPolicy:
    """Thresholds for how much slower than recent `main` a change may measure.

    Attributes
    ----------
    max_regression : float
        Finite, non-negative flat fractional slowdown tolerated by the gate.
    noise_sigmas : float
        Finite, non-negative MAD-derived spread multiplier.
    window_size : int
        Positive count of recent compatible main samples retained in the bar.
    """

    max_regression: float = 0.30
    noise_sigmas: float = DEFAULT_NOISE_SIGMAS
    window_size: int = DEFAULT_WINDOW_SIZE

    def __post_init__(self) -> None:
        """Reject thresholds that cannot describe a comparison."""
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

    Attributes
    ----------
    commit : str
        Commit provenance supplied by the completed benchmark run.
    run_id : str
        Workflow-run provenance supplied by the completed benchmark run.
    benchmark_profile_version : str
        Benchmark protocol version used to select compatible samples.
    worker_iterations : int
        Per-scenario worker count used to select compatible samples.
    ratios : collections.abc.Mapping[str, float]
        Scenario ratios copied into an immutable mapping proxy.
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


@dc.dataclass(frozen=True, slots=True)
class BaselineHistory:
    """The last N main-branch samples, oldest first.

    Attributes
    ----------
    samples : tuple[HistorySample, ...]
        Immutable, ordered samples; appending retains only the configured window.
    """

    samples: tuple[HistorySample, ...] = ()

    @property
    def scenarios(self) -> frozenset[str]:
        """Scenarios the newest sample measured, defining the expected shape."""
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
        """Return samples comparable with the current sampling protocol.

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
    return min(tolerance, MAX_NOISE_TOLERANCE)
