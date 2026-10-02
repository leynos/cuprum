"""Pipeline teardown under the opt-in process-group ownership policy.

A pipeline is where ownership matters most: every stage is a separate child,
so a pipeline run spans one group per owned stage, and a partial spawn is the
moment when some of those groups exist and others do not. These tests assert
that each stage's teardown honours the policy it was spawned under — the
ownership recorded per stage, not one blanket flag — and that a failure part
way through spawning still tears down the groups already created.

Per-stage bookkeeping is what these tests make observable: a teardown that
applied one stage's ownership to all of them would signal a group it does not
own, or skip a group it does, and either mistake shows up in what gets
signalled.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import dataclasses as dc
import os
import signal
import sys
import typing as typ

import pytest

from cuprum import _pipeline_spawn, sh
from cuprum._teardown_policy import _TeardownPolicy
from cuprum._testing import _prepare_pipeline_config
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from tests.helpers.catalogue import (
    python_catalogue,
)
from tests.helpers.timeouts import (
    pipe_holding_child_argv,
    process_is_running,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only


@dc.dataclass(slots=True)
class _RecordingProcess:
    """Process double that records the signals teardown delivers."""

    pid: int
    returncode: int | None = None
    terminated: bool = False
    killed: bool = False

    def terminate(self) -> None:
        """Record the initial signal and exit, as a cooperative child does."""
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        """Record the escalation and exit."""
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    async def wait(self) -> int:
        """Return once a signal has recorded an exit code."""
        await asyncio.sleep(0)
        return self.returncode if self.returncode is not None else 0


def _as_processes(doubles: list[_RecordingProcess]) -> list[asyncio.subprocess.Process]:
    """Present the doubles as the process handles teardown expects."""
    return [typ.cast("asyncio.subprocess.Process", double) for double in doubles]


def test_owned_pipeline_stages_are_each_signalled_as_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every owned stage's group is signalled, rather than only the first.

    Teardown reads ownership per stage, so the second stage's group is reached
    even though the first stage's exit settles its own wait first. A teardown
    that read one flag for the whole pipeline would leave the later stage's
    descendants running.
    """
    signalled: list[tuple[int, int]] = []

    def recording_killpg(pgid: int, sig: int) -> None:
        """Record the signal, then report the group as empty.

        There are no real members behind these doubles, so the probe that
        settles the grace period has to see the group empty; reporting
        ``ProcessLookupError`` for the null probe is how a stub says so.

        Raises
        ------
        ProcessLookupError
            For the null probe on signal ``0``, standing in for a group whose
            last member has gone.
        """
        signalled.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", recording_killpg)

    async def run_case() -> None:
        """Tear down a partial spawn whose recorded stages all own groups."""
        await _pipeline_spawn._cleanup_spawned_processes(
            _as_processes([_RecordingProcess(7001), _RecordingProcess(7002)]),
            [],
            None,
            _TeardownPolicy(0.1, owns_group=[True, True]),
        )

    asyncio.run(run_case())

    delivered = [entry for entry in signalled if entry[1] != 0]
    probes = [entry for entry in signalled if entry[1] == 0]
    assert delivered == [(7001, signal.SIGTERM), (7002, signal.SIGTERM)], (
        "each owned stage's group must be signalled, in stage order; "
        f"found {signalled!r}"
    )
    assert sorted(probes) == [(7001, 0), (7002, 0)], (
        "the grace period must be settled by probing each owned group; "
        f"found {probes!r}"
    )


def test_inherited_pipeline_stages_are_signalled_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stage group is signalled when the pipeline inherits the caller's.

    The control for the case above: with no ownership recorded, teardown must
    fall back to signalling each direct child and never call ``killpg``. A
    group signal here would reach the test runner's own group.
    """
    group_signals: list[int] = []
    doubles = [_RecordingProcess(7003), _RecordingProcess(7004)]

    def forbidden_killpg(pgid: int, sig: int) -> None:
        """Record rather than signal a group this run does not own."""
        del sig
        group_signals.append(pgid)

    monkeypatch.setattr(os, "killpg", forbidden_killpg)

    async def run_case() -> None:
        """Tear down stages recorded as inheriting the pipeline's group."""
        await _pipeline_spawn._cleanup_spawned_processes(
            _as_processes(doubles),
            [],
            None,
            _TeardownPolicy(0.1, owns_group=[False, False]),
        )

    asyncio.run(run_case())

    assert not group_signals, "an inheriting pipeline must never signal a group"
    assert all(double.terminated for double in doubles), (
        "every stage must still be signalled directly"
    )


@dc.dataclass(frozen=True, slots=True)
class _PartialSpawnCase:
    """A two-stage owned pipeline whose second stage refuses to start."""

    first: sh.SafeCmd
    second: sh.SafeCmd
    config: object
    pid_file: Path


def _build_partial_spawn_case(tmp_path: Path) -> _PartialSpawnCase:
    """Build the owned pipeline whose second stage will fail to spawn."""
    pid_file = tmp_path / "grandchild.pid"
    marker = tmp_path / "grandchild.ready"
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return _PartialSpawnCase(
        first=python(*pipe_holding_child_argv(pid_file, marker)),
        second=python("-c", "pass"),
        config=_prepare_pipeline_config(
            output=RunOutputOptions(capture=True, echo=False),
            timeout=None,
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        ),
        pid_file=pid_file,
    )


def _install_failing_second_spawn(
    monkeypatch: pytest.MonkeyPatch,
    case: _PartialSpawnCase,
) -> None:
    """Let the first stage spawn for real and make the second raise.

    The second spawn waits for the grandchild to announce itself before
    raising. Without that gate the cleanup could reap the first stage before
    the grandchild had recorded anything, and the case would pass while proving
    nothing about what the cleanup reached.
    """
    real_spawn = asyncio.create_subprocess_exec
    spawn_calls = 0

    async def failing_second_spawn(*args: object, **kwargs: object) -> object:
        """Spawn the first stage for real, then fail the second conditionally."""
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls > 1:
            await asyncio.to_thread(
                wait_for_pid_file,
                case.pid_file,
                context="grandchild readiness",
            )
            msg = "the second stage refused to start"
            raise FileNotFoundError(msg)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_second_spawn)


def test_a_partial_spawn_leaves_no_owned_group_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real pipeline that fails to spawn its second stage still owns nothing.

    End-to-end counterpart to the doubles above, and the case where ownership
    earns its keep: the first stage has already spawned a child that holds a
    pipe through a ``SIGTERM``-immune grandchild when the second stage cannot
    be started. That stage's group is the only thing that can reach the
    grandchild, so a partial-spawn cleanup that signalled direct children alone
    would leave both a process and a pipe behind.
    """
    case = _build_partial_spawn_case(tmp_path)
    _install_failing_second_spawn(monkeypatch, case)

    async def run_case() -> None:
        """Spawn the failing pipeline and assert the failure propagates."""
        with pytest.raises(FileNotFoundError):
            await _pipeline_spawn._spawn_pipeline_processes(
                (case.first, case.second),
                case.config,
            )

    asyncio.run(run_case())

    grandchild_pid = int(case.pid_file.read_text().strip())
    assert not process_is_running(grandchild_pid), (
        "a partial spawn must tear down the owned group it already created, "
        "grandchild included"
    )
