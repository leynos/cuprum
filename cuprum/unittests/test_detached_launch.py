"""The resolution rules a detached launch shares with a managed run.

A detached command is the *same command* a managed run would execute: the
allowlist decides whether it may run, an executable binding decides which file
runs, and the argument vector, environment, and working directory reach the
child unchanged. What differs is only what happens afterwards. These cases pin
the shared half by replacing the spawn with a recorder, so every assertion reads
the values the launch actually resolved rather than inferring them from a
child's behaviour.

The two refusals are here as well, because both must happen before a child
exists: a forbidden program, and a platform that cannot provide the session
isolation detachment requires.
"""

from __future__ import annotations

import os
import stat
import typing as typ

import pytest

import cuprum
from cuprum import ECHO, ScopeConfig, _detached_launch, scoped, sh
from cuprum._detached_launch import _DetachedLaunchEntryPoints
from cuprum._observability import _bound_executable
from cuprum.context import UNSET, EnvMode, bind_executable
from cuprum.context._scope import ForbiddenProgramError
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.detached import FAKE_PID, record_spawns

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.program import Program
    from cuprum.sh import DetachedLaunch, SafeCmd


def _command(*argv: str) -> tuple[SafeCmd, Program, ProgramCatalogue]:
    """Build an allowlisted Python command and the scope it needs."""
    env = build_python_catalogue_env()
    return env.builder(*argv), env.program, env.catalogue


def _launch_in_scope(
    command: SafeCmd,
    program: Program,
    *,
    context: ExecutionContext | None = None,
) -> DetachedLaunch:
    """Launch *command* inside a scope that allows *program*."""
    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        return command.launch_detached_sync(context=context)


def test_the_receipt_names_the_process_the_spawn_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt's PID is the spawned child's, not a placeholder."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    receipt = _launch_in_scope(command, program)

    assert calls, "the launch must spawn exactly one child"
    assert receipt.pid == FAKE_PID
    assert receipt.program == program
    assert receipt.executable == str(program)


def test_the_spawn_receives_the_catalogued_argv_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argument vector reaches the spawn verbatim, behind the executable."""
    calls = record_spawns(monkeypatch)
    arguments = ("-c", "print('a b')", "c;d", "$HOME", "*")
    command, program, _ = _command(*arguments)

    _launch_in_scope(command, program)

    assert calls[0].argv == (str(program), *arguments)


def test_the_child_is_spawned_as_a_session_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detachment always asks for the session isolation it documents."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    _launch_in_scope(command, program)

    assert calls[0].start_new_session is True


def test_the_context_environment_overlay_reaches_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OVERLAY`` keeps the parent's environment and layers the overlay on it."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    _launch_in_scope(
        command,
        program,
        context=ExecutionContext(env={"CUPRUM_DETACHED": "1"}),
    )

    env = calls[0].env
    assert env is not None
    assert env["CUPRUM_DETACHED"] == "1"
    assert env["PATH"] == os.environ["PATH"]


def test_replace_mode_starts_the_child_from_an_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``REPLACE`` gives the child only what the overlay names."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    _launch_in_scope(
        command,
        program,
        context=ExecutionContext(env={"CUPRUM_ONLY": "1"}, env_mode=EnvMode.REPLACE),
    )

    assert calls[0].env == {"CUPRUM_ONLY": "1"}


def test_an_unset_entry_removes_a_variable_from_the_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``UNSET`` overlay value withholds the variable without mutating ours."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    _launch_in_scope(
        command,
        program,
        context=ExecutionContext(env={"PATH": UNSET}),
    )

    env = calls[0].env
    assert env is not None
    assert "PATH" not in env
    assert "PATH" in os.environ


def test_the_context_working_directory_reaches_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The child is spawned in the directory the context names."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    _launch_in_scope(command, program, context=ExecutionContext(cwd=tmp_path))

    assert calls[0].cwd == str(tmp_path)


def test_a_bound_executable_is_what_runs_and_what_the_receipt_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A binding decides the executed file and the receipt's executable."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")
    bound = tmp_path / "bound-tool"
    bound.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bound.chmod(bound.stat().st_mode | stat.S_IXUSR)

    with (
        scoped(ScopeConfig(allowlist=frozenset({program}))),
        bind_executable(program, str(bound)),
    ):
        receipt = command.launch_detached_sync()

    assert calls[0].argv[0] == str(bound)
    assert receipt.executable == str(bound)


def test_an_unbound_program_runs_under_the_shared_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound program uses the same executable rule as a managed run.

    Both spawn sites reach the answer through ``_bound_executable``; comparing
    the launch's ``argv[0]`` with that rule is what keeps the two from drifting.
    """
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")

    receipt = _launch_in_scope(command, program)

    expected = _bound_executable(program, None)
    assert calls[0].argv[0] == expected
    assert receipt.executable == expected


def test_a_forbidden_program_is_refused_before_anything_is_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist is checked first, so a refusal creates no child.

    An empty allowlist is permissive, so the scope has to name some other
    program for the command to be refused at all.
    """
    calls = record_spawns(monkeypatch)
    command, _program, _ = _command("-c", "pass")

    with (
        scoped(ScopeConfig(allowlist=frozenset({ECHO}))),
        pytest.raises(ForbiddenProgramError),
    ):
        command.launch_detached_sync()

    assert not calls, "a refused command must not reach the spawn"


def test_a_platform_without_posix_sessions_is_refused_before_any_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform gate raises before the spawn, naming what it required."""
    calls = record_spawns(monkeypatch)
    command, program, _ = _command("-c", "pass")
    monkeypatch.setattr(os, "name", "nt")

    with (
        scoped(ScopeConfig(allowlist=frozenset({program}))),
        pytest.raises(ValueError, match="POSIX process groups"),
    ):
        command.launch_detached_sync()

    assert not calls, "an unsupported platform must not reach the spawn"


def test_the_entry_points_are_contributed_by_the_mixin() -> None:
    """Both spellings are the mixin's methods, not a second copy.

    ``SafeCmd`` is at the module ceiling, so the entry points live in the
    detached-launch module and are mixed in. Identity is what proves the class
    did not also grow a divergent definition of its own.
    """
    mixin = _DetachedLaunchEntryPoints
    assert sh.SafeCmd.launch_detached is mixin.launch_detached
    assert sh.SafeCmd.launch_detached_sync is mixin.launch_detached_sync


def test_the_public_receipt_is_exported_from_both_import_paths() -> None:
    """The receipt is reachable as ``cuprum.DetachedLaunch`` and from ``sh``."""
    assert cuprum.DetachedLaunch is _detached_launch.DetachedLaunch
    assert sh.DetachedLaunch is _detached_launch.DetachedLaunch
    for module in (cuprum, sh):
        assert "DetachedLaunch" in module.__all__, (
            f"{module.__name__}.__all__ must advertise the receipt"
        )
