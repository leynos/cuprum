"""Worker process for parent-side tee hot-path profiling."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses as dc
import json
import os
import pathlib as pth
import sys
import time
import typing as typ

from benchmarks.sinks import SinkKind, open_sink
from cuprum import (
    ExecEvent,
    ExecutionContext,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    ScopeConfig,
    _backend,
    scoped,
    sh,
)

type TeeMode = typ.Literal["echo", "capture", "tee"]
type BackendName = typ.Literal["auto", "python", "rust"]
type WorkerCommandResult = sh.CommandResult | sh.PipelineResult

_VALID_MODES = {"echo", "capture", "tee"}
_VALID_SINKS = {"devnull", "text_blackhole", "pty_blackhole"}
_VALID_BACKENDS = {"auto", "python", "rust"}


class TeeProfileWorkerResult(typ.TypedDict):
    """Machine-readable tee profiling worker result payload."""

    scenario: str
    fixture_path: str
    fixture_manifest_hash: str | None
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    wall_time_seconds: float
    status: typ.Literal["ok", "failed"]
    exit_code: int
    captured_output_length: int
    stdout_line_count: int


@dc.dataclass(frozen=True, slots=True)
class TeeProfileWorkerConfig:
    """Configuration for one tee profiling worker execution."""

    fixture_path: pth.Path
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    encoding: str = "utf-8"
    errors: str = "replace"

    def __post_init__(self) -> None:
        """Validate worker configuration."""
        self._coerce_fixture_path()
        self._validate_numeric_bounds()
        self._validate_enum_fields()

    def _coerce_fixture_path(self) -> None:
        """Coerce and validate the fixture path."""
        fixture_path = pth.Path(self.fixture_path)
        object.__setattr__(self, "fixture_path", fixture_path)
        if not fixture_path.is_file():
            msg = f"fixture_path must exist and be a file: {fixture_path}"
            raise ValueError(msg)

    def _validate_numeric_bounds(self) -> None:
        """Validate numeric worker bounds."""
        if self.stages < 1:
            msg = f"stages must be >= 1, got {self.stages}"
            raise ValueError(msg)
        if self.repeat_count < 1:
            msg = f"repeat-count must be >= 1, got {self.repeat_count}"
            raise ValueError(msg)

    def _validate_enum_fields(self) -> None:
        """Validate enum-like worker fields."""
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}, got {self.mode!r}"
            raise ValueError(msg)
        if self.sink_kind not in _VALID_SINKS:
            msg = (
                f"sink-kind must be one of {sorted(_VALID_SINKS)}, "
                f"got {self.sink_kind!r}"
            )
            raise ValueError(msg)
        if self.backend not in _VALID_BACKENDS:
            msg = (
                f"backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.backend!r}"
            )
            raise ValueError(msg)


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
) -> _WorkerCommand:
    """Build a single-stage command or multi-stage pipeline."""
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


@contextlib.contextmanager
def _selected_backend(backend: BackendName) -> typ.Iterator[None]:
    """Select the stream backend for parent-side pipeline pumping."""
    previous = os.environ.get("CUPRUM_STREAM_BACKEND")
    if backend == "auto":
        os.environ.pop("CUPRUM_STREAM_BACKEND", None)
    else:
        os.environ["CUPRUM_STREAM_BACKEND"] = backend
    _backend._check_rust_available.cache_clear()
    _backend.get_stream_backend.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CUPRUM_STREAM_BACKEND", None)
        else:
            os.environ["CUPRUM_STREAM_BACKEND"] = previous
        _backend._check_rust_available.cache_clear()
        _backend.get_stream_backend.cache_clear()


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
    except json.JSONDecodeError:
        return None
    value = payload.get("sha256")
    return value if isinstance(value, str) else None


def _run_command_sync(
    config: TeeProfileWorkerConfig,
    worker_cmd: _WorkerCommand,
    *,
    capture: bool,
    echo: bool,
) -> tuple[WorkerCommandResult, int]:
    """Run one configured command synchronously and count stdout line events."""
    line_count = 0

    def observe_line(event: ExecEvent) -> None:
        nonlocal line_count
        if event.phase == "stdout" and event.line is not None:
            line_count += 1

    with open_sink(
        config.sink_kind,
        encoding=config.encoding,
        errors=config.errors,
    ) as sink:
        context = ExecutionContext(
            stdout_sink=sink,
            encoding=config.encoding,
            errors=config.errors,
        )
        with scoped(ScopeConfig(allowlist=worker_cmd.allowlist)):
            if config.with_line_callbacks:
                with sh.observe(observe_line):
                    result = worker_cmd.cmd.run_sync(
                        capture=capture,
                        echo=echo,
                        context=context,
                    )
            else:
                result = worker_cmd.cmd.run_sync(
                    capture=capture,
                    echo=echo,
                    context=context,
                )
    return result, line_count


def _result_exit_code(result: WorkerCommandResult) -> int:
    """Return the failing exit code for a command or pipeline result."""
    if result.ok:
        return 0
    if isinstance(result, sh.PipelineResult):
        failure = result.failure
        return 0 if failure is None else int(failure.exit_code)
    return int(result.exit_code)


def _run_once(config: TeeProfileWorkerConfig) -> tuple[int, int, int]:
    """Run one Cuprum command and return status, exit code, and capture length."""
    worker_cmd = _build_command(config)
    capture, echo = _capture_and_echo_flags(config.mode)
    result, line_count = _run_command_sync(
        config,
        worker_cmd,
        capture=capture,
        echo=echo,
    )

    captured = result.stdout
    captured_len = len(captured) if captured is not None else 0
    return captured_len, _result_exit_code(result), line_count


def run_tee_profile_worker(config: TeeProfileWorkerConfig) -> TeeProfileWorkerResult:
    """Execute a configured tee profiling worker and return a JSON payload."""
    started = time.perf_counter()
    total_captured_len = 0
    total_line_count = 0
    exit_code = 0
    status: typ.Literal["ok", "failed"] = "ok"
    with _selected_backend(config.backend):
        for _ in range(config.repeat_count):
            captured_len, exit_code, line_count = _run_once(config)
            total_captured_len += captured_len
            total_line_count += line_count
            if exit_code != 0:
                status = "failed"
                break
    wall_time = time.perf_counter() - started
    return {
        "scenario": _scenario_label(config),
        "fixture_path": str(config.fixture_path),
        "fixture_manifest_hash": _manifest_hash(config.fixture_path),
        "stages": config.stages,
        "mode": config.mode,
        "sink_kind": config.sink_kind,
        "with_line_callbacks": config.with_line_callbacks,
        "backend": config.backend,
        "repeat_count": config.repeat_count,
        "wall_time_seconds": wall_time,
        "status": status,
        "exit_code": exit_code,
        "captured_output_length": total_captured_len,
        "stdout_line_count": total_line_count,
    }


def _scenario_label(config: TeeProfileWorkerConfig) -> str:
    """Build a compact label for ad hoc worker runs."""
    cb = "cb" if config.with_line_callbacks else "nocb"
    return f"{config.mode}-{config.sink_kind}-{cb}-s{config.stages}-{config.backend}"


def _parse_args() -> argparse.Namespace:
    """Parse worker CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=pth.Path, required=True)
    parser.add_argument("--stages", type=int, required=True)
    parser.add_argument("--mode", choices=sorted(_VALID_MODES), required=True)
    parser.add_argument("--sink-kind", choices=sorted(_VALID_SINKS), required=True)
    parser.add_argument("--line-callbacks", action="store_true")
    parser.add_argument("--backend", choices=sorted(_VALID_BACKENDS), default="auto")
    parser.add_argument("--repeat-count", type=int, default=1)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--errors", default="replace")
    parser.add_argument("--output", type=pth.Path)
    return parser.parse_args()


def main() -> int:
    """Run the tee profiling worker CLI."""
    args = _parse_args()
    try:
        config = TeeProfileWorkerConfig(
            fixture_path=args.fixture,
            stages=args.stages,
            mode=args.mode,
            sink_kind=args.sink_kind,
            with_line_callbacks=args.line_callbacks,
            backend=args.backend,
            repeat_count=args.repeat_count,
            encoding=args.encoding,
            errors=args.errors,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = run_tee_profile_worker(config)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
