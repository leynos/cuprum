"""Unit tests for the structured logging adapter."""

from __future__ import annotations

import logging
import typing as typ

if typ.TYPE_CHECKING:
    import pytest

from cuprum import sh
from cuprum.adapters.logging_adapter import (
    JsonLoggingFormatter,
    LogLevels,
    structured_logging_hook,
)
from cuprum.context import ScopeConfig, scoped
from cuprum.unittests._adapter_test_support import _python_builder


class TestStructuredLoggingHook:
    """Tests for structured_logging_hook."""

    @staticmethod
    def _assert_phase_logged(messages: list[str], phase: str) -> None:
        """Check if a phase appears in any message."""
        assert any(phase in m for m in messages), f"Expected {phase!r} in messages"

    @staticmethod
    def _assert_output_logged(messages: list[str], phase: str, output: str) -> None:
        """Check if a phase with specific output appears in any message."""
        assert any(phase in m and output in m for m in messages), (
            f"Expected {phase!r} with {output!r} in messages"
        )

    def test_logs_all_phases(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook logs plan, start, stdout, stderr, and exit events."""
        builder, catalogue = _python_builder(project_name="logging-test")
        cmd = builder(
            "-c",
            """import sys
print('out1')
print('err1', file=sys.stderr)""",
        )

        logger = logging.getLogger("cuprum.exec.test")
        logger.setLevel(logging.DEBUG)
        hook = structured_logging_hook(logger=logger)

        with (
            caplog.at_level(logging.DEBUG, logger="cuprum.exec.test"),
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(hook),
        ):
            result = cmd.run_sync()

        assert result.exit_code == 0, "logging hook command should exit cleanly"

        messages = [r.message for r in caplog.records]
        self._assert_phase_logged(messages, "cuprum.plan")
        self._assert_phase_logged(messages, "cuprum.start")
        self._assert_output_logged(messages, "cuprum.stdout", "out1")
        self._assert_output_logged(messages, "cuprum.stderr", "err1")
        self._assert_phase_logged(messages, "cuprum.exit")

    def test_includes_extra_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook attaches cuprum_* extra fields to log records."""
        builder, catalogue = _python_builder(project_name="extra-fields")
        cmd = builder("-c", "print('hello')")

        logger = logging.getLogger("cuprum.exec.extras")
        logger.setLevel(logging.DEBUG)
        hook = structured_logging_hook(logger=logger)

        with (
            caplog.at_level(logging.DEBUG, logger="cuprum.exec.extras"),
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(hook),
        ):
            cmd.run_sync()

        start_record = next(r for r in caplog.records if "cuprum.start" in r.message)
        assert hasattr(start_record, "cuprum_phase"), (
            "start log record should carry the cuprum phase"
        )
        assert start_record.cuprum_phase == "start", (
            "start log record should label the start phase"
        )
        assert hasattr(start_record, "cuprum_program"), (
            "start log record should carry the program"
        )
        assert hasattr(start_record, "cuprum_pid"), (
            "start log record should carry the process id"
        )

    def test_respects_log_levels(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook respects configured log levels for each phase."""
        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('x')")

        logger = logging.getLogger("cuprum.exec.levels")
        logger.setLevel(logging.INFO)
        levels = LogLevels(
            plan_level=logging.DEBUG,
            start_level=logging.INFO,
            output_level=logging.DEBUG,
            exit_level=logging.WARNING,
        )
        hook = structured_logging_hook(logger=logger, levels=levels)

        with (
            caplog.at_level(logging.INFO, logger="cuprum.exec.levels"),
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(hook),
        ):
            cmd.run_sync()

        messages = [r.message for r in caplog.records]
        assert not any("cuprum.plan" in m for m in messages), (
            "plan logs should stay below the configured capture level"
        )
        assert any("cuprum.start" in m for m in messages), (
            "start logs should be emitted at the configured level"
        )
        assert not any("cuprum.stdout" in m for m in messages), (
            "stdout logs should stay below the configured capture level"
        )
        assert any("cuprum.exit" in m for m in messages), (
            "exit logs should be emitted at the configured level"
        )


class TestJsonLoggingFormatter:
    """Tests for JsonLoggingFormatter."""

    def test_formats_as_json(self) -> None:
        """Formatter outputs valid JSON with structured fields."""
        import json

        formatter = JsonLoggingFormatter()
        record = logging.LogRecord(
            name="cuprum.exec",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="cuprum.start program=echo",
            args=(),
            exc_info=None,
        )
        record.cuprum_phase = "start"
        record.cuprum_program = "echo"
        record.cuprum_pid = 12345

        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO", "JSON formatter should include log level"
        assert data["logger"] == "cuprum.exec", (
            "JSON formatter should include logger name"
        )
        assert data["cuprum_phase"] == "start", (
            "JSON formatter should include cuprum phase"
        )
        assert data["cuprum_program"] == "echo", (
            "JSON formatter should include cuprum program"
        )
        assert data["cuprum_pid"] == 12345, (
            "JSON formatter should include cuprum process id"
        )
