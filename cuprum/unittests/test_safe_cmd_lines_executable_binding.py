"""Executable-binding and environment-policy refresh for ``SafeCmd.lines()``.

The third face of #543. ``lines()`` prepares a run in one scope and executes it
in whichever scope pulls the first event, so every decision an enclosing scope
made on the caller's behalf has to be re-made at the pull:

- the executable binding, because a scope may permit the same program and bind
  it to a different file — the preparation-time path would reach the spawn as
  ``argv[0]`` and run outside the consuming scope's binding;
- the environment policy, because a scope may replace the environment outright
  and the events the run emits must describe the policy it actually ran under.

Both assertions read the values the run really used: the executable off the
spawned execution, and the policy off the events an observer received.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, observe, scoped
from cuprum.context import EnvMode
from cuprum.executable_binding import executable_binding
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd
    from cuprum.unittests._safe_cmd_lines_authorization_support import (
        _SpawnRecorder,
    )


def test_lines_run_the_consuming_scopes_executable_binding(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
) -> None:
    """A stream prepared under one binding runs the consuming scope's binding.

    The stream is built in a scope that binds the program to a stub which
    prints ``outer``, then pulled in a scope that binds it to a different stub
    printing ``inner``. The consuming scope's binding is the one that must run.

    Had the preparation-time path survived, the run would have executed the
    outer stub, and the only visible difference — the stub that actually ran —
    would be wrong. The spawn record pins the executable the run handed to the
    child, and the child's own output pins that the record was honoured.
    """
    runtime = build_python_catalogue_env()
    outer_stub = _write_stub(tmp_path / "outer.py", "outer")
    inner_stub = _write_stub(tmp_path / "inner.py", "inner")
    command = runtime.builder("-c", "pass")

    async def collect() -> list[str]:
        """Build under one binding, then consume under a different one."""
        stream = command.lines()
        with scoped(
            ScopeConfig(
                executable_bindings={
                    runtime.program: executable_binding(runtime.program, inner_stub)
                }
            )
        ):
            return [event.text async for event in stream]

    with scoped(
        ScopeConfig(
            allowlist=frozenset([runtime.program]),
            executable_bindings={
                runtime.program: executable_binding(runtime.program, outer_stub)
            },
        )
    ):
        lines = asyncio.run(collect())

    assert lines == ["inner"], (
        "the consuming scope's binding must be the one that runs, got "
        f"{lines!r} from the outer stub's 'outer'"
    )
    assert len(spawn_recorder.executions) == 1, (
        "the allowed run must reach the spawn exactly once, got "
        f"{len(spawn_recorder.executions)}"
    )
    assert spawn_recorder.executions[0].observation.argv0 == inner_stub, (
        "the spawn must receive the consuming scope's executable, got "
        f"{spawn_recorder.executions[0].observation.argv0!r}"
    )


@pytest.mark.parametrize(
    ("prepared_mode", "consumed_mode"),
    [
        pytest.param(EnvMode.OVERLAY, EnvMode.REPLACE, id="overlay-then-replace"),
        pytest.param(EnvMode.REPLACE, EnvMode.REPLACE, id="replace-then-replace"),
    ],
)
def test_lines_report_the_consuming_scopes_environment_policy(
    python_builder: cabc.Callable[..., SafeCmd],
    spawn_recorder: _SpawnRecorder,
    *,
    prepared_mode: EnvMode,
    consumed_mode: EnvMode,
) -> None:
    """Every event reports the environment policy the run actually used.

    The stream is prepared under one overlay and pulled under another, with the
    consuming scope either replacing the environment outright or replacing it a
    second time under a different overlay. Both rows assert the consuming
    scope's overlay, and the first also asserts its mode, so a run that kept
    the preparing scope's record differs observably in at least one of them.

    The child's real environment already comes from the consuming scope at the
    spawn; this pins that the events agree, so a consumer reading ``env_mode``
    is not told the run inherited a policy it never had.

    An inner ``OVERLAY`` scope deliberately cannot discard an outer ``REPLACE``
    boundary — the mode is retained across that nesting — so the reverse
    direction is not a case where the two scopes' policies differ at all.
    """
    observed: list[ExecEvent] = []
    command = python_builder("-c", "pass")

    async def collect() -> None:
        """Build under one policy, then consume under another."""
        stream = command.lines()
        with scoped(
            ScopeConfig(
                env_mode=consumed_mode,
                env_overlay={"KEY": "consumed"},
            )
        ):
            async with stream:
                async for _event in stream:
                    pass

    with (
        scoped(
            ScopeConfig(
                allowlist=frozenset([command.program]),
                env_mode=prepared_mode,
                env_overlay={"KEY": "prepared"},
            )
        ),
        observe(observed.append),
    ):
        asyncio.run(collect())

    assert observed, "an observed run must emit lifecycle events"
    assert all(event.env_mode is consumed_mode for event in observed), (
        "every event must report the consuming scope's mode, got "
        f"{[event.env_mode for event in observed]!r}"
    )
    assert all(event.env == {"KEY": "consumed"} for event in observed), (
        "every event must report the consuming scope's overlay, got "
        f"{[event.env for event in observed]!r}"
    )
    assert len(spawn_recorder.executions) == 1, (
        "the allowed run must reach the spawn exactly once, got "
        f"{len(spawn_recorder.executions)}"
    )


def _write_stub(path: Path, word: str) -> str:
    """Write an executable stub that prints *word*, returning its path.

    The stub is a Python script rather than a compiled binary so the test needs
    no build step, and it ignores its arguments so the same command can be
    bound to either stub unchanged.

    Returns
    -------
    str
        The stub's absolute path, ready to hand to
        :func:`~cuprum.executable_binding.executable_binding`.
    """
    path.write_text(f"#!{sys.executable}\nprint({word!r})\n")
    path.chmod(0o755)
    return str(path)
