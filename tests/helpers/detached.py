"""Running a detached launch from an outer process and observing its worker.

Detachment is a claim about two processes, so the only honest way to test it is
with two. A launcher process starts a worker through cuprum and exits; the
worker it leaves behind reports what it sees. Nothing the worker reports can
travel over its standard streams — a detached launch disconnects all three by
design — so it reports through a JSON file in the directory the caller names.

The launcher is deliberately a separate interpreter rather than an in-process
child: a worker that outlives its launcher is exactly the property under test,
and an in-process launcher cannot exit without taking the test with it.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the launcher is this repository's own source, run with a fixed argv.
import sys
import time
import typing as typ

from cuprum import _detached_launch
from tests.helpers.docs import repo_root
from tests.helpers.process_state import process_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    import pytest

__all__ = [
    "FAKE_PID",
    "LAUNCHER_SOURCE",
    "WORKER_SOURCE",
    "FakeProcess",
    "LaunchSpec",
    "Receipt",
    "SpawnCall",
    "WorkerReport",
    "is_running",
    "read_receipt",
    "read_worker_report",
    "record_abandonment",
    "record_spawns",
    "run_launcher",
    "stop_group",
    "wait_for_file",
]

#: A PID no test has started. It is plausible enough that the kernel could have
#: assigned it (the default ``pid_max`` exceeds it), so ``record_spawns`` also
#: replaces the abandonment path with a guard: a launch that reaches it fails
#: the case instead of signalling a stranger's process group.
FAKE_PID = 2_000_000


@dc.dataclass(frozen=True, slots=True)
class SpawnCall:
    """The arguments a launch handed to the spawn seam."""

    argv: tuple[str, ...]
    env: cabc.Mapping[str, str] | None
    cwd: str | None
    start_new_session: bool


class FakeProcess:
    """The part of a ``Popen`` object a launch reads after spawning."""

    __slots__ = ("pid",)

    def __init__(self, *, pid: int = FAKE_PID) -> None:
        """Store the PID this stand-in reports."""
        self.pid = pid


def record_spawns(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int = FAKE_PID,
) -> list[SpawnCall]:
    """Replace the spawn seam with one that records and reports *pid*.

    Returns
    -------
    list[SpawnCall]
        The calls the launch makes, in order. Empty means no child was started.
    """
    calls: list[SpawnCall] = []

    def recording_spawn(
        argv: cabc.Sequence[str],
        *,
        env: cabc.Mapping[str, str] | None,
        cwd: str | None,
        start_new_session: bool,
    ) -> FakeProcess:
        """Record the spawn inputs and hand back a PID."""
        calls.append(SpawnCall(tuple(argv), env, cwd, start_new_session))
        return FakeProcess(pid=pid)

    def refuse_abandonment(pid: int) -> None:
        """Fail the case rather than signal a fake PID."""
        message = f"unexpected abandonment of fake PID {pid}"
        raise AssertionError(message)

    monkeypatch.setattr(_detached_launch, "_spawn_detached", recording_spawn)
    # ``FAKE_PID`` can name a live process on a host whose ``pid_max`` exceeds
    # it, so the default abandonment path must never run against a recorded
    # spawn. A case that expects abandonment replaces this guard with
    # ``record_abandonment``.
    monkeypatch.setattr(
        _detached_launch,
        "_abandon_detached",
        refuse_abandonment,
    )
    return calls


def record_abandonment(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the abandonment path with a recorder.

    Returns
    -------
    list[int]
        The PIDs the launch abandoned, in order.
    """
    abandoned: list[int] = []
    monkeypatch.setattr(_detached_launch, "_abandon_detached", abandoned.append)
    return abandoned


#: How long the worker blocks after writing its report. Long enough that a
#: case finding it alive is not racing its natural exit.
WORKER_SLEEP_SECONDS = 300

#: The worker. It reports what it can see about itself, then blocks until
#: something stops it. ``argv[1]`` is the report path; anything after it is
#: passed through so a caller can prove argument boundaries survive the spawn.
WORKER_SOURCE = "\n".join((
    "import json, os, pathlib, sys, time",
    "report_path = pathlib.Path(sys.argv[1])",
    "try:",
    "    stdin_chunk = os.read(0, 1)",
    "except OSError:",
    "    stdin_chunk = None",
    "def fd_target(fd):",
    "    try:",
    "        return os.readlink(f'/proc/self/fd/{fd}')",
    "    except OSError:",
    "        return None",
    "report = {",
    "    'pid': os.getpid(),",
    "    'sid': os.getsid(0),",
    "    'pgid': os.getpgid(0),",
    "    'argv': sys.argv,",
    "    'cwd': os.getcwd(),",
    "    'env': dict(os.environ),",
    "    'stdin_eof': stdin_chunk == b'',",
    "    'fd_targets': {str(fd): fd_target(fd) for fd in (0, 1, 2)},",
    "}",
    # Published by rename so the polling reader never sees a partial report:
    # creating the destination directly exposes an empty file before the write.
    "tmp_report = report_path.with_name(report_path.name + '.tmp')",
    "tmp_report.write_text(json.dumps(report))",
    "os.replace(tmp_report, report_path)",
    f"time.sleep({WORKER_SLEEP_SECONDS})",
))

#: The launcher. It resolves the spec, launches one command through cuprum, and
#: writes the receipt. Exiting is the point: whatever the worker does next
#: happens after the process that started it is gone.
LAUNCHER_SOURCE = "\n".join((
    "import json, pathlib, sys",
    "from cuprum import ProgramCatalogue, ScopeConfig, scoped, sh",
    "from cuprum.context import UNSET, EnvMode, bind_executable",
    "from cuprum.program import Program",
    "from cuprum.sh import ExecutionContext",
    "spec = json.loads(pathlib.Path(sys.argv[1]).read_text())",
    "program = Program(sys.executable)",
    "catalogue = ProgramCatalogue.from_programs(program)",
    "builder = sh.make(program, catalogue=catalogue)",
    "overlay = {",
    "    name: UNSET if value is None else value",
    "    for name, value in spec['env'].items()",
    "}",
    "context = ExecutionContext(",
    "    env=overlay, env_mode=EnvMode(spec['env_mode']), cwd=spec['cwd'],",
    ")",
    "allowed = program if spec['allow'] else Program('cuprum-not-this-program')",
    "scope = ScopeConfig(allowlist=frozenset({allowed}))",
    "def launch():",
    "    with scoped(scope):",
    "        return builder(*spec['argv']).launch_detached_sync(context=context)",
    "if spec['bind'] is None:",
    "    receipt = launch()",
    "else:",
    "    with bind_executable(program, spec['bind']):",
    "        receipt = launch()",
    "pathlib.Path(spec['receipt']).write_text(json.dumps({",
    "    'pid': receipt.pid,",
    "    'program': str(receipt.program),",
    "    'executable': receipt.executable,",
    "}))",
))

#: How often a wait re-checks the fact it is waiting for.
_POLL_SECONDS = 0.01


@dc.dataclass(frozen=True, slots=True)
class LaunchSpec:
    """What the launcher should start, and where it writes the receipt.

    Attributes
    ----------
    argv:
        The worker's argument vector, as cuprum will pass it to ``exec``.
    receipt_path:
        File the launcher writes the receipt to. The spec itself is written
        beside it, so cleanup is the caller's ``tmp_path``.
    env:
        Environment overlay. ``None`` for a value means the variable is unset.
    env_mode:
        ``"overlay"`` or ``"replace"``; the launcher passes it to cuprum.
    cwd:
        Working directory for the worker, or ``None`` to inherit.
    allow:
        Whether the launcher's scope allows the program it launches. ``False``
        makes the launch a ForbiddenProgramError case.
    bind:
        An executable binding for the program, or ``None`` for none. A path to
        a file that does not exist exercises the start-up failure case.
    """

    argv: tuple[str, ...]
    receipt_path: pathlib.Path
    env: cabc.Mapping[str, str | None] = dc.field(default_factory=dict)
    env_mode: str = "overlay"
    cwd: pathlib.Path | None = None
    allow: bool = True
    bind: str | None = None


@dc.dataclass(frozen=True, slots=True)
class Receipt:
    """The launcher's account of the launch it made."""

    pid: int
    program: str
    executable: str


@dc.dataclass(frozen=True, slots=True)
class WorkerReport:
    """What the worker could see about itself once it was running."""

    pid: int
    sid: int
    pgid: int
    argv: tuple[str, ...]
    cwd: str
    env: cabc.Mapping[str, str]
    stdin_eof: bool
    fd_targets: cabc.Mapping[str, str | None]


def run_launcher(
    spec: LaunchSpec,
    *,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run the launcher for *spec* and return its completed process.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The launcher's result, with its output captured so a failed launch can
        be diagnosed from the traceback it printed.
    """
    payload = {
        "argv": list(spec.argv),
        "env": dict(spec.env),
        "env_mode": spec.env_mode,
        "cwd": None if spec.cwd is None else str(spec.cwd),
        "allow": spec.allow,
        "bind": spec.bind,
        "receipt": str(spec.receipt_path),
    }
    spec_path = spec.receipt_path.with_name("launch-spec.json")
    spec_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - a fixed interpreter and this repository's own source.
        [sys.executable, "-c", LAUNCHER_SOURCE, str(spec_path)],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def read_receipt(path: pathlib.Path) -> Receipt:
    """Read the receipt the launcher wrote to *path*."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Receipt(
        pid=typ.cast("int", payload["pid"]),
        program=typ.cast("str", payload["program"]),
        executable=typ.cast("str", payload["executable"]),
    )


def wait_for_file(path: pathlib.Path, *, timeout: float = 30.0) -> None:
    """Block until *path* exists, failing with the path when it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(_POLL_SECONDS)
    message = f"timed out after {timeout}s waiting for {path}"
    raise AssertionError(message)


def read_worker_report(path: pathlib.Path) -> WorkerReport:
    """Read the report the worker wrote to *path*."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return WorkerReport(
        pid=typ.cast("int", payload["pid"]),
        sid=typ.cast("int", payload["sid"]),
        pgid=typ.cast("int", payload["pgid"]),
        argv=tuple(typ.cast("list[str]", payload["argv"])),
        cwd=typ.cast("str", payload["cwd"]),
        env=typ.cast("dict[str, str]", payload["env"]),
        stdin_eof=bool(payload["stdin_eof"]),
        fd_targets=typ.cast("dict[str, str | None]", payload["fd_targets"]),
    )


def is_running(pid: int) -> bool:
    """Report whether *pid* names a live, unreaped process."""
    state = process_state(pid)
    if state.available:
        return not state.exited
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(pid: int, *, timeout: float = 10.0) -> None:
    """Signal *pid*'s group and wait for it to stop running.

    The worker is not this process's child, so it cannot be reaped from here;
    waiting for it to disappear from the process table is the strongest
    available confirmation. ``SIGKILL`` is used because the worker ignores
    nothing and the case wants it gone, not asked to leave.

    Raises
    ------
    AssertionError
        If the worker is still running when the timeout expires.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            return
        time.sleep(_POLL_SECONDS)
    message = f"worker {pid} was still running {timeout}s after SIGKILL"
    raise AssertionError(message)
