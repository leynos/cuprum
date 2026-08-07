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
