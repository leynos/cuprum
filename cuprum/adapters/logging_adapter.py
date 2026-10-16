"""Structured logging adapter for Cuprum execution events.

This module provides an observe hook that emits structured log records for
each execution event phase. Unlike the simpler ``logging_hook()`` which logs
only start/exit with before/after hooks, this adapter leverages the full
``ExecEvent`` stream for fine-grained observability.

The adapter demonstrates how to transform ``ExecEvent`` values into structured
log records suitable for log aggregation systems (ELK, Splunk, etc.).

Example::

    import logging
    from cuprum import ScopeConfig, scoped, sh
    from cuprum.adapters.logging_adapter import structured_logging_hook

    logging.basicConfig(level=logging.DEBUG)

    with scoped(
        ScopeConfig(allowlist=my_allowlist)
    ), sh.observe(structured_logging_hook()):
        sh.make(ECHO)("hello").run_sync()

"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import logging
import typing as typ

from cuprum.adapters._logging_formatter import JsonLoggingFormatter
from cuprum.adapters._logging_projection import (
    # Re-exported under this module's name as well as imported for use here:
    # the adapter's recorded projection is part of what its tests and the
    # Skylos contract read from this path, so a move is not a rename.
    _build_extra,
    _format_message,
)

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent, ExecHook
    from cuprum.redaction import RedactionPolicy

_DEFAULT_LOGGER_NAME = "cuprum.exec"


@dataclasses.dataclass(slots=True)
class LogLevels:
    """Configuration for logging levels per execution event phase.

    Attributes
    ----------
    plan_level:
        Log level for ``plan`` events (intent to execute). Default DEBUG.
    start_level:
        Log level for ``start`` events (process spawned). Default INFO.
    output_level:
        Log level for ``stdout``/``stderr`` events. Default DEBUG.
    exit_level:
        Log level for ``exit`` events (process completed). Default INFO.
    fail_fast_level:
        Log level for ``pipeline_fail_fast`` events (a pipeline is being torn
        down because a non-final stage failed first). Default WARNING: unlike
        the other phases this reports an abnormal outcome, and it would
        otherwise fall through to the unhandled-phase default of DEBUG and be
        invisible in a normal configuration.
    group_teardown_level:
        Log level for ``group_teardown`` events (a teardown could not establish
        settlement). Default WARNING for the same reason as ``fail_fast_level``:
        unlike the other phases this reports an abnormal outcome, and it would
        otherwise fall through to the unhandled-phase default of DEBUG and be
        invisible in a normal configuration.
    stdio_error_level:
        Log level for ``stdio_error`` events (a standard-stream boundary
        failed). Default WARNING, for the same reason as ``fail_fast_level``:
        it reports a failure, and a record nobody retains is not a diagnostic.

    """

    plan_level: int = logging.DEBUG
    start_level: int = logging.INFO
    output_level: int = logging.DEBUG
    exit_level: int = logging.INFO
    fail_fast_level: int = logging.WARNING
    group_teardown_level: int = logging.WARNING
    stdio_error_level: int = logging.WARNING


class _StructuredLoggingHook:
    """Render execution events and pipeline-wait records through one logger."""

    __slots__ = ("_level_by_phase", "_logger", "_redaction")

    def __init__(
        self,
        logger: logging.Logger,
        levels: LogLevels,
        *,
        redaction: RedactionPolicy | None = None,
    ) -> None:
        """Initialize the adapter with its logger and level policy."""
        self._logger = logger
        self._redaction = redaction
        self._level_by_phase = {
            "plan": levels.plan_level,
            "start": levels.start_level,
            "stdout": levels.output_level,
            "stderr": levels.output_level,
            "exit": levels.exit_level,
            "settled": levels.exit_level,
            "pipeline_fail_fast": levels.fail_fast_level,
            "group_teardown": levels.group_teardown_level,
            "stdio_error": levels.stdio_error_level,
        }

    def __call__(self, event: ExecEvent) -> None:
        """Log ``event`` at the level configured for its phase."""
        level = self._level_for(event.phase)
        if not self._logger.isEnabledFor(level):
            return
        if self._redaction is not None:
            event = self._redaction.redact_event(event)
        self._logger.log(level, _format_message(event), extra=_build_extra(event))

    def report_pipeline_wait(
        self,
        message: str,
        args: tuple[object, ...],
        extra: cabc.Mapping[str, object],
    ) -> None:
        """Render one core pipeline-wait record at WARNING level."""
        if self._logger.isEnabledFor(logging.WARNING):
            self._logger.warning(message, *args, extra=dict(extra))

    def _level_for(self, phase: str) -> int:
        """Return the configured logging level for ``phase``."""
        return self._level_by_phase.get(phase, logging.DEBUG)


def structured_logging_hook(
    *,
    logger: logging.Logger | None = None,
    levels: LogLevels | None = None,
    redaction: RedactionPolicy | None = None,
) -> ExecHook:
    """Create an observe hook that logs execution events with structured data.

    Parameters
    ----------
    logger:
        Logger instance for event emission. Defaults to
        ``logging.getLogger("cuprum.exec")``.
    levels:
        Log level configuration for different event phases. Defaults to
        ``LogLevels()`` with standard levels.
    redaction:
        Optional projection policy applied before rendering messages and extras.
        Defaults to ``None``; execution inputs and raw events remain unchanged.

    Returns
    -------
    ExecHook
        A hook suitable for use with ``sh.observe()``.

    Notes
    -----
    This hook is synchronous and non-blocking. Log emission happens inline
    with event processing. For high-throughput scenarios, consider using an
    async handler or buffered logging configuration.

    The hook attaches structured ``extra`` data to log records including:

    - ``cuprum_phase``: Event phase (plan, start, stdout, stderr, stdin,
      stdin_error, timeout, teardown_error, capture_eof_grace_expired, exit,
      settled, pipeline_fail_fast)
    - ``cuprum_program``: Program being executed
    - ``cuprum_argv``: Full argument vector
    - ``cuprum_pid``: Process ID (when available)
    - ``cuprum_exit_code``: Exit code (for exit and pipeline_fail_fast events)
    - ``cuprum_terminal_outcome``: Bounded category on ``settled`` events
    - ``cuprum_duration_s``: Duration in seconds (for exit and
      pipeline_fail_fast events)
    - ``cuprum_stage_index`` / ``cuprum_stage_count``: Position of the failing
      stage and the pipeline width (for pipeline_fail_fast events)
    - ``cuprum_eof_grace_s`` / ``cuprum_pending_readers``: Fixed grace duration
      and pending-reader count (for capture_eof_grace_expired events)
    - ``cuprum_max_rss_bytes`` / ``cuprum_user_cpu_seconds`` /
      ``cuprum_system_cpu_seconds`` / ``cuprum_resource_usage_mode``: Child
      resource measurements and the mode naming their source. The mode is
      carried on every terminal exit event — ``wait4_child``,
      ``aggregate_cpu_delta``, or ``unavailable`` — while the three figures
      are present only where a source produced them

    The adapter projects selected execution fields into log extras; it does
    not emit the full tags mapping.

    """
    return _StructuredLoggingHook(
        logger or logging.getLogger(_DEFAULT_LOGGER_NAME),
        levels or LogLevels(),
        redaction=redaction,
    )


__all__ = [
    "JsonLoggingFormatter",
    "LogLevels",
    "structured_logging_hook",
]
