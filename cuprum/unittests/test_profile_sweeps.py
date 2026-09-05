"""Tests for read-size planning and sweep execution."""

from __future__ import annotations

import typing as typ

from benchmarks import profile_tee_hotpath, tee_profile_driver
from benchmarks.profile_tee_hotpath import TeeProfileDriverConfig

if typ.TYPE_CHECKING:
    import pathlib as pth

    import pytest


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


def test_profile_plan_lists_each_sweep_measurement(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plans list every read-size and round that a sweep will measure."""
    monkeypatch.setattr(profile_tee_hotpath, "can_use_rust_backend", lambda: False)
    config = _config(tmp_path, read_sizes=(4096, 65536))

    plan = profile_tee_hotpath.run_profile_plan(config=config)
    entries = typ.cast("list[dict[str, object]]", plan["scenarios"])
    matching = [entry for entry in entries if entry["name"] == config.scenario_name]
    assert config.output_dir is not None
    assert config.scenario_name is not None

    assert [entry["read_size"] for entry in matching] == [
        4096,
        65536,
        4096,
        65536,
    ], f"plan must enumerate every configured sweep sample, got {matching}"
    assert [entry["profile_dir"] for entry in matching] == [
        str(config.output_dir / config.scenario_name / "read-size-4096" / "round-01"),
        str(config.output_dir / config.scenario_name / "read-size-65536" / "round-01"),
        str(config.output_dir / config.scenario_name / "read-size-4096" / "round-02"),
        str(config.output_dir / config.scenario_name / "read-size-65536" / "round-02"),
    ], f"plan must use the sweep's actual artefact directories, got {matching}"


def test_canonical_profile_plan_uses_configured_read_size(tmp_path: pth.Path) -> None:
    """The canonical plan forwards its configured singleton read size."""
    config = _config(tmp_path, read_sizes=(4096,))

    plan = tee_profile_driver.run_profile_plan(config=config)
    entries = typ.cast("list[dict[str, object]]", plan["scenarios"])

    assert {entry["read_size"] for entry in entries} == {4096}, (
        f"canonical plan must report the configured size, got {entries}"
    )


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
