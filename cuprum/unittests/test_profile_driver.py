"""Tests for the tee hot-path profile driver, plan, and CLI."""

from __future__ import annotations

import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - integration tests exercise fixed CLI commands.
import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchmarks import profile_tee_hotpath
from benchmarks.profile_tee_hotpath import (
    TeeProfileDriverConfig,
    default_tee_profile_scenarios,
    run_profile_plan,
)
from benchmarks.tee_profile_scenarios import (
    _named_scenario,
    _resolved_read_size,
    _scenario_by_name,
)
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


_SCENARIO_NAMES_WITH_RUST: list[str] = [
    "echo-devnull-nocb-s1",
    "echo-textblackhole-nocb-s1",
    "echo-pty-nocb-s1",
    "tee-devnull-nocb-s1",
    "capture-devnull-nocb-s1",
    "echo-devnull-cb-s1",
    "echo-devnull-nocb-s4-python",
    "echo-devnull-nocb-s4-rust",
]

_SCENARIO_NAMES_WITHOUT_RUST: list[str] = [
    "echo-devnull-nocb-s1",
    "echo-textblackhole-nocb-s1",
    "echo-pty-nocb-s1",
    "tee-devnull-nocb-s1",
    "capture-devnull-nocb-s1",
    "echo-devnull-cb-s1",
    "echo-devnull-nocb-s4-python",
]


def _scenario_config(
    tmp_path: pth.Path,
    *,
    scenario_name: str | None,
    read_sizes: tuple[int, ...],
) -> TeeProfileDriverConfig:
    """Build a minimal config for private scenario-selection tests."""
    return TeeProfileDriverConfig(
        fixture_path=tmp_path / "fixture.b64",
        wrapped_fixture_path=tmp_path / "fixture-wrap76.b64",
        output_dir=tmp_path / "profiles",
        profiler="none",
        read_sizes=read_sizes,
        scenario_name=scenario_name,
    )


def test_scenario_lookup_explicit_read_size_overrides_configured_values(
    tmp_path: pth.Path,
) -> None:
    """An explicit selection read size overrides a configured sweep."""
    scenario = _scenario_by_name(
        _scenario_config(
            tmp_path,
            scenario_name="echo-devnull-nocb-s1",
            read_sizes=(4096, 16384),
        ),
        read_size=65536,
    )

    assert scenario.read_size == 65536, "explicit read size must win"


def test_resolved_read_size_requires_one_configured_value(tmp_path: pth.Path) -> None:
    """Implicit selection rejects a configuration containing a sweep."""
    config = _scenario_config(
        tmp_path,
        scenario_name="echo-devnull-nocb-s1",
        read_sizes=(4096, 16384),
    )

    with pytest.raises(
        ValueError,
        match=re.escape("one read size is required outside a sweep"),
    ):
        _resolved_read_size(config, read_size=None)


def test_named_scenario_requires_name() -> None:
    """Missing scenario names retain their established error contract."""
    with pytest.raises(ValueError, match=re.escape("scenario name is required")):
        _named_scenario((), scenario_name=None)


def test_named_scenario_reports_ordered_valid_names(tmp_path: pth.Path) -> None:
    """Unknown names retain the ordered scenario-list error contract."""
    config = _scenario_config(
        tmp_path,
        scenario_name="does-not-exist",
        read_sizes=(4096,),
    )
    scenarios = default_tee_profile_scenarios(
        fixture_path=config.fixture_path,
        wrapped_fixture_path=config.wrapped_fixture_path,
        repeat_count=config.repeat_count,
    )
    valid = ", ".join(scenario.name for scenario in scenarios)
    expected_message = f"unknown scenario 'does-not-exist'; expected one of: {valid}"

    with pytest.raises(
        ValueError,
        match=re.escape(expected_message),
    ):
        _named_scenario(scenarios, scenario_name=config.scenario_name)


@pytest.mark.parametrize(
    ("rust_available", "expected_names"),
    [
        pytest.param(True, _SCENARIO_NAMES_WITH_RUST, id="rust-available"),
        pytest.param(False, _SCENARIO_NAMES_WITHOUT_RUST, id="rust-unavailable"),
    ],
)
def test_profile_plan_scenario_matrix(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    rust_available: bool,
    expected_names: list[str],
) -> None:
    """The default plan includes or omits the Rust scenario.

    Backend availability controls whether the Rust scenario is present.
    """
    monkeypatch.setattr(
        profile_tee_hotpath, "can_use_rust_backend", lambda: rust_available
    )
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=1,
        repeat_count=3,
    )

    plan = run_profile_plan(config=config)
    scenarios = typ.cast("list[dict[str, object]]", plan["scenarios"])

    assert [scenario["name"] for scenario in scenarios] == expected_names, (
        f"expected scenario matrix for rust_available={rust_available!r}, "
        f"got {scenarios}"
    )


def test_run_profile_plan_snapshot(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: SnapshotAssertion,
) -> None:
    """run_profile_plan output structure matches snapshot."""
    monkeypatch.setattr(profile_tee_hotpath, "can_use_rust_backend", lambda: False)
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=1,
        repeat_count=1,
    )
    plan = run_profile_plan(config=config)
    assert redact(plan, _VOLATILE_KEYS) == snapshot


def test_default_scenarios_use_requested_repeat_count(tmp_path: pth.Path) -> None:
    """Scenario expansion keeps fixed repeat counts across the matrix."""
    fixture = tmp_path / "fixture.b64"
    wrapped = tmp_path / "fixture-wrap76.b64"

    scenarios = default_tee_profile_scenarios(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        repeat_count=7,
    )

    assert {scenario.repeat_count for scenario in scenarios} == {7}, (
        f"expected every scenario repeat count to be 7, got {scenarios}"
    )


@given(repeat_count=st.integers(min_value=1, max_value=10))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_scenario_matrix_order_is_stable(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    repeat_count: int,
) -> None:
    """default_tee_profile_scenarios always returns scenarios in the same order.

    The scenario matrix order is a stable contract: callers and snapshot tests
    depend on it. This property test verifies that varying ``repeat_count``
    never reorders the scenarios.
    """
    monkeypatch.setattr(profile_tee_hotpath, "can_use_rust_backend", lambda: False)
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    scenarios = default_tee_profile_scenarios(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        repeat_count=repeat_count,
    )
    names = [s.name for s in scenarios]
    expected_prefix = [
        "echo-devnull-nocb-s1",
        "echo-textblackhole-nocb-s1",
        "echo-pty-nocb-s1",
        "tee-devnull-nocb-s1",
        "capture-devnull-nocb-s1",
        "echo-devnull-cb-s1",
        "echo-devnull-nocb-s4-python",
    ]
    assert names == expected_prefix, (
        f"expected stable scenario order {expected_prefix}, got {names}"
    )


def test_profile_matrix_stops_after_first_worker_failure(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile matrix execution stops after the first failing scenario."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=0,
        repeat_count=1,
    )
    observed: list[str | None] = []

    def fail_scenario(*, config: TeeProfileDriverConfig) -> dict[str, object]:
        """Record the scenario name and report a failed run.

        Returns
        -------
        dict[str, object]
            A result payload marking the scenario as failed.
        """
        observed.append(config.scenario_name)
        return {"status": "failed", "exit_code": 9}

    monkeypatch.setattr(profile_tee_hotpath, "run_profile_scenario", fail_scenario)

    results = profile_tee_hotpath.run_profile_matrix(config=config)

    assert observed == ["echo-devnull-nocb-s1"], (
        f"expected matrix to stop after first scenario, got {observed}"
    )
    assert results == [{"status": "failed", "exit_code": 9}], (
        f"expected only first failure result, got {results}"
    )


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"warmup_count": -1},
            "warmup-count must be >= 0",
            id="warmup-negative",
        ),
        pytest.param(
            {"repeat_count": 0},
            "repeat-count must be >= 1",
            id="repeat-zero",
        ),
        pytest.param(
            {"warmup_count": 1001},
            "warmup-count must be <= 1000",
            id="warmup-too-large",
        ),
        pytest.param(
            {"repeat_count": 1001},
            "repeat-count must be <= 1000",
            id="repeat-too-large",
        ),
        pytest.param(
            {"perf_frequency": 0},
            "perf-frequency must be >= 1",
            id="perf-freq-zero",
        ),
        pytest.param(
            {"perf_call_graph": "  "},
            "perf-call-graph must be a non-empty string",
            id="blank-call-graph",
        ),
    ],
)
def test_driver_config_rejects_invalid_fields(
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """TeeProfileDriverConfig raises ValueError for invalid numeric/string fields."""
    warmup_count = kwargs.get("warmup_count", 1)
    repeat_count = kwargs.get("repeat_count", 3)
    perf_frequency = kwargs.get("perf_frequency", 999)
    perf_call_graph = kwargs.get("perf_call_graph", "dwarf,16384")
    assert isinstance(warmup_count, int)
    assert isinstance(repeat_count, int)
    assert isinstance(perf_frequency, int)
    assert isinstance(perf_call_graph, str)
    with pytest.raises(ValueError, match=fragment):
        TeeProfileDriverConfig(
            warmup_count=warmup_count,
            repeat_count=repeat_count,
            perf_frequency=perf_frequency,
            perf_call_graph=perf_call_graph,
        )


def test_worker_command_uses_module_invocation(tmp_path: pth.Path) -> None:
    """_worker_command produces a -m benchmarks.tee_profile_worker invocation."""
    from benchmarks.profile_tee_hotpath import TeeProfileScenario, _worker_command

    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    scenario = TeeProfileScenario(
        name="echo-devnull-nocb-s1",
        fixture_path=fixture,
        stages=1,
        mode="echo",
        sink_kind="devnull",
        with_line_callbacks=False,
        backend="python",
        repeat_count=1,
        read_size=16384,
    )
    cmd = _worker_command(scenario)

    assert cmd[1] == "-m", f"expected -m flag at index 1, got {cmd}"
    assert cmd[2] == "benchmarks.tee_profile_worker", (
        f"expected module name at index 2, got {cmd}"
    )
    read_size_index = cmd.index("--read-size")
    assert cmd[read_size_index : read_size_index + 2] == ["--read-size", "16384"], (
        f"expected worker command to propagate read size, got {cmd}"
    )


def test_profile_sweep_runs_every_size_in_each_round(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-size sweeps preserve every configured measurement point per round."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=0,
        repeat_count=1,
        read_sizes=(4096, 16384),
        rounds=2,
        randomize_order=True,
        scenario_name="tee-devnull-nocb-s1",
    )
    observed: list[int] = []

    def fake_run(
        scenario: profile_tee_hotpath.TeeProfileScenario,
        *,
        config: TeeProfileDriverConfig,
        scenario_dir: pth.Path,
    ) -> dict[str, object]:
        """Record the scenario's read size without running a subprocess."""
        _ = (config, scenario_dir)
        observed.append(scenario.read_size)
        return {"exit_code": 0, "read_size": scenario.read_size, "status": "ok"}

    monkeypatch.setattr(profile_tee_hotpath, "_run_profile_scenario", fake_run)

    results = profile_tee_hotpath.run_profile_sweep(config=config)

    assert len(results) == 4, f"expected four sweep samples, got {results}"
    assert set(observed[:2]) == {4096, 16384}, (
        f"first round must visit each size exactly once, got {observed}"
    )
    assert set(observed[2:]) == {4096, 16384}, (
        f"second round must visit each size exactly once, got {observed}"
    )


def _run_profile_cli(*args: str) -> int:
    """Invoke benchmarks.profile_tee_hotpath via subprocess and return its exit code."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-m", "benchmarks.profile_tee_hotpath", *args],
        check=False,
    )
    return completed.returncode


@pytest.mark.parametrize(
    ("subcommand_args", "description"),
    [
        pytest.param(
            ("run-scenario", "--scenario", "echo-devnull-nocb-s1"),
            "run-scenario with missing fixture",
            id="scenario-worker-failure",
        ),
        pytest.param(
            ("run",),
            "run matrix with missing fixtures",
            id="matrix-failure",
        ),
    ],
)
def test_profile_cli_returns_failure_exit_code(
    tmp_path: pth.Path,
    subcommand_args: tuple[str, ...],
    description: str,
) -> None:
    """Profile CLI returns non-zero when the worker fails."""
    missing = tmp_path / "no_such_fixture.b64"
    wrapped = tmp_path / "no_such_wrapped.b64"
    exit_code = _run_profile_cli(
        "--fixture",
        str(missing),
        "--wrapped-fixture",
        str(wrapped),
        "--output-dir",
        str(tmp_path / "profiles"),
        "--profiler",
        "none",
        "--warmup-count",
        "0",
        "--repeat-count",
        "1",
        *subcommand_args,
    )
    assert exit_code != 0, (
        f"expected non-zero exit code for {description}, got {exit_code}"
    )
