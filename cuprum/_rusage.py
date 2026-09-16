"""Portable snapshots of child-process resource accounting.

The POSIX ``resource`` module reports aggregate accounting for all reaped
children, while ``wait4`` returns usage for the child it reaps. This module
isolates both optional boundaries, normalizing direct-child RSS to bytes and
leaving aggregate snapshots CPU-only.
"""

from __future__ import annotations

import dataclasses as dc
import os
import sys
import typing as typ

try:
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment] # resource is unavailable on Windows


@dc.dataclass(frozen=True, slots=True)
class _ChildRusageSnapshot:
    """Normalized POSIX child-resource snapshot."""

    max_rss_bytes: int
    user_cpu_seconds: float
    system_cpu_seconds: float


@dc.dataclass(frozen=True, slots=True)
class ChildResourceUsage:
    """Resource usage attributable to one completed child where available."""

    max_rss_bytes: int | None
    user_cpu_seconds: float
    system_cpu_seconds: float


class _Wait4Usage(typ.Protocol):
    """Attributes supplied by the resource record returned from ``wait4``."""

    @property
    def ru_maxrss(self) -> int:
        """Maximum RSS reported by the reaped child."""

    @property
    def ru_utime(self) -> float:
        """User CPU time reported by the reaped child."""

    @property
    def ru_stime(self) -> float:
        """System CPU time reported by the reaped child."""


def child_resource_measurement_available() -> bool:
    """Return whether this platform exposes child resource accounting."""
    return (
        resource is not None
        and hasattr(resource, "RUSAGE_CHILDREN")
        and hasattr(resource, "getrusage")
    )


def wait4_resource_measurement_available() -> bool:
    """Return whether this platform can reap one child with its usage."""
    return (
        (sys.platform.startswith("linux") or sys.platform == "darwin")
        and hasattr(os, "wait4")
        and hasattr(os, "waitstatus_to_exitcode")
    )


def resource_usage_from_wait4(usage: _Wait4Usage) -> ChildResourceUsage:
    """Normalize the child-specific resource usage returned by ``os.wait4``."""
    max_rss_bytes = int(usage.ru_maxrss)
    if sys.platform.startswith("linux"):
        max_rss_bytes *= 1024
    return ChildResourceUsage(
        max_rss_bytes=max(0, max_rss_bytes),
        user_cpu_seconds=max(0.0, float(usage.ru_utime)),
        system_cpu_seconds=max(0.0, float(usage.ru_stime)),
    )


def capture_child_rusage() -> _ChildRusageSnapshot | None:
    """Capture child resource accounting, or ``None`` when unavailable."""
    resource_module = resource
    if resource_module is None:
        return None
    if not child_resource_measurement_available():
        return None
    try:
        usage = resource_module.getrusage(resource_module.RUSAGE_CHILDREN)
    except OSError:
        return None
    max_rss_bytes = usage.ru_maxrss
    if sys.platform.startswith("linux"):
        max_rss_bytes *= 1024
    return _ChildRusageSnapshot(
        max_rss_bytes=max_rss_bytes,
        user_cpu_seconds=usage.ru_utime,
        system_cpu_seconds=usage.ru_stime,
    )


def child_rusage_delta(
    before: _ChildRusageSnapshot | None,
    after: _ChildRusageSnapshot | None,
) -> ChildResourceUsage | None:
    """Return CPU deltas without attributing a high-water RSS measurement."""
    if before is None or after is None:
        return None
    return ChildResourceUsage(
        # RUSAGE_CHILDREN.ru_maxrss is a high-water mark over all reaped
        # children, so subtracting snapshots cannot identify this command.
        max_rss_bytes=None,
        user_cpu_seconds=max(0.0, after.user_cpu_seconds - before.user_cpu_seconds),
        system_cpu_seconds=max(
            0.0,
            after.system_cpu_seconds - before.system_cpu_seconds,
        ),
    )
