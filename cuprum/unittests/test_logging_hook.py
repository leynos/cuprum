"""Unit tests for the built-in logging hook."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import ECHO, sh
from cuprum.context import ScopeConfig, current_context, scoped
from cuprum.logging_hooks import _build_logging_hooks, logging_hook
from cuprum.sh import CommandResult, RunOutputOptions
from cuprum.unittests._adapter_test_support import capturing_logger

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


def test_logging_hook_registers_and_detaches() -> None:
    """logging_hook adds paired hooks to the current context and detaches cleanly."""
    with (
        capturing_logger("cuprum.test.registry") as capture,
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
    ):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        registration = logging_hook(logger=capture.logger)

        with_hooks = current_context()
        assert len(with_hooks.before_hooks) == before_count + 1
        assert len(with_hooks.after_hooks) == after_count + 1

        registration.detach()

        restored = current_context()
        assert len(restored.before_hooks) == before_count
        assert len(restored.after_hooks) == after_count


def test_logging_hook_emits_start_and_exit() -> None:
    """logging_hook emits start and exit records when a command runs."""
    with (
        capturing_logger("cuprum.test.emit") as capture,
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
        logging_hook(logger=capture.logger),
    ):
        cmd: SafeCmd = sh.make(ECHO)("-n", "hello logs")
        result = cmd.run_sync()

    messages = [record.getMessage() for record in capture.records]
    start_messages = [msg for msg in messages if "cuprum.start" in msg]
    exit_messages = [msg for msg in messages if "cuprum.exit" in msg]

    assert len(start_messages) == 1
    assert len(exit_messages) == 1

    start = start_messages[0]
    finish = exit_messages[0]

    assert "program=echo" in start
    assert "argv=('echo'," in start
    assert "program=echo" in finish
    assert "exit_code=0" in finish
    assert "pid=" in finish
    assert "duration_s=" in finish
    assert "duration_s=unknown" not in finish
    assert f"stdout_len={len(result.stdout or '')}" in finish
    assert "stderr_len=0" in finish
    assert result.stdout is not None


def test_logging_hook_handles_uncaptured_output() -> None:
    """logging_hook logs exit even when output capture is disabled."""
    with (
        capturing_logger("cuprum.test.uncaptured") as capture,
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
        logging_hook(logger=capture.logger),
    ):
        cmd: SafeCmd = sh.make(ECHO)("uncaptured")
        _ = cmd.run_sync(output=RunOutputOptions(capture=False))

    messages = [record.getMessage() for record in capture.records]
    exit_lines = [msg for msg in messages if "cuprum.exit" in msg]
    assert exit_lines, "Expected an exit log line"
    exit_line = exit_lines[0]
    assert "stdout_len=0" in exit_line
    assert "stderr_len=0" in exit_line
    assert "program=echo" in exit_line
    assert "exit_code=0" in exit_line
    assert "duration_s=" in exit_line


def test_logging_hook_detach_is_idempotent() -> None:
    """Calling detach() multiple times is safe and leaves hooks removed."""
    with (
        capturing_logger("cuprum.test.registry.idempotent") as capture,
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
    ):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        registration = logging_hook(logger=capture.logger)
        registration.detach()
        registration.detach()  # second call should be a no-op

        restored = current_context()
        assert len(restored.before_hooks) == before_count
        assert len(restored.after_hooks) == after_count


def test_logging_hook_context_manager_detaches_and_is_idempotent() -> None:
    """Context manager usage detaches hooks and allows further detach calls."""
    with (
        capturing_logger("cuprum.test.registry.context_manager") as capture,
        scoped(ScopeConfig(allowlist=frozenset([ECHO]))),
    ):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        with logging_hook(logger=capture.logger) as registration:
            with_hooks = current_context()
            assert len(with_hooks.before_hooks) == before_count + 1
            assert len(with_hooks.after_hooks) == after_count + 1

        restored = current_context()
        assert len(restored.before_hooks) == before_count
        assert len(restored.after_hooks) == after_count

        registration.detach()
        post_detach = current_context()
        assert len(post_detach.before_hooks) == before_count
        assert len(post_detach.after_hooks) == after_count


def test_logging_hook_logs_unknown_duration() -> None:
    """Exit hook falls back to 'unknown' duration when start timestamp is missing."""
    with capturing_logger("cuprum.test.duration") as capture:
        _start, exit_ = _build_logging_hooks(
            logger=capture.logger,
            start_level=logging.INFO,
            exit_level=logging.INFO,
        )

        # Intentionally call exit without a matching start to hit the fallback.
        cmd: SafeCmd = sh.make(ECHO)("-n", "duration")
        result = CommandResult(
            program=cmd.program,
            argv=cmd.argv,
            exit_code=0,
            pid=1234,
            stdout=None,
            stderr=None,
        )
        exit_(cmd, result)

    messages = [record.getMessage() for record in capture.records]
    exit_lines = [msg for msg in messages if "cuprum.exit" in msg]
    assert exit_lines, "Expected an exit log line"
    assert "duration_s=unknown" in exit_lines[0]


def test_logging_hook_logs_non_zero_exit_code() -> None:
    """Exit hook logs non-zero exit codes with duration and output lengths."""
    with capturing_logger("cuprum.test.failure") as capture:
        start, exit_ = _build_logging_hooks(
            logger=capture.logger,
            start_level=logging.INFO,
            exit_level=logging.INFO,
        )

        cmd: SafeCmd = sh.make(ECHO)("-n", "fail-path")
        start(cmd)
        result = CommandResult(
            program=cmd.program,
            argv=cmd.argv,
            exit_code=1,
            pid=4321,
            stdout="x" * 10,
            stderr="y" * 5,
        )
        exit_(cmd, result)

    messages = [record.getMessage() for record in capture.records]
    assert any(
        "exit_code=1" in msg
        and "duration_s=" in msg
        and "stdout_len=10" in msg
        and "stderr_len=5" in msg
        for msg in messages
    ), "Expected exit log with non-zero exit code and output lengths"
