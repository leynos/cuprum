"""Unit tests for the CI smoke benchmark ratchet helper."""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ci_benchmark_ratchet_profile import (
    build_hyperfine_command,
    load_plan_payload,
    main,
    select_ci_ratchet_scenarios,
    write_filtered_plan,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


@dc.dataclass(frozen=True)
class _ScenarioSpec:
    """Parameters for a CI benchmark test scenario."""

    name: str
    backend: str
    payload_bytes: int
    stages: int
    with_line_callbacks: bool = False


def _scenario(spec: _ScenarioSpec) -> dict[str, object]:
    """Create a scenario dict for CI benchmark tests."""
    return {
        "name": spec.name,
        "backend": spec.backend,
        "payload_bytes": spec.payload_bytes,
        "stages": spec.stages,
        "with_line_callbacks": spec.with_line_callbacks,
    }


_CI_PROFILE_SCENARIO_SPECS: tuple[_ScenarioSpec, ...] = (
    _ScenarioSpec(
        name="python-small-single-nocb",
        backend="python",
        payload_bytes=1024,
        stages=2,
    ),
    _ScenarioSpec(
        name="python-small-single-cb",
        backend="python",
        payload_bytes=1024,
        stages=2,
        with_line_callbacks=True,
    ),
    _ScenarioSpec(
        name="rust-small-single-nocb",
        backend="rust",
        payload_bytes=1024,
        stages=2,
    ),
    _ScenarioSpec(
        name="rust-small-single-cb",
        backend="rust",
        payload_bytes=1024,
        stages=2,
        with_line_callbacks=True,
    ),
    _ScenarioSpec(
        name="rust-large-single-nocb",
        backend="rust",
        payload_bytes=131072,
        stages=2,
    ),
    _ScenarioSpec(
        name="python-small-multi-nocb",
        backend="python",
        payload_bytes=1024,
        stages=3,
    ),
)


def test_select_ci_ratchet_scenarios_filters_for_ci_profile() -> None:
    """Only two-stage scenarios up to 64 KiB should remain in the CI profile."""
    full_payload = {
        "dry_run": True,
        "rust_available": True,
        "command": [
            "hyperfine",
            "--export-json",
            "throughput.json",
            "--warmup",
            "1",
            "--runs",
            "3",
            "python small nocb",
            "python small cb",
            "rust small nocb",
            "rust small cb",
            "rust too-big",
            "python too-deep",
        ],
        "scenarios": [_scenario(spec) for spec in _CI_PROFILE_SCENARIO_SPECS],
    }

    selected = select_ci_ratchet_scenarios(full_payload)

    assert [scenario["name"] for scenario, _ in selected] == [
        "python-small-single-nocb",
        "rust-small-single-nocb",
        "python-small-single-cb",
        "rust-small-single-cb",
    ]


@pytest.mark.parametrize(
    ("scenario_kwargs", "last_command", "error_match"),
    [
        pytest.param(
            {
                "name": "python-small-single-nocb",
                "backend": "python",
                "payload_bytes": 1024,
            },
            "python only",
            "must include Rust scenarios",
            id="no-rust-scenario",
        ),
        pytest.param(
            {
                "name": "rust-small-single-nocb",
                "backend": "rust",
                "payload_bytes": -1,
            },
            "rust only",
            "payload_bytes must be >= 0",
            id="negative-payload-bytes",
        ),
    ],
)
def test_select_ci_ratchet_scenarios_rejects_invalid_single_scenario(
    scenario_kwargs: dict[str, object],
    last_command: str,
    error_match: str,
) -> None:
    """Invalid single-scenario CI payloads should raise ValueError."""
    with pytest.raises(ValueError, match=error_match):
        select_ci_ratchet_scenarios({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", last_command],
            "scenarios": [
                _scenario(
                    _ScenarioSpec(
                        name=typ.cast("str", scenario_kwargs["name"]),
                        backend=typ.cast("str", scenario_kwargs["backend"]),
                        payload_bytes=typ.cast("int", scenario_kwargs["payload_bytes"]),
                        stages=2,
                    )
                )
            ],
        })


def test_load_plan_payload_rejects_mismatched_command_count(tmp_path: pth.Path) -> None:
    """The helper should reject dry-run plans with misaligned command counts."""
    plan_path = tmp_path / "full-plan.json"
    plan_path.write_text(
        json.dumps({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", "cmd-1"],
            "scenarios": [
                _scenario(
                    _ScenarioSpec(
                        name="python-small-single-nocb",
                        backend="python",
                        payload_bytes=1024,
                        stages=2,
                    )
                ),
                _scenario(
                    _ScenarioSpec(
                        name="rust-small-single-nocb",
                        backend="rust",
                        payload_bytes=1024,
                        stages=2,
                    )
                ),
            ],
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scenario count must match"):
        load_plan_payload(plan_path)


def test_build_hyperfine_command_includes_selected_scenarios(
    tmp_path: pth.Path,
) -> None:
    """The hyperfine command should target the filtered scenario commands."""
    throughput_path = tmp_path / "throughput.json"
    selected = [
        (
            _scenario(
                _ScenarioSpec(
                    name="python-small-single-nocb",
                    backend="python",
                    payload_bytes=1024,
                    stages=2,
                )
            ),
            "python cmd",
        ),
        (
            _scenario(
                _ScenarioSpec(
                    name="rust-small-single-nocb",
                    backend="rust",
                    payload_bytes=1024,
                    stages=2,
                )
            ),
            "rust cmd",
        ),
    ]

    command = build_hyperfine_command(
        throughput_path=throughput_path,
        selected=selected,
    )

    assert command == [
        "hyperfine",
        "--export-json",
        str(throughput_path),
        "--warmup",
        "1",
        "--runs",
        "10",
        "python cmd",
        "rust cmd",
    ]


def test_write_filtered_plan_preserves_selected_scenarios(tmp_path: pth.Path) -> None:
    """The filtered plan should mirror the selected CI benchmark subset."""
    filtered_plan_path = tmp_path / "plan.json"
    command = [
        "hyperfine",
        "--export-json",
        "throughput.json",
        "python cmd",
        "rust cmd",
    ]
    selected = [
        (
            _scenario(
                _ScenarioSpec(
                    name="python-small-single-nocb",
                    backend="python",
                    payload_bytes=1024,
                    stages=2,
                )
            ),
            "python cmd",
        ),
        (
            _scenario(
                _ScenarioSpec(
                    name="rust-small-single-nocb",
                    backend="rust",
                    payload_bytes=1024,
                    stages=2,
                )
            ),
            "rust cmd",
        ),
    ]

    write_filtered_plan(
        filtered_plan_path=filtered_plan_path,
        full_payload={
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "rust_available": True,
            "worker_iterations": 20,
        },
        command=command,
        selected=selected,
    )

    payload = json.loads(filtered_plan_path.read_text(encoding="utf-8"))
    assert payload == {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "command": command,
        "dry_run": True,
        "rust_available": True,
        "scenarios": [scenario for scenario, _ in selected],
        "worker_iterations": 20,
    }


def test_main_rejects_non_bool_rust_availability(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI helper should reject malformed dry-run plan metadata."""
    full_plan_path = tmp_path / "full-plan.json"
    filtered_plan_path = tmp_path / "filtered-plan.json"
    throughput_path = tmp_path / "throughput.json"
    full_plan_path.write_text(
        json.dumps({
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "dry_run": True,
            "rust_available": "false",
            "worker_iterations": 20,
            "command": ["a", "b", "c", "d", "e", "f", "g", "rust cmd"],
            "scenarios": [
                _scenario(
                    _ScenarioSpec(
                        name="rust-small-single-nocb",
                        backend="rust",
                        payload_bytes=1024,
                        stages=2,
                    )
                )
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "benchmarks.ci_benchmark_ratchet_profile.subprocess.run",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(TypeError, match="rust_available must be a bool"):
        main([
            "--full-plan",
            str(full_plan_path),
            "--filtered-plan",
            str(filtered_plan_path),
            "--throughput",
            str(throughput_path),
        ])


def test_write_filtered_plan_rejects_non_boolean_rust_available(
    tmp_path: pth.Path,
) -> None:
    """The filtered plan must not coerce string ``rust_available`` values."""
    with pytest.raises(TypeError, match="rust_available"):
        write_filtered_plan(
            filtered_plan_path=tmp_path / "plan.json",
            full_payload={
                "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
                "rust_available": "false",
                "worker_iterations": 20,
            },
            command=["hyperfine", "rust cmd"],
            selected=[
                (
                    _scenario(
                        _ScenarioSpec(
                            name="rust-small-single-nocb",
                            backend="rust",
                            payload_bytes=1024,
                            stages=2,
                        )
                    ),
                    "rust cmd",
                ),
            ],
        )
