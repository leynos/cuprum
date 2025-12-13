"""Unit tests for the built-in logging hook."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import ECHO, sh
from cuprum.context import current_context, scoped
from cuprum.logging_hooks import _build_logging_hooks, logging_hook
from cuprum.sh import CommandResult

if typ.TYPE_CHECKING:
    import pytest

    from cuprum.sh import SafeCmd


def test_logging_hook_registers_and_detaches() -> None:
    """logging_hook adds paired hooks to the current context and detaches cleanly."""
    logger = logging.getLogger("cuprum.test.registry")
    with scoped(allowlist=frozenset([ECHO])):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        registration = logging_hook(logger=logger)

        with_hooks = current_context()
        assert len(with_hooks.before_hooks) == before_count + 1
        assert len(with_hooks.after_hooks) == after_count + 1

        registration.detach()

        restored = current_context()
        assert len(restored.before_hooks) == before_count
        assert len(restored.after_hooks) == after_count


def test_logging_hook_emits_start_and_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """logging_hook emits start and exit records when a command runs."""
    caplog.set_level(logging.INFO, logger="cuprum.test.emit")
    logger = logging.getLogger("cuprum.test.emit")

    with scoped(allowlist=frozenset([ECHO])), logging_hook(logger=logger):
        cmd: SafeCmd = sh.make(ECHO)("-n", "hello logs")
        result = cmd.run_sync()

    messages = [record.getMessage() for record in caplog.records]
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


def test_logging_hook_handles_uncaptured_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """logging_hook logs exit even when output capture is disabled."""
    caplog.set_level(logging.INFO, logger="cuprum.test.uncaptured")
    logger = logging.getLogger("cuprum.test.uncaptured")

    with scoped(allowlist=frozenset([ECHO])), logging_hook(logger=logger):
        cmd: SafeCmd = sh.make(ECHO)("uncaptured")
        _ = cmd.run_sync(capture=False)

    messages = [record.getMessage() for record in caplog.records]
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
    logger = logging.getLogger("cuprum.test.registry.idempotent")
    with scoped(allowlist=frozenset([ECHO])):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        registration = logging_hook(logger=logger)
        registration.detach()
        registration.detach()  # second call should be a no-op

        restored = current_context()
        assert len(restored.before_hooks) == before_count
        assert len(restored.after_hooks) == after_count


def test_logging_hook_context_manager_detaches_and_is_idempotent() -> None:
    """Context manager usage detaches hooks and allows further detach calls."""
    logger = logging.getLogger("cuprum.test.registry.context_manager")
    with scoped(allowlist=frozenset([ECHO])):
        before_count = len(current_context().before_hooks)
        after_count = len(current_context().after_hooks)

        with logging_hook(logger=logger) as registration:
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


def test_logging_hook_logs_unknown_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit hook falls back to 'unknown' duration when start timestamp is missing."""
    caplog.set_level(logging.INFO, logger="cuprum.test.duration")
    logger = logging.getLogger("cuprum.test.duration")
    _start, exit_ = _build_logging_hooks(
        logger=logger,
        start_level=logging.INFO,
        exit_level=logging.INFO,
    )

    # Intentionally call exit without a matching start to hit the fallback path.
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

    messages = [record.getMessage() for record in caplog.records]
    exit_lines = [msg for msg in messages if "cuprum.exit" in msg]
    assert exit_lines, "Expected an exit log line"
    assert "duration_s=unknown" in exit_lines[0]


def test_logging_hook_logs_non_zero_exit_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit hook logs non-zero exit codes with duration and output lengths."""
    caplog.set_level(logging.INFO, logger="cuprum.test.failure")
    logger = logging.getLogger("cuprum.test.failure")
    start, exit_ = _build_logging_hooks(
        logger=logger,
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

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "exit_code=1" in msg
        and "duration_s=" in msg
        and "stdout_len=10" in msg
        and "stderr_len=5" in msg
        for msg in messages
    ), "Expected exit log with non-zero exit code and output lengths"
