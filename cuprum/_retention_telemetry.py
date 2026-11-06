"""Bounded, structured telemetry for observe-hook task retention.

Retention decisions are only useful to an operator if they can be told apart
after the fact: "this run was slow" and "this run paused its child because its
observers could not keep up" look the same in a bare duration. This module owns
the recorder that distinguishes them. It holds counters only — the admission
timeline of one stage — and writes fixed-field log records; the rules the
counters describe live in :mod:`cuprum._observe_task_retention`.

Every field is a number, a closed phase name, or the execution's correlation
token. No command text, decoded line, or error message is recorded, so log
volume and label cardinality stay bounded no matter what a child prints.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum.events import ExecId

_LOGGER = logging.getLogger(__name__)


@dc.dataclass(slots=True)
class _RetentionTelemetry:
    """Bounded counters and structured logs for one execution's retention.

    One recorder belongs to each stage observation. The counters describe the
    admission decisions that stage made, while ``depth`` and ``max_depth`` read
    the shared retention list and therefore describe the run. Every field is a
    number, a closed phase name, or the execution's correlation token: no
    command text, decoded line, or error message is recorded, so the log
    volume and label cardinality stay bounded.

    Attributes
    ----------
    exec_id:
        Correlation token shared with the stage's execution events.
    depth:
        Unfinished observer tasks at the most recent observation.
    max_depth:
        Highest unfinished depth seen for this stage.
    admissions:
        Admission decisions this stage made.
    saturations:
        Admission decisions refused because the ceiling was reached.
    pruned:
        Settled tasks dropped by in-place pruning.
    pruned_failures:
        Failed or cancelled tasks dropped because an earlier failure is kept.
    waits:
        Capacity waits that suspended the line read loop.
    wait_seconds:
        Total monotonic time spent suspended in capacity waits.

    """

    exec_id: ExecId
    depth: int = 0
    max_depth: int = 0
    admissions: int = 0
    saturations: int = 0
    pruned: int = 0
    pruned_failures: int = 0
    waits: int = 0
    wait_seconds: float = 0.0

    def observe_depth(self, pending_tasks: list[asyncio.Task[None]]) -> int:
        """Record the current unfinished-task depth and return it."""
        unfinished = sum(1 for task in pending_tasks if not task.done())
        self.depth = unfinished
        self.max_depth = max(self.max_depth, unfinished)
        return unfinished

    def record_admission(
        self,
        *,
        admitted: bool,
        ceiling: int,
        phase: str | None,
    ) -> None:
        """Record one admission decision at debug level."""
        self.admissions += 1
        if not admitted:
            self.saturations += 1
        _LOGGER.debug(
            "observe_task_admission admitted=%s phase=%s ceiling=%s depth=%s "
            "max_depth=%s admissions=%s saturations=%s",
            admitted,
            phase,
            ceiling,
            self.depth,
            self.max_depth,
            self.admissions,
            self.saturations,
            extra=self._fields(phase=phase, ceiling=ceiling, admitted=admitted),
        )

    def record_prune(
        self,
        *,
        pruned: int,
        pruned_failures: int,
        ceiling: int,
        phase: str | None,
    ) -> None:
        """Record one in-place prune of the shared retention list."""
        self.pruned += pruned
        self.pruned_failures += pruned_failures
        _LOGGER.debug(
            "observe_task_pruned pruned=%s pruned_failures=%s phase=%s "
            "ceiling=%s depth=%s max_depth=%s",
            pruned,
            pruned_failures,
            phase,
            ceiling,
            self.depth,
            self.max_depth,
            extra=self._fields(
                phase=phase,
                ceiling=ceiling,
                prune_delta=pruned,
                prune_failures_delta=pruned_failures,
            ),
        )

    def log_capacity_wait(self, *, ceiling: int, phase: str | None) -> None:
        """Log the transition into observer-task backpressure."""
        _LOGGER.info(
            "observe_task_capacity_wait phase=%s ceiling=%s depth=%s max_depth=%s",
            phase,
            ceiling,
            self.depth,
            self.max_depth,
            extra=self._fields(phase=phase, ceiling=ceiling),
        )

    def record_wait(
        self,
        *,
        elapsed: float,
        ceiling: int,
        phase: str | None,
    ) -> None:
        """Record one completed capacity wait and its duration."""
        self.waits += 1
        self.wait_seconds += elapsed
        _LOGGER.debug(
            "observe_task_capacity_resumed phase=%s ceiling=%s elapsed_s=%.6f "
            "waits=%s wait_seconds=%.6f",
            phase,
            ceiling,
            elapsed,
            self.waits,
            self.wait_seconds,
            extra=self._fields(
                phase=phase,
                ceiling=ceiling,
                elapsed_s=elapsed,
            ),
        )

    def _fields(
        self,
        *,
        phase: str | None,
        ceiling: int,
        **extra: object,
    ) -> dict[str, object]:
        """Build the fixed structured-log fields shared by every record.

        Callers pass the per-record values by their bare names; they are
        prefixed here so every field this module logs shares one namespace and
        a consumer can select them without colliding with ``LogRecord``'s own
        attributes. The prefix also keeps a per-prune delta from overwriting the
        running total it belongs to.

        Returns
        -------
        dict[str, object]
            The fixed fields plus the prefixed per-record values.
        """
        fields: dict[str, object] = {
            "cuprum_exec_id": str(self.exec_id),
            "cuprum_phase": phase,
            "cuprum_ceiling": ceiling,
            "cuprum_depth": self.depth,
            "cuprum_max_depth": self.max_depth,
            "cuprum_admissions": self.admissions,
            "cuprum_saturations": self.saturations,
            "cuprum_pruned": self.pruned,
            "cuprum_pruned_failures": self.pruned_failures,
            "cuprum_waits": self.waits,
            "cuprum_wait_seconds": self.wait_seconds,
        }
        fields.update({f"cuprum_{key}": value for key, value in extra.items()})
        return fields


__all__ = ["_RetentionTelemetry"]
