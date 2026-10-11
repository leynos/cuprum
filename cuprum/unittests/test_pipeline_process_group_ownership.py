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

    from cuprum._pipeline_config import _PipelineRunConfig

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
    live: set[int] = {7001, 7002}

    def recording_killpg(pgid: int, sig: int) -> None:
        """Record the signal and retire the group it needs to signal.

        The probe that settles the grace period rides on the same call, so the
        stub reports a group as live until it has been signalled — which is
        what a real group does, and what keeps the probe from standing in for
        settlement before anything has been asked to exit.

        Raises
        ------
        ProcessLookupError
            Once *pgid* has no members left. The call is recorded before that
        decision so a poll that finds the group already gone is still
        observable as a probe that happened.
        """
        signalled.append((pgid, sig))
        if pgid not in live:
            raise ProcessLookupError
        if sig != 0:
            live.discard(pgid)

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
    assert probes == [(7001, 0), (7002, 0), (7001, 0), (7002, 0)], (
        "each owned group is probed by the settled check and again by the "
        f"grace-period wait; found {probes!r}"
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


class _SpawnOptions(typ.TypedDict, total=False):
    """The spawn options the pipeline passes, as the spawn call receives them.

    Declared once so the partial-spawn double can forward them verbatim
    without re-listing them as parameters. Each entry is checked against the
    real ``create_subprocess_exec`` parameter it lands on, so the declaration
    is what keeps the forward honest.
    """

    stdin: int
    stdout: int
    stderr: int
    env: dict[str, str] | None
    cwd: str | None
    start_new_session: bool


@dc.dataclass(frozen=True, slots=True)
class _PartialSpawnCase:
    """A two-stage owned pipeline whose second stage refuses to start."""

    first: sh.SafeCmd
    second: sh.SafeCmd
    config: _PipelineRunConfig
    pid_file: Path


def _build_partial_spawn_case(tmp_path: Path) -> _PartialSpawnCase:
    """Build the owned pipeline whose second stage will fail to spawn."""
    pid_file = tmp_path / "grandchild.pid"
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return _PartialSpawnCase(
        first=python(*pipe_holding_child_argv(pid_file)),
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

    async def failing_second_spawn(
        program: str,
        *args: str,
        **kwargs: typ.Unpack[_SpawnOptions],
    ) -> asyncio.subprocess.Process:
        """Spawn the first stage for real, then fail the second conditionally.

        The options the pipeline passes are declared in ``_SpawnOptions`` and
        forwarded verbatim, so the real spawn receives exactly what it would
        have received unpatched while this double keeps a call signature small
        enough to read. The declaration is a check rather than documentation:
        ``ty`` validates each entry against the real parameter it lands on, so
        an option given the wrong type fails here.

        Returns
        -------
        asyncio.subprocess.Process
            The real spawn's result, for the first call only.

        Raises
        ------
        FileNotFoundError
            For the second call, standing in for a stage that cannot start.
        """
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
        return await real_spawn(program, *args, **kwargs)

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

    grandchild_pid = int(case.pid_file.read_text(encoding="utf-8").strip())
    assert not process_is_running(grandchild_pid), (
        "a partial spawn must tear down the owned group it already created, "
        "grandchild included"
    )


def test_an_exited_owned_stage_still_reaches_its_live_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage that exited early is still torn down through its group.

    ``_cleanup_spawned_processes`` runs on the partial-spawn path, where a
    stage that has already exited is an ordinary outcome rather than a
    failure. A completed direct child is nevertheless not settlement for an
    owning run: the group it led can outlive it, and a descendant still in that
    group holds the inherited pipes open.

    The group name is the reaped leader's pid, so the probe that decides this
    must itself be safe — and it is, because the kernel keeps a group
    addressable while it has a member, so a name that still answers cannot have
    been recycled. Signalling it therefore reaches this run's own descendant
    rather than the stranger the old blanket short-circuit feared.
    """
    doubles = [_RecordingProcess(7005)]
    doubles[0].returncode = 0
    group_signals: list[tuple[int, int]] = []
    # A group stays addressable while it holds a member, which is what makes
    # the probe safe. Model a member that ignores SIGTERM, so the probe keeps
    # answering until the escalation empties the group.
    is_live = True

    def live_group_killpg(pgid: int, sig: int) -> None:
        """Report the group as live until it is compelled to exit."""
        nonlocal is_live
        if is_live is False:
            raise ProcessLookupError(pgid, "No such process")
        if sig == 0:
            return
        group_signals.append((pgid, sig))
        if sig == signal.SIGKILL:
            is_live = False

    monkeypatch.setattr(os, "killpg", live_group_killpg)

    async def run_case() -> None:
        """Tear down a partial spawn whose one stage has already exited."""
        await _pipeline_spawn._cleanup_spawned_processes(
            _as_processes(doubles),
            [],
            None,
            _TeardownPolicy(0.1, owns_group=[True]),
        )

    asyncio.run(run_case())

    assert group_signals == [(7005, signal.SIGTERM), (7005, signal.SIGKILL)], (
        "an exited owned stage's group must be asked to exit and then "
        f"compelled, reaching the descendant the leader left behind; got "
        f"{group_signals!r}"
    )
    assert doubles[0].terminated is False, (
        "an owned stage is reached through its group, not the direct route"
    )
    assert doubles[0].killed is False, (
        "the direct child has already exited, so it is never escalated"
    )
    assert doubles[0].returncode == 0, (
        "the stage's recorded exit code must survive teardown unchanged"
    )
