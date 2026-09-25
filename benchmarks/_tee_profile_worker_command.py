"""Command construction for the tee hot-path profiling worker.

This module owns building the Cuprum ``SafeCmd``/``Pipeline`` that replays a
fixture through zero or more pass-through stages, converting worker mode into
Cuprum capture/echo flags, reading a neighbouring fixture manifest hash, and
extracting a normalized exit code from a command or pipeline result.
"""

from __future__ import annotations

import dataclasses as dc
import json
import sys
import typing as typ

from cuprum import Program, ProgramCatalogue, ProjectSettings, sh

if typ.TYPE_CHECKING:
    import pathlib as pth

    from benchmarks._tee_profile_worker_config import TeeMode, TeeProfileWorkerConfig

type WorkerCommandResult = sh.CommandResult | sh.PipelineResult

__all__ = ["WorkerCommandResult"]


@dc.dataclass(frozen=True, slots=True)
class _WorkerCommand:
    """Built command plus allowlist required to execute it."""

    cmd: sh.SafeCmd | sh.Pipeline
    allowlist: frozenset[Program]


def _writer_script() -> str:
    """Return the fixture replay script."""
    return "\n".join(
        [
            "import sys",
            "path = sys.argv[1]",
            "out = sys.stdout.buffer",
            "with open(path, 'rb') as source:",
            "    while True:",
            "        chunk = source.read(65536)",
            "        if not chunk:",
            "            break",
            "        out.write(chunk)",
            "out.flush()",
        ],
    )


def _passthrough_script() -> str:
    """Return the intermediate pass-through script."""
    return "\n".join(
        [
            "import shutil",
            "import sys",
            "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer, 65536)",
            "sys.stdout.buffer.flush()",
        ],
    )


def _catalogue_for_worker() -> tuple[ProgramCatalogue, Program]:
    """Create a benchmark-specific catalogue for the current Python executable."""
    python_program = Program(sys.executable)
    project = ProjectSettings(
        name="tee-profile-worker",
        programs=(python_program,),
        documentation_locations=("benchmarks/README.md",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,)), python_program


def _build_command(
    config: TeeProfileWorkerConfig,
    *,
    catalogue: ProgramCatalogue | None = None,
    python_program: Program | None = None,
) -> _WorkerCommand:
    """Build a single-stage command or multi-stage pipeline."""
    if catalogue is None or python_program is None:
        catalogue, python_program = _catalogue_for_worker()
    python = sh.make(python_program, catalogue=catalogue)
    writer = python("-c", _writer_script(), str(config.fixture_path))
    allowlist = frozenset([python_program])
    if config.stages == 1:
        return _WorkerCommand(cmd=writer, allowlist=allowlist)

    command: sh.SafeCmd | sh.Pipeline = writer | python("-c", _passthrough_script())
    for _ in range(config.stages - 2):
        command |= python("-c", _passthrough_script())
    return _WorkerCommand(cmd=command, allowlist=allowlist)


def _capture_and_echo_flags(mode: TeeMode) -> tuple[bool, bool]:
    """Convert worker mode into Cuprum capture and echo flags."""
    if mode == "echo":
        return False, True
    if mode == "capture":
        return True, False
    if mode == "tee":
        return True, True
    typ.assert_never(mode)


def _manifest_hash(fixture_path: pth.Path) -> str | None:
    """Read a neighbouring manifest hash when one is available."""
    manifest_path = fixture_path.with_suffix(".json")
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("sha256")
    return value if isinstance(value, str) else None


def _result_exit_code(result: WorkerCommandResult) -> int:
    """Return the failing exit code for a command or pipeline result."""
    if result.ok:
        return 0
    if isinstance(result, sh.PipelineResult):
        failure = result.failure
        return 0 if failure is None else int(failure.exit_code)
    return int(result.exit_code)
