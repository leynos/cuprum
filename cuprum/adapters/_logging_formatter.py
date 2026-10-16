"""JSON rendering for structured execution log records."""

from __future__ import annotations

import collections.abc as cabc
import json
import logging


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
