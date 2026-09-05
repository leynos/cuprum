"""Tests for read-size planning and sweep execution."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from benchmarks import profile_tee_hotpath, tee_profile_driver, tee_profile_scenarios
from benchmarks.profile_tee_hotpath import TeeProfileDriverConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth
    import types


def _config(
    tmp_path: pth.Path, *, read_sizes: tuple[int, ...]
) -> TeeProfileDriverConfig:
    """Build a profile configuration with local fixtures."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    return TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=0,
        repeat_count=1,
        read_sizes=read_sizes,
        rounds=2,
        randomize_order=True,
        scenario_name="tee-devnull-nocb-s1",
    )


_PLAN_RUNNERS = (
    pytest.param(
        profile_tee_hotpath,
        profile_tee_hotpath.run_profile_plan,
        id="legacy-module",
    ),
    pytest.param(
        tee_profile_scenarios,
        tee_profile_driver.run_profile_plan,
        id="scenario-module",
    ),
)


@pytest.mark.parametrize(("scenario_module", "plan_runner"), _PLAN_RUNNERS)
def test_profile_plan_lists_each_sweep_measurement(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_module: types.ModuleType,
    plan_runner: cabc.Callable[..., cabc.Mapping[str, object]],
) -> None:
    """Plans list every read-size and round that a sweep will measure."""
    monkeypatch.setattr(scenario_module, "can_use_rust_backend", lambda: False)
    config = _config(tmp_path, read_sizes=(4096, 65536))

    plan = plan_runner(config=config)
    entries = typ.cast("list[dict[str, object]]", plan["scenarios"])
    matching = [entry for entry in entries if entry["name"] == config.scenario_name]
    assert config.output_dir is not None
    assert config.scenario_name is not None

    assert [entry["read_size"] for entry in matching] == [
        4096,
        65536,
        4096,
        65536,
    ], f"canonical plan must enumerate every sweep measurement, got {matching}"
    assert [entry["profile_dir"] for entry in matching] == [
        str(config.output_dir / config.scenario_name / "read-size-4096" / "round-01"),
        str(config.output_dir / config.scenario_name / "read-size-65536" / "round-01"),
        str(config.output_dir / config.scenario_name / "read-size-4096" / "round-02"),
        str(config.output_dir / config.scenario_name / "read-size-65536" / "round-02"),
    ], f"canonical plan must use the measurement artefact directories, got {matching}"


def test_profile_sweep_runs_every_size_in_each_round(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-size sweeps preserve every configured measurement point per round."""
    config = _config(tmp_path, read_sizes=(4096, 16384))
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


def test_canonical_profile_sweep_runs_every_size_in_each_round(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical scenario command preserves all requested measurements."""
    config = _config(tmp_path, read_sizes=(4096, 16384))
    observed: list[int] = []

    def fake_run(
        scenario: profile_tee_hotpath.TeeProfileScenario,
        *,
        config: TeeProfileDriverConfig,
        scenario_dir: pth.Path,
    ) -> dict[str, object]:
        """Record the resolved read size without running a subprocess."""
        del config, scenario_dir
        observed.append(scenario.read_size)
        return {"exit_code": 0, "read_size": scenario.read_size, "status": "ok"}

    monkeypatch.setattr(tee_profile_driver, "_run_profile_scenario", fake_run)
    monkeypatch.setattr(
        tee_profile_driver,
        "_shuffle_for",
        lambda _config: lambda read_sizes: read_sizes.reverse(),
    )

    results = tee_profile_driver.run_profile_sweep(config=config)

    assert len(results) == 4, f"expected four sweep samples, got {results}"
    assert observed == [16384, 4096, 16384, 4096], (
        f"canonical scenario sweeps must preserve the composition order, got {observed}"
    )


def test_canonical_profile_matrix_runs_every_size_in_each_round(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical matrix preserves all requested read-size measurements."""
    monkeypatch.setattr(tee_profile_scenarios, "can_use_rust_backend", lambda: False)
    config = _config(tmp_path, read_sizes=(4096, 16384))
    config = dc.replace(config, randomize_order=False)
    observed: list[tuple[int, str]] = []

    def fake_run(
        scenario: profile_tee_hotpath.TeeProfileScenario,
        *,
        config: TeeProfileDriverConfig,
        scenario_dir: pth.Path,
    ) -> dict[str, object]:
        """Record matrix dimensions without starting worker processes."""
        del config
        observed.append((scenario.read_size, str(scenario_dir)))
        return {"exit_code": 0, "status": "ok"}

    monkeypatch.setattr(tee_profile_driver, "_run_profile_scenario", fake_run)

    results = tee_profile_driver.run_profile_matrix(config=config)
    scenario_count = len(
        tee_profile_driver.default_tee_profile_scenarios(
            fixture_path=config.fixture_path,
            wrapped_fixture_path=config.wrapped_fixture_path,
            repeat_count=config.repeat_count,
            read_size=4096,
        )
    )

    assert len(results) == scenario_count * 4, (
        "matrix execution must cover each scenario for every size in every "
        f"round, got {results}"
    )
    assert [read_size for read_size, _ in observed] == (
        [4096] * scenario_count
        + [16384] * scenario_count
        + [4096] * scenario_count
        + [16384] * scenario_count
    ), f"matrix must retain ordered read-size rounds, got {observed}"
    assert all("read-size-" in scenario_dir for _, scenario_dir in observed), (
        f"matrix samples must use dedicated sweep artefact directories, got {observed}"
    )


def test_profile_sweep_uses_injected_shuffle_dependency(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy composition boundary supplies the randomized sweep order."""
    config = _config(tmp_path, read_sizes=(4096, 16384))
    observed: list[int] = []

    def reverse(read_sizes: list[int]) -> None:
        """Use an observable deterministic shuffle double."""
        read_sizes.reverse()

    def fake_run(
        scenario: profile_tee_hotpath.TeeProfileScenario,
        *,
        config: TeeProfileDriverConfig,
        scenario_dir: pth.Path,
    ) -> dict[str, object]:
        """Record the resolved size without running a worker."""
        del config, scenario_dir
        observed.append(scenario.read_size)
        return {"exit_code": 0, "status": "ok"}

    monkeypatch.setattr(profile_tee_hotpath, "_shuffle_for", lambda _config: reverse)
    monkeypatch.setattr(profile_tee_hotpath, "_run_profile_scenario", fake_run)

    profile_tee_hotpath.run_profile_sweep(config=config)

    assert observed == [16384, 4096, 16384, 4096], (
        f"the sweep must use the injected read-size order in each round, got {observed}"
    )
