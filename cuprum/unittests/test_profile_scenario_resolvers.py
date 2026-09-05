"""Tests for shared and legacy profile-scenario resolvers."""

from __future__ import annotations

import re
import typing as typ

import pytest

from benchmarks.profile_tee_hotpath import (
    _named_scenario as _legacy_named_scenario,
)
from benchmarks.profile_tee_hotpath import (
    _resolved_read_size as _legacy_resolved_read_size,
)
from benchmarks.profile_tee_hotpath import (
    _scenario_by_name as _legacy_scenario_by_name,
)
from benchmarks.tee_profile_configuration import (
    _named_scenario,
    _resolved_read_size,
    _scenario_by_name,
)
from benchmarks.tee_profile_scenarios import (
    TeeProfileDriverConfig,
    TeeProfileScenario,
    default_tee_profile_scenarios,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


_SCENARIO_RESOLVERS = (
    pytest.param(_scenario_by_name, id="scenario-module"),
    pytest.param(_legacy_scenario_by_name, id="legacy-module"),
)

_READ_SIZE_RESOLVERS = (
    pytest.param(_resolved_read_size, id="scenario-module"),
    pytest.param(_legacy_resolved_read_size, id="legacy-module"),
)

_NAMED_SCENARIO_RESOLVERS = (
    pytest.param(_named_scenario, id="scenario-module"),
    pytest.param(_legacy_named_scenario, id="legacy-module"),
)


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


@pytest.mark.parametrize("scenario_resolver", _SCENARIO_RESOLVERS)
def test_scenario_lookup_explicit_read_size_overrides_configured_values(
    tmp_path: pth.Path,
    scenario_resolver: cabc.Callable[..., TeeProfileScenario],
) -> None:
    """An explicit read size overrides a configured sweep in both modules."""
    scenario = scenario_resolver(
        _scenario_config(
            tmp_path,
            scenario_name="echo-devnull-nocb-s1",
            read_sizes=(4096, 16384),
        ),
        read_size=65536,
    )

    assert scenario.read_size == 65536, "explicit read size must win"


@pytest.mark.parametrize("read_size_resolver", _READ_SIZE_RESOLVERS)
def test_resolved_read_size_requires_one_configured_value(
    tmp_path: pth.Path,
    read_size_resolver: cabc.Callable[..., int],
) -> None:
    """Implicit selection rejects a configuration containing a sweep in both modules."""
    config = _scenario_config(
        tmp_path,
        scenario_name="echo-devnull-nocb-s1",
        read_sizes=(4096, 16384),
    )

    with pytest.raises(
        ValueError,
        match=re.escape("one read size is required outside a sweep"),
    ):
        read_size_resolver(config, read_size=None)


@pytest.mark.parametrize("named_scenario_resolver", _NAMED_SCENARIO_RESOLVERS)
def test_named_scenario_requires_name(
    named_scenario_resolver: cabc.Callable[..., TeeProfileScenario],
) -> None:
    """Missing scenario names retain their error contract in both modules."""
    with pytest.raises(ValueError, match=re.escape("scenario name is required")):
        named_scenario_resolver((), scenario_name=None)


@pytest.mark.parametrize("named_scenario_resolver", _NAMED_SCENARIO_RESOLVERS)
def test_named_scenario_reports_ordered_valid_names(
    tmp_path: pth.Path,
    named_scenario_resolver: cabc.Callable[..., TeeProfileScenario],
) -> None:
    """Unknown names retain the ordered scenario-list contract in both modules."""
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
        named_scenario_resolver(scenarios, scenario_name=config.scenario_name)


@pytest.mark.parametrize("named_scenario_resolver", _NAMED_SCENARIO_RESOLVERS)
def test_named_scenario_returns_requested_read_size(
    tmp_path: pth.Path,
    named_scenario_resolver: cabc.Callable[..., TeeProfileScenario],
) -> None:
    """Known names preserve their selected read size in both modules."""
    scenarios = default_tee_profile_scenarios(
        fixture_path=tmp_path / "fixture.b64",
        wrapped_fixture_path=tmp_path / "fixture-wrap76.b64",
        repeat_count=1,
        read_size=65536,
    )

    scenario = named_scenario_resolver(
        scenarios,
        scenario_name="tee-devnull-nocb-s1",
    )

    assert scenario.name == "tee-devnull-nocb-s1", "must select the named scenario"
    assert scenario.read_size == 65536, "must preserve the matrix read size"
