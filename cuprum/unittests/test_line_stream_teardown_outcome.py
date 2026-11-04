"""Teardown failures on the ``SafeCmd.lines()`` path reach the sink outcome.

A child can finish successfully while an observe hook scheduled as a task
fails only when teardown drains it. That failure is part of how the stream
ended, so the session must annotate it instead of recording the child's exit
status. A failure the run itself reported still outranks it.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.sinks.github_actions import _escape_property
from cuprum.unittests.test_sinks_end_to_end import (
    _ON_CI,
    _annotations,
    _flags_output,
    _python_command,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent

_HOOK_FAILURE_MESSAGE = "observe hook failed"


async def _drain(command: sh.SafeCmd, exits: list[int | None]) -> None:
    """Consume one streamed run, recording its exit code even if teardown fails."""
    async with command.lines(
        output=_flags_output(annotate_failure=True),
    ) as stream:
        try:
            async for _event in stream:
                pass
        finally:
            # A teardown failure surfaces from the final ``__anext__``, which
            # refuses to complete the loop; the run's result is already set.
            result = stream.result
            exits.append(None if result is None else result.exit_code)


def _failing_hook(_event: ExecEvent) -> cabc.Awaitable[None]:
    """Return an awaitable that fails when teardown drains its task."""

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError(_HOOK_FAILURE_MESSAGE)

    return fail()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(("print('done')", 0, "error"), id="child-succeeded"),
        pytest.param(("raise SystemExit(7)", 7, "exit_nonzero"), id="child-failed"),
    ],
)
def test_teardown_failure_reaches_the_close_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, int, str],
) -> None:
    """A teardown failure annotates the close without masking the run's verdict.

    The hook task fails after the child has already reported its exit status,
    so iteration itself ends normally. The stream must still surface the hook
    failure to the caller, and the session must record it: a successful child
    is annotated ``error`` rather than left looking clean, while a child that
    exited non-zero keeps ``exit_nonzero`` because its own failure outranks the
    teardown error.
    """
    child_source, expected_exit, expected_detail = case
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(child_source)
    exits: list[int | None] = []

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        sh.observe(_failing_hook),
        pytest.raises(RuntimeError, match="observe hook failed"),
    ):
        asyncio.run(_drain(command, exits))

    assert exits == [expected_exit], (
        f"the child must exit {expected_exit}; got {exits!r}"
    )
    label = _escape_property(_run_label(command))
    assert _annotations(capsys.readouterr().err) == [
        f"title={label}::{expected_detail}",
    ], "the close outcome must report the teardown failure without masking the run"
