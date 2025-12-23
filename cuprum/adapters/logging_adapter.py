"""Structured logging adapter for Cuprum execution events.

This module provides an observe hook that emits structured log records for
each execution event phase. Unlike the simpler ``logging_hook()`` which logs
only start/exit with before/after hooks, this adapter leverages the full
``ExecEvent`` stream for fine-grained observability.

The adapter demonstrates how to transform ``ExecEvent`` values into structured
log records suitable for log aggregation systems (ELK, Splunk, etc.).

Example::

    import logging
    from cuprum import scoped, sh
    from cuprum.adapters.logging_adapter import structured_logging_hook

    logging.basicConfig(level=logging.DEBUG)

    with scoped(allowlist=my_allowlist), sh.observe(structured_logging_hook()):
        sh.make(ECHO)("hello").run_sync()

"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import logging
import typing as typ

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

    """

    plan_level: int = logging.DEBUG
    start_level: int = logging.INFO
    output_level: int = logging.DEBUG
    exit_level: int = logging.INFO


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

    - ``cuprum_phase``: Event phase (plan, start, stdout, stderr, exit)
    - ``cuprum_program``: Programme being executed
    - ``cuprum_argv``: Full argument vector
    - ``cuprum_pid``: Process ID (when available)
    - ``cuprum_exit_code``: Exit code (for exit events)
    - ``cuprum_duration_s``: Duration in seconds (for exit events)
    - ``cuprum_tags``: Event tags as a dict

    """
    log = logger or logging.getLogger(_DEFAULT_LOGGER_NAME)
    lvls = levels or LogLevels()

    level_map: dict[str, int] = {
        "plan": lvls.plan_level,
        "start": lvls.start_level,
        "stdout": lvls.output_level,
        "stderr": lvls.output_level,
        "exit": lvls.exit_level,
    }

    def hook(event: ExecEvent) -> None:
        level = level_map.get(event.phase, logging.DEBUG)
        if not log.isEnabledFor(level):
            return

        extra = _build_extra(event)
        message = _format_message(event)
        log.log(level, message, extra=extra)

    return hook


def _build_extra(event: ExecEvent) -> dict[str, object]:
    """Build structured extra data for a log record."""
    extra: dict[str, object] = {
        "cuprum_phase": event.phase,
        "cuprum_program": str(event.program),
        "cuprum_argv": event.argv,
        "cuprum_tags": dict(event.tags),
    }
    if event.pid is not None:
        extra["cuprum_pid"] = event.pid
    if event.cwd is not None:
        extra["cuprum_cwd"] = str(event.cwd)
    if event.exit_code is not None:
        extra["cuprum_exit_code"] = event.exit_code
    if event.duration_s is not None:
        extra["cuprum_duration_s"] = event.duration_s
    if event.line is not None:
        extra["cuprum_line"] = event.line
    return extra


def _format_message(event: ExecEvent) -> str:
    """Format a human-readable log message for the event."""
    program = event.program
    match event.phase:
        case "plan":
            return f"cuprum.plan program={program} argv={event.argv!r}"
        case "start":
            return f"cuprum.start program={program} pid={event.pid}"
        case "stdout":
            return f"cuprum.stdout pid={event.pid} line={event.line!r}"
        case "stderr":
            return f"cuprum.stderr pid={event.pid} line={event.line!r}"
        case "exit":
            duration = (
                f"{event.duration_s:.6f}" if event.duration_s is not None else "unknown"
            )
            return (
                f"cuprum.exit program={program} pid={event.pid} "
                f"exit_code={event.exit_code} duration_s={duration}"
            )
        case _:
            return f"cuprum.{event.phase} program={program}"


class JsonLoggingFormatter(logging.Formatter):
    """A JSON formatter for structured log output.

    This formatter serialises log records as JSON objects, suitable for
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
        """Format the log record as a JSON string."""
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
    if isinstance(value, cabc.Mapping):
        return {str(k): _json_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_serializable(v) for v in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)


__all__ = [
    "JsonLoggingFormatter",
    "LogLevels",
    "structured_logging_hook",
]
