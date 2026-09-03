"""Rolling main-branch benchmark samples used by the ratchet.

The median resists one outlier. A MAD-derived noise band and the flat threshold
must both be exceeded before a candidate regresses; every completed main run
contributes a sample, avoiding low-tail bias.
"""

from __future__ import annotations

import dataclasses as dc
import json
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

#: Recent main-branch runs retained; seven resists one outlier.
DEFAULT_WINDOW_SIZE = 7

#: MAD-derived standard deviations a candidate must exceed.
DEFAULT_NOISE_SIGMAS = 3.0

#: MAD-to-standard-deviation scale for normally distributed samples.
_MAD_TO_SIGMA = 1.4826

#: Widest noise band; at the cap, only a candidate slower than twice the median fails.
MAX_NOISE_TOLERANCE = 1.0

#: Schema version; later shapes fail rather than silently becoming empty history.
HISTORY_SCHEMA = 1

#: Fewest samples that can exhibit a spread. One measurement has none.
_MIN_SPREAD_SAMPLES = 2


class BaselineHistoryReadError(ValueError, BenchmarkError):
    """A persisted baseline-history payload could not be read or validated.

    Parameters
    ----------
    reason : str
        Stable explanation of the failed read or validation operation.
    path : pathlib.Path | None
        History file involved in the failure, when one was being read.

    Attributes
    ----------
    reason : str
        Stable explanation supplied to the constructor.
    path : pathlib.Path | None
        File supplied to the constructor, if the failure involved one.
    """

    def __init__(self, reason: str, *, path: pth.Path | None = None) -> None:
        self.path = path
        self.reason = reason
        subject = f"baseline history {path}" if path is not None else "baseline history"
        super().__init__(f"{subject}: {reason}")


class BaselineHistoryNotFoundError(BaselineHistoryReadError):
    """The optional baseline-history file was not present.

    Parameters
    ----------
    path : pathlib.Path
        Missing history file, exposed through ``path``; ``reason`` is
        ``"does not exist"``.
    """

    def __init__(self, path: pth.Path) -> None:
        super().__init__("does not exist", path=path)


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
        Commit provenance, serialized unchanged; payload loading requires it.
    run_id : str
        Workflow-run provenance, serialized unchanged; payload loading requires it.
    benchmark_profile_version : str
        Benchmark protocol version; payload loading requires it for compatibility.
    worker_iterations : int
        Per-scenario worker count; payload loading requires a positive value.
    ratios : collections.abc.Mapping[str, float]
        Scenario ratios copied into an immutable mapping proxy; payload loading
        requires non-empty names and finite positive values.
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
    return min(tolerance, MAX_NOISE_TOLERANCE)
