"""Shared SafeCmd execution types for the test suite.

Defines the ``_RunKwargs`` keyword-argument shape accepted by
``SafeCmd.run``/``SafeCmd.run_sync`` and the ``ExecuteFn`` callable alias used by
the parametrized ``run()``/``run_sync()`` execution fixtures across the SafeCmd
test modules.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.sh import (
        CommandResult,
        ExecutionContext,
        RunOutputOptions,
        SafeCmd,
        StdinInput,
    )


class _RunKwargs(typ.TypedDict, total=False):
    """Keyword arguments accepted by ``SafeCmd.run``/``SafeCmd.run_sync``."""

    output: RunOutputOptions | None
    timeout: float | None
    context: ExecutionContext | None
    stdin: StdinInput | None


type ExecuteFn = cabc.Callable[[SafeCmd, _RunKwargs], CommandResult]


def assert_capture_disabled(result: CommandResult) -> None:
    """Assert a command exited cleanly with no output retained."""
    if result.exit_code != 0:
        msg = f"expected a clean exit with capture disabled, got {result.exit_code!r}"
        raise AssertionError(msg)
    if result.stdout is not None:
        msg = (
            f"stdout must not be retained when capture is disabled, "
            f"got {result.stdout!r}"
        )
        raise AssertionError(msg)
    if result.stderr is not None:
        msg = (
            f"stderr must not be retained when capture is disabled, "
            f"got {result.stderr!r}"
        )
        raise AssertionError(msg)
