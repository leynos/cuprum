"""Unit tests for the built-in logging hook."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import ECHO, sh
from cuprum.context import current_context, scoped
from cuprum.logging_hooks import logging_hook

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
    assert any("cuprum.exit" in msg for msg in messages)
    # Should not crash when stdout/stderr are None
    assert not [msg for msg in messages if "stdout_len=None" in msg]
