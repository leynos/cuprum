"""Unit tests for context-hook integration during concurrent execution.

These verify that before, after, and observe hooks fire once per command
when commands are run concurrently within a scoped allowlist.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

from cuprum import (
    ECHO,
    CommandResult,
    ExecEvent,
    SafeCmd,
    ScopeConfig,
    after,
    before,
    observe,
    scoped,
    sh,
)
from cuprum.concurrent import (
    run_concurrent_sync,
)


def _run_hook_test[T](
    hook_context: cabc.Callable[[list[T]], contextlib.AbstractContextManager[None]],
    num_commands: int = 3,
) -> list[T]:
    """Run concurrent echo commands within a hook context and return hook calls."""
    echo = sh.make(ECHO)
    commands = [echo("-n", f"cmd{i}") for i in range(num_commands)]
    hook_calls: list[T] = []

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), hook_context(hook_calls):
        run_concurrent_sync(*commands)

    return hook_calls


class TestConcurrentHooks:
    """Verify hook integration during concurrent execution."""

    @staticmethod
    def test_before_hooks_fire_per_command() -> None:
        """Before hooks fire for each command in concurrent execution."""

        def make_tracker(calls: list[str]) -> cabc.Callable[[SafeCmd], None]:
            """Build a before-hook that records each command's program."""

            def track_before(cmd: SafeCmd) -> None:
                """Append the command's program to the tracked before-hook calls."""
                calls.append(str(cmd.program))

            return track_before

        hook_calls = _run_hook_test(lambda calls: before(make_tracker(calls)))

        assert len(hook_calls) == 3, "the before hook must fire once per command"
        assert all(call == "echo" for call in hook_calls), (
            "each before hook call must report the echo program"
        )

    @staticmethod
    def test_after_hooks_fire_per_command() -> None:
        """After hooks fire for each command in concurrent execution."""

        def make_tracker(
            calls: list[int],
        ) -> cabc.Callable[[SafeCmd, CommandResult], None]:
            """Build an after-hook that records each command's exit code."""

            def track_after(cmd: SafeCmd, result: CommandResult) -> None:
                """Append the command's exit code to the tracked after-hook calls."""
                _ = cmd  # Unused
                calls.append(result.exit_code)

            return track_after

        hook_calls = _run_hook_test(lambda calls: after(make_tracker(calls)))

        assert len(hook_calls) == 3, "the after hook must fire once per command"
        assert all(code == 0 for code in hook_calls), (
            "each after hook call must report a successful exit code"
        )

    @staticmethod
    def test_observe_hooks_receive_events_per_command() -> None:
        """Observe hooks receive events for each command."""

        def make_tracker(
            calls: list[ExecEvent],
        ) -> cabc.Callable[[ExecEvent], None]:
            """Build an observe-hook that records each execution event."""

            def track_events(ev: ExecEvent) -> None:
                """Append each received execution event to the tracked list."""
                calls.append(ev)

            return track_events

        events = _run_hook_test(
            lambda calls: observe(make_tracker(calls)),
            num_commands=2,
        )

        # Each command should emit plan, start, stdout (if any), exit events
        phases = [ev.phase for ev in events]
        assert phases.count("plan") == 2, "each command must emit one plan event"
        assert phases.count("start") == 2, "each command must emit one start event"
        assert phases.count("exit") == 2, "each command must emit one exit event"
