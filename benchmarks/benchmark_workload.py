"""Benchmark workload identity and the measurement protocol a plan records.

The throughput runner exposes three workloads that differ only in the
scenario matrix they select. Once a plan is read back, those scenarios are
indistinguishable in shape from another workload's, so a summary that names a
workload has to be told which one it is describing rather than inferring it
from the scenario names — a smoke matrix and the CI ratchet both carry
per-backend scenarios, and naming the wrong one misdescribes the measurement.

This module owns the identifiers the runner records in a plan and the validated
protocol read back from them. How that protocol is *rendered* is a separate
concern with a separate reason to change, and lives with the report that
renders it, in ``benchmarks.comparison_report``. Keeping the value object free
of prose means a change to report wording cannot reach the data a plan
recorded, and the object a formatter accepts is validated on construction
rather than merely annotated.
"""

from __future__ import annotations

# ``cabc`` is imported at runtime (not under ``TYPE_CHECKING``) because
# ``_payload_sizes`` performs an ``isinstance`` check against
# ``cabc.Sequence``, which a type-checking-only import cannot serve.
import collections.abc as cabc
import dataclasses as dc
import typing as typ

from benchmarks._validation import (
    _require_mapping,
    _require_non_empty_string,
)

__all__ = [
    "CI_RATCHET_WORKLOAD",
    "SMOKE_WORKLOAD",
    "THROUGHPUT_SWEEP_WORKLOAD",
    "WORKLOADS",
    "WORKLOAD_PLAN_KEY",
    "WorkloadName",
    "WorkloadProtocol",
    "read_workload",
    "read_workload_protocol",
]

#: Plan key recording which workload produced the plan's scenario matrix.
WORKLOAD_PLAN_KEY = "workload"

#: The default throughput sweep: three payload tiers at the sweep's own
#: worker-iteration count.
THROUGHPUT_SWEEP_WORKLOAD = "throughput-sweep"

#: The reduced-payload smoke matrix, for fast validation.
SMOKE_WORKLOAD = "smoke"

#: The single-payload matrix the CI ratchet compares between runs. It exists
#: because streaming work has to dominate the fixed per-run cost the ratchet
#: cancels; see ``pipeline_throughput_scenarios`` for where its payload came
#: from.
CI_RATCHET_WORKLOAD = "ci-ratchet"

#: Every workload a plan may declare.
WORKLOADS: tuple[str, ...] = (
    THROUGHPUT_SWEEP_WORKLOAD,
    SMOKE_WORKLOAD,
    CI_RATCHET_WORKLOAD,
)

type WorkloadName = typ.Literal["throughput-sweep", "smoke", "ci-ratchet"]


def _require_known_workload(value: object) -> WorkloadName:
    """Return *value* as a workload identifier the runner can produce."""
    workload = _require_non_empty_string(value, name=WORKLOAD_PLAN_KEY)
    if workload not in WORKLOADS:
        msg = (
            f"unknown benchmark workload {workload!r}; expected one of "
            f"{', '.join(WORKLOADS)}"
        )
        raise ValueError(msg)
    return typ.cast("WorkloadName", workload)


def _require_worker_iterations(value: object) -> int:
    """Return *value* as a positive worker iteration count."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "worker_iterations must be an int"
        raise TypeError(msg)
    if value < 1:
        msg = "worker_iterations must be >= 1"
        raise ValueError(msg)
    return value


def _require_payload_bytes(value: object) -> tuple[int, ...]:
    """Return *value* as distinct ascending integer payload sizes."""
    if not isinstance(value, tuple):
        msg = "payload_bytes must be a tuple"
        raise TypeError(msg)
    for size in value:
        if isinstance(size, bool) or not isinstance(size, int):
            msg = "payload_bytes must contain only ints"
            raise TypeError(msg)
    sizes = typ.cast("tuple[int, ...]", value)
    if sizes != tuple(sorted(set(sizes))):
        msg = "payload_bytes must be distinct and ascending"
        raise ValueError(msg)
    return sizes


@dc.dataclass(frozen=True, slots=True)
class WorkloadProtocol:
    """Measurement protocol a benchmark plan describes.

    Every field is validated on construction. The ratchet only compares
    samples whose profile metadata agrees, so a protocol value carrying a
    workload the runner cannot produce, or a payload list that is not the
    ascending distinct form a plan reads back as, would describe a measurement
    no run could have made. Rejecting those here means a formatter accepting a
    ``WorkloadProtocol`` cannot be handed one that describes nothing.

    Parameters
    ----------
    workload : WorkloadName
        Identifier of the workload that produced the plan's scenarios.
    profile_version : str | None
        The plan's ``benchmark_profile_version``, or ``None`` when the plan
        does not carry one.
    worker_iterations : int | None
        Pipeline executions batched inside one worker process, or ``None``
        when the plan does not carry the count.
    payload_bytes : tuple[int, ...]
        Payload sizes the plan's scenarios measure at, ascending and without
        duplicates. Empty when the plan carries no scenarios.

    Raises
    ------
    TypeError
        If ``workload`` is not a string, ``profile_version`` is present but
        not a string, ``worker_iterations`` is present but not an integer, or
        ``payload_bytes`` is not a tuple of integers.
    ValueError
        If ``workload`` is empty, whitespace-only, or unknown;
        ``profile_version`` is present but blank; ``worker_iterations`` is
        less than one; or ``payload_bytes`` is unsorted or carries a
        duplicate.

    Examples
    --------
    >>> WorkloadProtocol(
    ...     workload="ci-ratchet",
    ...     profile_version="profile-1",
    ...     worker_iterations=5,
    ...     payload_bytes=(1024, 4096),
    ... )
    WorkloadProtocol(workload='ci-ratchet', profile_version='profile-1', \
worker_iterations=5, payload_bytes=(1024, 4096))
    """

    workload: WorkloadName
    profile_version: str | None
    worker_iterations: int | None
    payload_bytes: tuple[int, ...]

    def __post_init__(self) -> None:
        """Validate the protocol a plan recorded before a report renders it."""
        _require_known_workload(self.workload)
        if self.profile_version is not None:
            _require_non_empty_string(self.profile_version, name="profile_version")
        if self.worker_iterations is not None:
            _require_worker_iterations(self.worker_iterations)
        _require_payload_bytes(self.payload_bytes)


def read_workload(payload: cabc.Mapping[str, object]) -> WorkloadName:
    """Return the validated workload identifier recorded in *payload*.

    Absence is not an error. Plans written before the runner recorded a
    workload describe the throughput sweep, which was the only workload then
    available, so defaulting keeps those artefacts readable.

    Parameters
    ----------
    payload : collections.abc.Mapping[str, object]
        A benchmark plan payload.

    Returns
    -------
    WorkloadName
        The recorded workload, or the throughput sweep when absent.

    Raises
    ------
    TypeError
        If the recorded workload is present but not a string.
    ValueError
        If the recorded workload is empty, whitespace-only, or unknown.
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError and ValueError are inherited from _require_non_empty_string
    value = payload.get(WORKLOAD_PLAN_KEY)
    if value is None:
        return typ.cast("WorkloadName", THROUGHPUT_SWEEP_WORKLOAD)
    return _require_known_workload(value)


def _optional_str(payload: cabc.Mapping[str, object], key: str) -> str | None:
    """Return *key* as a non-empty string, or None when it is absent."""
    value = payload.get(key)
    if value is None:
        return None
    return _require_non_empty_string(value, name=key)


def _optional_worker_iterations(payload: cabc.Mapping[str, object]) -> int | None:
    """Return the plan's worker iteration count, or None when it is absent."""
    value = payload.get("worker_iterations")
    if value is None:
        return None
    return _require_worker_iterations(value)


def _scenario_payload_size(index: int, value: object) -> int | None:
    """Return ``scenarios[index]``'s payload size, or None when it declares none.

    A scenario may legitimately omit the size, so absence is not an error; a
    present-but-wrong type is, and the message names the position because the
    plan's scenarios carry no other identifier a reader could act on.

    Parameters
    ----------
    index : int
        Position of the scenario, used in the error message.
    value : object
        The scenario entry to read.

    Returns
    -------
    int | None
        The declared payload size, or ``None`` when the scenario declares none.

    Raises
    ------
    TypeError
        If the scenario is not a mapping, or its size is not an int.
    """
    scenario = _require_mapping(value, name=f"scenarios[{index}]")
    size = scenario.get("payload_bytes")
    if size is None:
        return None
    if isinstance(size, bool) or not isinstance(size, int):
        msg = f"scenarios[{index}].payload_bytes must be an int"
        raise TypeError(msg)
    return size


def _payload_sizes(payload: cabc.Mapping[str, object]) -> tuple[int, ...]:
    """Return the distinct ascending payload sizes of a plan's scenarios."""
    scenarios = payload.get("scenarios")
    if scenarios is None:
        return ()
    if isinstance(scenarios, (str, bytes)) or not isinstance(scenarios, cabc.Sequence):
        msg = "scenarios must be a sequence"
        raise TypeError(msg)
    sizes = {
        size
        for index, value in enumerate(scenarios)
        if (size := _scenario_payload_size(index, value)) is not None
    }
    return tuple(sorted(sizes))


def read_workload_protocol(payload: cabc.Mapping[str, object]) -> WorkloadProtocol:
    """Return the workload and measurement protocol a plan describes.

    Parameters
    ----------
    payload : collections.abc.Mapping[str, object]
        A benchmark plan payload.

    Returns
    -------
    WorkloadProtocol
        The recorded workload with whatever protocol metadata the plan
        carried.

    Raises
    ------
    TypeError
        If a field is present but of the wrong type.
    ValueError
        If a field is present but empty or invalid.
    """  # ruff: ignore[docstring-extraneous-exception] - the validators propagate their contract errors
    return WorkloadProtocol(
        workload=read_workload(payload),
        profile_version=_optional_str(payload, "benchmark_profile_version"),
        worker_iterations=_optional_worker_iterations(payload),
        payload_bytes=_payload_sizes(payload),
    )
