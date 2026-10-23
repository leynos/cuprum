"""Hook re-collection for ``SafeCmd.lines()``.

The fourth face of #543, and the one no other assertion could see. Hooks are
resolved from the active context, so a stream prepared inside a scope carries
that scope's ``before``, ``after``, and ``observe`` hooks. Those hooks belong to
a scope the caller has left by the time the stream is pulled, and a prepared
stream is not permission to keep running another scope's callbacks.

Both hook carriers are covered deliberately — the before/after hooks a line
iteration dispatches itself, and the observe hooks that become ``ExecEvent``
values — because one refresh has to keep them agreed, and a future split that
refreshed only one would still be the same defect.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import ScopeConfig, scoped
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult
    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult, SafeCmd
    from cuprum.unittests._safe_cmd_lines_authorization_support import (
        _SpawnRecorder,
    )


def _prepare_then_consume(
    command: SafeCmd,
    prepare_scope: ScopeConfig,
    consume_scope: ScopeConfig,
) -> CommandResult | None:
    """Build a stream in *prepare_scope*, then pull it in *consume_scope*.

    The preparing scope is left before the pull, not merely nested inside it:
    ``narrow()`` layers an inner scope's hooks onto the outer set, so a nested
    pair would let the preparing hooks fire legitimately and hide a stream that
    carried them itself.

    Returns
    -------
    CommandResult | None
        The stream's result after the enumeration completes.
    """

    async def collect() -> CommandResult | None:
        with scoped(prepare_scope):
            stream = command.lines()
        with scoped(consume_scope):
            async with stream:
                async for _event in stream:
                    pass
        return stream.result

    return asyncio.run(collect())


def test_lines_run_the_consuming_scopes_hooks(
    spawn_recorder: _SpawnRecorder,
) -> None:
    """A stream prepared under one hook set runs the consuming scope's hooks.

    The stream is built inside a scope that installs a before and an after
    hook, and that scope has already exited before the first event is pulled.
    Its hooks are therefore unreachable from the context when the run starts:
    anything they record could only come from the prepared stream itself. The
    consuming scope installs a different before, after, and observe hook, and
    all three must fire.

    Both halves are asserted together because either alone is satisfiable by a
    mistake: dropping every hook would pass the silence half, and collecting
    the consumed set on top of the prepared one would pass the noise half. The
    pair pins that the hook set was replaced, not added to.

    The spawning scope's allowlist is provided by an outer scope, so neither
    the preparing nor the consuming scope is responsible for permission and
    each is only about hooks.
    """
    runtime = build_python_catalogue_env()
    command = runtime.builder("-c", "pass")
    prepared_calls: list[str] = []
    consumed_calls: list[str] = []
    observed: list[ExecEvent] = []

    def prepared_before(_cmd: SafeCmd) -> None:
        """Record that the preparing scope's before hook ran."""
        prepared_calls.append("before")

    def prepared_after(_cmd: SafeCmd, _result: _AnyCommandResult) -> None:
        """Record that the preparing scope's after hook ran."""
        prepared_calls.append("after")

    def consumed_before(_cmd: SafeCmd) -> None:
        """Record that the consuming scope's before hook ran."""
        consumed_calls.append("before")

    def consumed_after(_cmd: SafeCmd, _result: _AnyCommandResult) -> None:
        """Record that the consuming scope's after hook ran."""
        consumed_calls.append("after")

    def consume_events(event: ExecEvent) -> None:
        """Collect the lifecycle events the consuming scope observes."""
        observed.append(event)

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        result = _prepare_then_consume(
            command,
            ScopeConfig(
                before_hooks=(prepared_before,),
                after_hooks=(prepared_after,),
            ),
            ScopeConfig(
                before_hooks=(consumed_before,),
                after_hooks=(consumed_after,),
                observe_hooks=(consume_events,),
            ),
        )

    assert result is not None, "the run must publish a result"
    assert not prepared_calls, (
        "the preparing scope's hooks must not run after that scope has exited, "
        f"got {prepared_calls!r}"
    )
    assert consumed_calls == ["before", "after"], (
        "the consuming scope's before and after hooks must both run, got "
        f"{consumed_calls!r}"
    )
    assert observed, "the consuming scope's observe hook must receive lifecycle events"
    assert len(spawn_recorder.executions) == 1, (
        "the allowed run must reach the spawn exactly once, got "
        f"{len(spawn_recorder.executions)}"
    )
