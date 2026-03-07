"""Unit tests for the CI smoke benchmark ratchet helper."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.ci_benchmark_ratchet_profile import (
    build_hyperfine_command,
    load_plan_payload,
    select_ci_ratchet_scenarios,
    write_filtered_plan,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _scenario(
    *,
    name: str,
    backend: str,
    payload_bytes: int,
    stages: int,
) -> dict[str, object]:
    """Create a scenario dict for CI benchmark tests."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": payload_bytes,
        "stages": stages,
    }


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
            "python small",
            "rust small",
            "rust too-big",
            "python too-deep",
        ],
        "scenarios": [
            _scenario(
                name="python-small-single-nocb",
                backend="python",
                payload_bytes=1024,
                stages=2,
            ),
            _scenario(
                name="rust-small-single-nocb",
                backend="rust",
                payload_bytes=1024,
                stages=2,
            ),
            _scenario(
                name="rust-large-single-nocb",
                backend="rust",
                payload_bytes=131072,
                stages=2,
            ),
            _scenario(
                name="python-small-multi-nocb",
                backend="python",
                payload_bytes=1024,
                stages=3,
            ),
        ],
    }

    selected = select_ci_ratchet_scenarios(full_payload)

    assert [scenario["name"] for scenario, _ in selected] == [
        "python-small-single-nocb",
        "rust-small-single-nocb",
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
                    name=typ.cast("str", scenario_kwargs["name"]),
                    backend=typ.cast("str", scenario_kwargs["backend"]),
                    payload_bytes=typ.cast("int", scenario_kwargs["payload_bytes"]),
                    stages=2,
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
                    name="python-small-single-nocb",
                    backend="python",
                    payload_bytes=1024,
                    stages=2,
                ),
                _scenario(
                    name="rust-small-single-nocb",
                    backend="rust",
                    payload_bytes=1024,
                    stages=2,
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
                name="python-small-single-nocb",
                backend="python",
                payload_bytes=1024,
                stages=2,
            ),
            "python cmd",
        ),
        (
            _scenario(
                name="rust-small-single-nocb",
                backend="rust",
                payload_bytes=1024,
                stages=2,
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
        "3",
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
                name="python-small-single-nocb",
                backend="python",
                payload_bytes=1024,
                stages=2,
            ),
            "python cmd",
        ),
        (
            _scenario(
                name="rust-small-single-nocb",
                backend="rust",
                payload_bytes=1024,
                stages=2,
            ),
            "rust cmd",
        ),
    ]

    write_filtered_plan(
        filtered_plan_path=filtered_plan_path,
        rust_available=True,
        command=command,
        selected=selected,
    )

    payload = json.loads(filtered_plan_path.read_text(encoding="utf-8"))
    assert payload == {
        "command": command,
        "dry_run": True,
        "rust_available": True,
        "scenarios": [scenario for scenario, _ in selected],
    }
