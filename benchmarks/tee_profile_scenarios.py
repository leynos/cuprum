"""Scenario composition for Cuprum tee hot-path profiling."""

from __future__ import annotations

import dataclasses as dc
import pathlib as pth
import sys
import typing as typ

from benchmarks.tee_profile_worker import TeeProfileWorkerConfig
from cuprum import is_rust_available

if typ.TYPE_CHECKING:
    import argparse

    from benchmarks.sinks import SinkKind
    from benchmarks.tee_profile_worker import BackendName, TeeMode

type ProfilerName = typ.Literal["none", "perf", "py-spy"]

_DEFAULT_OUTPUT_DIR = pth.Path("dist/profiles")
_DEFAULT_FIXTURE = pth.Path("dist/fixtures/seed12345-nowrap.b64")
_DEFAULT_WRAPPED_FIXTURE = pth.Path("dist/fixtures/seed12345-wrap76.b64")


def can_use_rust_backend() -> bool:
    """Return whether Rust backend scenarios can run in this environment.

    Returns
    -------
    bool
        ``True`` when the Cuprum Rust extension is importable in the current
        environment; ``False`` otherwise.
    """
    return is_rust_available()


@dc.dataclass(frozen=True, slots=True)
class TeeProfileScenario:
    """One resolved tee profiling scenario."""

    name: str
    fixture_path: pth.Path
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    encoding: str = "utf-8"
    errors: str = "replace"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable scenario mapping."""
        return {
            "name": self.name,
            "fixture_path": str(self.fixture_path),
            "stages": self.stages,
            "mode": self.mode,
            "sink_kind": self.sink_kind,
            "with_line_callbacks": self.with_line_callbacks,
            "backend": self.backend,
            "repeat_count": self.repeat_count,
            "encoding": self.encoding,
            "errors": self.errors,
        }

    def worker_config(
        self, *, repeat_count: int | None = None
    ) -> TeeProfileWorkerConfig:
        """Convert this scenario into a worker configuration."""
        return TeeProfileWorkerConfig(
            fixture_path=self.fixture_path,
            stages=self.stages,
            mode=self.mode,
            sink_kind=self.sink_kind,
            with_line_callbacks=self.with_line_callbacks,
            backend=self.backend,
            repeat_count=self.repeat_count if repeat_count is None else repeat_count,
            encoding=self.encoding,
            errors=self.errors,
        )


@dc.dataclass(frozen=True, slots=True)
class TeeProfileDriverConfig:
    """Configuration for scenario planning and execution."""

    fixture_path: pth.Path = _DEFAULT_FIXTURE
    wrapped_fixture_path: pth.Path = _DEFAULT_WRAPPED_FIXTURE
    output_dir: pth.Path = _DEFAULT_OUTPUT_DIR
    profiler: ProfilerName = "none"
    warmup_count: int = 1
    repeat_count: int = 3
    perf_frequency: int = 999
    perf_call_graph: str = "dwarf,16384"
    scenario_name: str | None = None

    def __post_init__(self) -> None:
        """Validate driver configuration."""
        int_bounds: tuple[tuple[str, int, int], ...] = (
            ("warmup-count", self.warmup_count, 0),
            ("repeat-count", self.repeat_count, 1),
            ("perf-frequency", self.perf_frequency, 1),
        )
        for name, value, minimum in int_bounds:
            if value < minimum:
                msg = f"{name} must be >= {minimum}, got {value}"
                raise ValueError(msg)
        if not self.perf_call_graph.strip():
            msg = "perf-call-graph must be a non-empty string"
            raise ValueError(msg)


def _single_stage_no_callback_scenarios(
    fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return single-stage, no-callback scenarios across sinks and modes."""
    return (
        TeeProfileScenario(
            name="echo-devnull-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="echo-textblackhole-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="text_blackhole",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="echo-pty-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="echo",
            sink_kind="pty_blackhole",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
        TeeProfileScenario(
            name="tee-devnull-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="auto",
            repeat_count=repeat_count,
        ),
    )


def _line_callback_scenarios(
    wrapped_fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return single-stage line-callback scenarios using the wrapped fixture."""
    return (
        TeeProfileScenario(
            name="echo-devnull-cb-s1",
            fixture_path=wrapped_fixture_path,
            stages=1,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="auto",
            repeat_count=repeat_count,
        ),
    )


def _multi_stage_backend_scenarios(
    fixture_path: pth.Path,
    *,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return multi-stage scenarios for Python/Rust backend comparison."""
    python_scenario = TeeProfileScenario(
        name="echo-devnull-nocb-s4-python",
        fixture_path=fixture_path,
        stages=4,
        mode="echo",
        sink_kind="devnull",
        with_line_callbacks=False,
        backend="python",
        repeat_count=repeat_count,
    )
    if not can_use_rust_backend():
        return (python_scenario,)
    return (
        python_scenario,
        TeeProfileScenario(
            name="echo-devnull-nocb-s4-rust",
            fixture_path=fixture_path,
            stages=4,
            mode="echo",
            sink_kind="devnull",
            with_line_callbacks=False,
            backend="rust",
            repeat_count=repeat_count,
        ),
    )


def default_tee_profile_scenarios(
    *,
    fixture_path: pth.Path,
    wrapped_fixture_path: pth.Path,
    repeat_count: int,
) -> tuple[TeeProfileScenario, ...]:
    """Return the required initial tee profiling scenario matrix.

    Parameters
    ----------
    fixture_path:
        Path to the unwrapped base64 fixture used by most scenarios.
    wrapped_fixture_path:
        Path to the wrap-76 fixture used by line-callback scenarios.
    repeat_count:
        Measured repeat count applied to every scenario.

    Returns
    -------
    tuple[TeeProfileScenario, ...]
        Ordered tuple of default profiling scenarios. The Rust-backend scenario
        omitted when ``can_use_rust_backend()`` returns ``False``.
    """
    return (
        *_single_stage_no_callback_scenarios(fixture_path, repeat_count=repeat_count),
        *_line_callback_scenarios(wrapped_fixture_path, repeat_count=repeat_count),
        *_multi_stage_backend_scenarios(fixture_path, repeat_count=repeat_count),
    )


def _scenario_by_name(config: TeeProfileDriverConfig) -> TeeProfileScenario:
    """Resolve one scenario from the configured matrix."""
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    )
    if config.scenario_name is None:
        msg = "scenario name is required"
        raise ValueError(msg)
    for scenario in scenarios:
        if scenario.name == config.scenario_name:
            return scenario
    valid = ", ".join(scenario.name for scenario in scenarios)
    msg = f"unknown scenario {config.scenario_name!r}; expected one of: {valid}"
    raise ValueError(msg)


def _worker_command(scenario: TeeProfileScenario) -> list[str]:
    """Build an equivalent worker command for plans and manual reruns."""
    command = [
        sys.executable,
        "-m",
        "benchmarks.tee_profile_worker",
        "--fixture",
        str(scenario.fixture_path),
        "--stages",
        str(scenario.stages),
        "--mode",
        scenario.mode,
        "--sink-kind",
        scenario.sink_kind,
        "--backend",
        scenario.backend,
        "--repeat-count",
        str(scenario.repeat_count),
        "--encoding",
        scenario.encoding,
        "--errors",
        scenario.errors,
    ]
    if scenario.with_line_callbacks:
        command.append("--line-callbacks")
    return command


def _config_from_args(args: argparse.Namespace) -> TeeProfileDriverConfig:
    """Convert parsed arguments to driver configuration."""
    return TeeProfileDriverConfig(
        fixture_path=args.fixture,
        wrapped_fixture_path=args.wrapped_fixture,
        output_dir=args.output_dir,
        profiler=args.profiler,
        warmup_count=args.warmup_count,
        repeat_count=args.repeat_count,
        perf_frequency=args.perf_frequency,
        perf_call_graph=args.perf_call_graph,
        scenario_name=getattr(args, "scenario", None),
    )
