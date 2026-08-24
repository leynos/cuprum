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
import json
import logging
import typing as typ

from cuprum.adapters._support import _event_common_fields, _prefixed

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent, ExecHook

_DEFAULT_LOGGER_NAME = "cuprum.exec"


@dataclasses.dataclass
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

    """

    plan_level: int = logging.DEBUG
    start_level: int = logging.INFO
    output_level: int = logging.DEBUG
    exit_level: int = logging.INFO
    fail_fast_level: int = logging.WARNING

class _StructuredLoggingHook:
    """Render execution events and pipeline-wait records through one logger."""

    __slots__ = ("_levels", "_logger")

    def __init__(self, logger: logging.Logger, levels: LogLevels) -> None:
        """Initialize the adapter with its logger and level policy."""
        self._logger = logger
        self._levels = levels

    def __call__(self, event: ExecEvent) -> None:
        """Log ``event`` at the level configured for its phase."""
        level = self._level_for(event.phase)
        if not self._logger.isEnabledFor(level):
            return
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
        return {
            "plan": self._levels.plan_level,
            "start": self._levels.start_level,
            "stdout": self._levels.output_level,
            "stderr": self._levels.output_level,
            "exit": self._levels.exit_level,
            "pipeline_fail_fast": self._levels.fail_fast_level,
        }.get(phase, logging.DEBUG)
def structured_logging_hook(
    *,
    logger: logging.Logger | None = None,
    levels: LogLevels | None = None,
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
      stdin_error, exit, pipeline_fail_fast)
    - ``cuprum_program``: Program being executed
    - ``cuprum_argv``: Full argument vector
    - ``cuprum_pid``: Process ID (when available)
    - ``cuprum_exit_code``: Exit code (for exit and pipeline_fail_fast events)
    - ``cuprum_duration_s``: Duration in seconds (for exit and
      pipeline_fail_fast events)
    - ``cuprum_stage_index`` / ``cuprum_stage_count``: Position of the failing
      stage and the pipeline width (for pipeline_fail_fast events)

    The adapter projects selected execution fields into log extras; it does
    not emit the full tags mapping.

    """
    return _StructuredLoggingHook(
        logger or logging.getLogger(_DEFAULT_LOGGER_NAME),
        levels or LogLevels(),
    )


def _build_extra(event: ExecEvent) -> dict[str, object]:
    """Build structured extra data for a log record."""
    extra: dict[str, object] = {"cuprum_phase": event.phase}
    common_fields = _event_common_fields(event, _prefixed("cuprum_"))
    if event.phase == "pipeline_fail_fast":
        # Fail-fast is warning-level by default. Its decision fields are enough
        # to diagnose a teardown, so do not elevate arbitrary command arguments
        # or caller tags into a channel operators commonly retain.
        extra.update(
            (name, value) for name, value in common_fields if name != "cuprum_argv"
        )
    else:
        extra.update(common_fields)
        extra["cuprum_tags"] = dict(event.tags)
    return extra


def _format_duration(duration_s: float | None) -> str:
    """Render an elapsed time for a log message, or ``unknown`` when absent."""
    return "unknown" if duration_s is None else f"{duration_s:.6f}"


def _format_exit_message(event: ExecEvent) -> str:
    """Render the message for a completed subprocess."""
    return (
        f"cuprum.exit program={event.program} pid={event.pid} "
        f"exit_code={event.exit_code} "
        f"duration_s={_format_duration(event.duration_s)}"
    )


def _format_fail_fast_message(event: ExecEvent) -> str:
    """Render the message for a pipeline's fail-fast decision.

    Rendered explicitly because the generic unhandled-phase message reports
    only the program, which is the one part of this event that says nothing:
    the point of the record is which stage of how many failed, and how.

    Returns
    -------
    str
        Human-readable fail-fast message containing the decision fields.
    """
    return (
        f"cuprum.pipeline_fail_fast program={event.program} "
        f"stage_index={event.stage_index} "
        f"stage_count={event.stage_count} "
        f"exit_code={event.exit_code} "
        f"duration_s={_format_duration(event.duration_s)}"
    )
def _format_message(event: ExecEvent) -> str:
    """Format a human-readable log message for the event."""
    program = event.program
    match event.phase:
        case "plan":
            return f"cuprum.plan program={program} argv={event.argv!r}"
        case "start":
            return f"cuprum.start program={program} pid={event.pid}"
        case "stdout" | "stderr":
            # One rendering for both: they differ only in the phase name.
            return f"cuprum.{event.phase} pid={event.pid} line={event.line!r}"
        case "exit":
            return _format_exit_message(event)
        case "pipeline_fail_fast":
            return _format_fail_fast_message(event)
        case _:
            return f"cuprum.{event.phase} program={program}"


class JsonLoggingFormatter(logging.Formatter):
    """A JSON formatter for structured log output.

    This formatter serializes log records as JSON objects, suitable for
    log aggregation systems. It includes all ``cuprum_*`` extra fields.

    Example::

        import logging
        from cuprum.adapters.logging_adapter import JsonLoggingFormatter

        handler = logging.StreamHandler()
        handler.setFormatter(JsonLoggingFormatter())
        logger = logging.getLogger("cuprum.exec")
        logger.addHandler(handler)

    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as a JSON string.

        Parameters
        ----------
        record : logging.LogRecord
            The log record to render as a JSON object.

        Returns
        -------
        str
            The record serialized as a JSON object, including
            ``cuprum_*`` extra fields.
        """
        output: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in vars(record).items():
            if key.startswith("cuprum_"):
                output[key] = _json_serializable(value)

        return json.dumps(output, default=str)


def _json_serializable(value: object) -> object:
    """Ensure a value is JSON-serializable."""
    match value:
        case cabc.Mapping():
            return {str(k): _json_serializable(v) for k, v in value.items()}
        case list() | tuple():
            return [_json_serializable(v) for v in value]
        case str() | int() | float() | bool() | None:
            return value
        case _:
            return str(value)


__all__ = [
    "JsonLoggingFormatter",
    "LogLevels",
    "structured_logging_hook",
]
