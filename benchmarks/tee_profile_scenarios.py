"""Scenario composition for Cuprum tee hot-path profiling."""

from __future__ import annotations

import dataclasses as dc
import pathlib as pth
import sys
import typing as typ

from benchmarks._benchmark_type_validators import (
    _validate_iteration_count,
    _validate_minimum_int,
)
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig
from cuprum import is_rust_available as can_use_rust_backend
from cuprum._streams_pump import _READ_SIZE

if typ.TYPE_CHECKING:
    import argparse

    from benchmarks.sinks import SinkKind
    from benchmarks.tee_profile_worker import BackendName, TeeMode

type ProfilerName = typ.Literal["none", "perf", "py-spy"]

_DEFAULT_OUTPUT_DIR = pth.Path("dist/profiles")
_DEFAULT_FIXTURE = pth.Path("dist/fixtures/seed12345-nowrap.b64")
_DEFAULT_WRAPPED_FIXTURE = pth.Path("dist/fixtures/seed12345-wrap76.b64")


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
    read_size: int = _READ_SIZE
    encoding: str = "utf-8"
    errors: str = "replace"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable scenario mapping.

        Returns
        -------
        dict[str, object]
            The scenario fields as a JSON-serializable mapping.
        """
        return {
            "name": self.name,
            "fixture_path": str(self.fixture_path),
            "stages": self.stages,
            "mode": self.mode,
            "sink_kind": self.sink_kind,
            "with_line_callbacks": self.with_line_callbacks,
            "backend": self.backend,
            "repeat_count": self.repeat_count,
            "read_size": self.read_size,
            "encoding": self.encoding,
            "errors": self.errors,
        }

    def worker_config(
        self, *, repeat_count: int | None = None
    ) -> TeeProfileWorkerConfig:
        """Convert this scenario into a worker configuration.

        Parameters
        ----------
        repeat_count : int | None
            Optional repeat count; when supplied it overrides the
            scenario's stored repeat count.

        Returns
        -------
        TeeProfileWorkerConfig
            The worker configuration derived from this scenario.
        """
        return TeeProfileWorkerConfig(
            fixture_path=self.fixture_path,
            stages=self.stages,
            mode=self.mode,
            sink_kind=self.sink_kind,
            with_line_callbacks=self.with_line_callbacks,
            backend=self.backend,
            repeat_count=self.repeat_count if repeat_count is None else repeat_count,
            read_size=self.read_size,
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
    read_sizes: tuple[int, ...] = (_READ_SIZE,)
    rounds: int = 1
    randomize_order: bool = False
    perf_frequency: int = 999
    perf_call_graph: str = "dwarf,16384"
    scenario_name: str | None = None

    def _validate_numeric_bounds(self) -> None:
        _validate_iteration_count(
            self.warmup_count,
            name="warmup-count",
            min_value=0,
        )
        _validate_iteration_count(
            self.repeat_count,
            name="repeat-count",
            min_value=1,
        )
        _validate_iteration_count(
            self.rounds,
            name="rounds",
            min_value=1,
        )
        _validate_minimum_int(
            self.perf_frequency,
            name="perf-frequency",
            min_value=1,
        )
        if not self.read_sizes:
            msg = "read-sizes must contain at least one positive integer"
            raise ValueError(msg)
        for read_size in self.read_sizes:
            _validate_minimum_int(read_size, name="read-size", min_value=1)

    def _validate_string_fields(self) -> None:
        if not self.perf_call_graph.strip():
            msg = "perf-call-graph must be a non-empty string"
            raise ValueError(msg)

    def __post_init__(self) -> None:
        """Validate driver configuration."""
        self._validate_numeric_bounds()
        self._validate_string_fields()


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
        TeeProfileScenario(
            name="capture-devnull-nocb-s1",
            fixture_path=fixture_path,
            stages=1,
            mode="capture",
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
    """Return multi-stage scenarios for Python/Rust backend comparison.

    Returns
    -------
    tuple[TeeProfileScenario, ...]
        The multi-stage backend-comparison scenarios. The tuple always
        includes the Python scenario; the Rust scenario is included only when
        ``can_use_rust_backend()`` returns ``True``, so a Python-only tuple is
        returned when Rust is unavailable.
    """
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
    read_size: int = _READ_SIZE,
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
    scenarios = (
        *_single_stage_no_callback_scenarios(fixture_path, repeat_count=repeat_count),
        *_line_callback_scenarios(wrapped_fixture_path, repeat_count=repeat_count),
        *_multi_stage_backend_scenarios(fixture_path, repeat_count=repeat_count),
    )
    return tuple(dc.replace(scenario, read_size=read_size) for scenario in scenarios)


def _resolved_read_size(
    config: TeeProfileDriverConfig,
    *,
    read_size: int | None,
) -> int:
    """Return an explicit read size or the sole configured value."""
    if read_size is not None:
        return read_size
    if len(config.read_sizes) != 1:
        msg = "one read size is required outside a sweep"
        raise ValueError(msg)
    return config.read_sizes[0]


def _named_scenario(
    scenarios: tuple[TeeProfileScenario, ...],
    *,
    scenario_name: str | None,
) -> TeeProfileScenario:
    """Return the named scenario from an ordered scenario matrix."""
    if scenario_name is None:
        msg = "scenario name is required"
        raise ValueError(msg)
    scenarios_by_name = {scenario.name: scenario for scenario in scenarios}
    try:
        return scenarios_by_name[scenario_name]
    except KeyError:
        valid = ", ".join(scenario.name for scenario in scenarios)
        msg = f"unknown scenario {scenario_name!r}; expected one of: {valid}"
        raise ValueError(msg) from None


def _scenario_by_name(
    config: TeeProfileDriverConfig,
    *,
    read_size: int | None = None,
) -> TeeProfileScenario:
    """Resolve one scenario from the configured matrix."""
    resolved_read_size = _resolved_read_size(config, read_size=read_size)
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
        read_size=resolved_read_size,
    )
    return _named_scenario(scenarios, scenario_name=config.scenario_name)


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
        "--read-size",
        str(scenario.read_size),
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
        read_sizes=args.read_sizes,
        rounds=args.rounds,
        randomize_order=args.randomize_order,
        perf_frequency=args.perf_frequency,
        perf_call_graph=args.perf_call_graph,
        scenario_name=getattr(args, "scenario", None),
    )
