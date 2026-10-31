"""The two interpreters a detached launch case needs, and how to start them.

The worker is the process under observation: it reports what it can see about
itself, then keeps publishing a heartbeat until something stops it. The
launcher starts that worker through cuprum and exits, because surviving its
launcher is the property under test and an in-process launcher cannot exit
without taking the test with it.

This module only produces and starts those programs. Reading their records, and
waiting for them, belong to :mod:`tests.helpers.detached`.
"""

from __future__ import annotations

import dataclasses as dc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the launcher is this repository's own source, run with a fixed argv.
import sys
import typing as typ

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

__all__ = [
    "LAUNCHER_SOURCE",
    "WORKER_SOURCE",
    "LaunchSpec",
    "heartbeat_path",
    "run_launcher",
]

#: How often the worker publishes a heartbeat once its report is written. Short
#: enough that a case can observe several beats after the launcher has exited,
#: long enough not to spin a core while it waits to be stopped.
_WORKER_BEAT_SECONDS = 0.05

#: The worker. It reports what it can see about itself, then keeps publishing a
#: heartbeat until something stops it. ``argv[1]`` is the report path; anything
#: after it is passed through so a caller can prove argument boundaries survive
#: the spawn.
WORKER_SOURCE = "\n".join((
    "import json, os, pathlib, sys, time",
    "report_path = pathlib.Path(sys.argv[1])",
    "beat_path = report_path.with_name(report_path.name + '.beat')",
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
    "beat = 0",
    "while True:",
    "    beat += 1",
    "    tmp_beat = beat_path.with_name(beat_path.name + '.tmp')",
    "    tmp_beat.write_text(str(beat))",
    "    os.replace(tmp_beat, beat_path)",
    f"    time.sleep({_WORKER_BEAT_SECONDS})",
))

#: The launcher. It resolves the spec, launches one command through cuprum, and
#: writes the receipt. Exiting is the point: whatever the worker does next
#: happens after the process that started it is gone. ``spec['aio']`` selects
#: the asynchronous entry point and runs it on a loop that is closed when the
#: launcher exits, so survival is not a property of the loop either.
LAUNCHER_SOURCE = "\n".join((
    "import asyncio, json, pathlib, sys",
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
    "async def launch_async():",
    "    with scoped(scope):",
    "        return await builder(*spec['argv']).launch_detached(context=context)",
    "def start():",
    "    if spec['aio']:",
    "        return asyncio.run(launch_async())",
    "    return launch()",
    "if spec['bind'] is None:",
    "    receipt = start()",
    "else:",
    "    with bind_executable(program, spec['bind']):",
    "        receipt = start()",
    "pathlib.Path(spec['receipt']).write_text(json.dumps({",
    "    'pid': receipt.pid,",
    "    'program': str(receipt.program),",
    "    'executable': receipt.executable,",
    "    'entry_point': 'async' if spec['aio'] else 'sync',",
    "}))",
))


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
    aio:
        Whether the launcher awaits ``launch_detached`` on an ``asyncio`` loop,
        which ``asyncio.run`` closes when the launch returns, instead of
        calling the synchronous entry point.
    """

    argv: tuple[str, ...]
    receipt_path: pathlib.Path
    env: cabc.Mapping[str, str | None] = dc.field(default_factory=dict)
    env_mode: str = "overlay"
    cwd: pathlib.Path | None = None
    allow: bool = True
    bind: str | None = None
    aio: bool = False


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
        "aio": spec.aio,
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


def heartbeat_path(report_path: pathlib.Path) -> pathlib.Path:
    """Return the file the worker publishes its running heartbeat to."""
    return report_path.with_name(report_path.name + ".beat")
