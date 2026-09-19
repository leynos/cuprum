"""Benchmark workload identity for maintainer-facing summaries.

The throughput runner exposes three workloads that differ only in the
scenario matrix they select. Once a plan is read back, those scenarios are
indistinguishable in shape from another workload's, so a summary that names a
workload has to be told which one it is describing rather than inferring it
from the scenario names — a smoke matrix and the CI ratchet both carry
per-backend scenarios, and naming the wrong one misdescribes the measurement.

This module owns both halves of that answer: the identifiers the runner
records in a plan, and the protocol summary a report renders from them. They
live together because a description changed here must not be able to leave a
summary claiming a workload the plan did not record.
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

#: Rendered descriptions, keyed by workload identifier.
_DESCRIPTIONS: dict[str, str] = {
    THROUGHPUT_SWEEP_WORKLOAD: "the throughput sweep, covering three payload tiers",
    SMOKE_WORKLOAD: "the smoke workload, the sweep's shape at reduced payloads",
    CI_RATCHET_WORKLOAD: (
        "the CI-ratchet workload, one large payload measured at the ratchet's "
        "own worker-iteration count"
    ),
}


@dc.dataclass(frozen=True, slots=True)
class WorkloadProtocol:
    """Measurement protocol a benchmark plan describes.

    Parameters
    ----------
    workload : str
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
    """

    workload: str
    profile_version: str | None
    worker_iterations: int | None
    payload_bytes: tuple[int, ...]

    def describe(self) -> str:
        """Return a one-line summary of the workload and its protocol.

        Only the metadata the plan actually carried is named. A plan that
        omits a field is summarized without it rather than with a default,
        because a default here would state a measurement protocol as fact
        when nothing recorded it.

        Returns
        -------
        str
            The summary rendered into maintainer-facing report prose.
        """
        parts = [f"the {self.workload} workload"]
        if self.profile_version is not None:
            parts.append(f"profile {self.profile_version}")
        if self.payload_bytes:
            sizes = "/".join(
                f"{size / (1024 * 1024):.0f}" for size in self.payload_bytes
            )
            parts.append(
                f"payload {sizes} MiB"
                if len(self.payload_bytes) == 1
                else f"payloads {sizes} MiB"
            )
        if self.worker_iterations is not None:
            parts.append(f"{self.worker_iterations} worker iterations")
        return ", ".join(parts)

    def describe_workload(self) -> str:
        """Return the prose sentence naming this plan's workload."""
        return _DESCRIPTIONS[self.workload]


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
    workload = _require_non_empty_string(value, name=WORKLOAD_PLAN_KEY)
    if workload not in WORKLOADS:
        msg = (
            f"unknown benchmark workload {workload!r}; expected one of "
            f"{', '.join(WORKLOADS)}"
        )
        raise ValueError(msg)
    return typ.cast("WorkloadName", workload)


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
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "worker_iterations must be an int"
        raise TypeError(msg)
    if value < 1:
        msg = "worker_iterations must be >= 1"
        raise ValueError(msg)
    return value


def _payload_sizes(payload: cabc.Mapping[str, object]) -> tuple[int, ...]:
    """Return the distinct ascending payload sizes of a plan's scenarios."""
    scenarios = payload.get("scenarios")
    if scenarios is None:
        return ()
    if isinstance(scenarios, (str, bytes)) or not isinstance(scenarios, cabc.Sequence):
        msg = "scenarios must be a sequence"
        raise TypeError(msg)
    sizes: set[int] = set()
    for index, value in enumerate(scenarios):
        scenario = _require_mapping(value, name=f"scenarios[{index}]")
        size = scenario.get("payload_bytes")
        if size is None:
            continue
        if isinstance(size, bool) or not isinstance(size, int):
            msg = f"scenarios[{index}].payload_bytes must be an int"
            raise TypeError(msg)
        sizes.add(size)
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
