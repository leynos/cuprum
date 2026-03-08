"""Build and execute the CI smoke benchmark profile for the Rust ratchet."""

from __future__ import annotations

import argparse
import json
import pathlib as pth
import subprocess  # noqa: S404 - helper intentionally invokes hyperfine
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)

_HYPERFINE_PREFIX_ARGUMENT_COUNT = 7
_CI_RATCHET_STAGE_COUNT = 2
_CI_RATCHET_MAX_PAYLOAD_BYTES = 65536


def load_plan_payload(full_plan_path: pth.Path) -> typ.Mapping[str, object]:
    """Load and validate the dry-run benchmark plan payload."""
    payload = json.loads(full_plan_path.read_text(encoding="utf-8"))
    full_payload = _require_mapping(payload, name=f"plan payload from {full_plan_path}")
    scenarios = _require_list(full_payload.get("scenarios"), name="scenarios")
    plan_command = _require_list(full_payload.get("command"), name="command")
    scenario_commands = plan_command[_HYPERFINE_PREFIX_ARGUMENT_COUNT:]
    if len(scenarios) != len(scenario_commands):
        msg = "scenario count must match scenario command count"
        raise ValueError(msg)
    return full_payload


def _require_numeric_payload_bytes(value: object) -> int | float:
    """Return *value* as a numeric payload size, or raise ``TypeError``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "scenario payload_bytes must be numeric"
        raise TypeError(msg)
    return value


def _select_scenario(
    scenario_value: object,
    scenario_command_value: object,
) -> tuple[typ.Mapping[str, object], str] | None:
    """Return a filtered (scenario, command) pair, or ``None``."""
    scenario = _require_mapping(scenario_value, name="scenario")
    scenario_command = _require_non_empty_string(
        scenario_command_value,
        name="scenario command",
    )
    if scenario.get("stages") != _CI_RATCHET_STAGE_COUNT:
        return None
    payload_bytes = _require_numeric_payload_bytes(scenario.get("payload_bytes", 0))
    if payload_bytes < 0:
        msg = "scenario payload_bytes must be >= 0"
        raise ValueError(msg)
    if payload_bytes > _CI_RATCHET_MAX_PAYLOAD_BYTES:
        return None
    return scenario, scenario_command


def select_ci_ratchet_scenarios(
    full_payload: typ.Mapping[str, object],
) -> list[tuple[typ.Mapping[str, object], str]]:
    """Return the benchmark scenarios retained by the CI ratchet profile."""
    scenarios = _require_list(full_payload.get("scenarios"), name="scenarios")
    plan_command = _require_list(full_payload.get("command"), name="command")
    scenario_commands = plan_command[_HYPERFINE_PREFIX_ARGUMENT_COUNT:]

    selected = [
        entry
        for scenario_value, scenario_command_value in zip(
            scenarios, scenario_commands, strict=True
        )
        if (entry := _select_scenario(scenario_value, scenario_command_value))
        is not None
    ]

    if not selected:
        msg = "no scenarios selected for CI benchmark ratchet"
        raise ValueError(msg)
    if not any(scenario.get("backend") == "rust" for scenario, _ in selected):
        msg = "selected CI benchmark profile must include Rust scenarios"
        raise ValueError(msg)
    return selected


def build_hyperfine_command(
    *,
    throughput_path: pth.Path,
    selected: typ.Sequence[tuple[typ.Mapping[str, object], str]],
) -> list[str]:
    """Build the hyperfine command for the filtered CI scenario set."""
    return [
        "hyperfine",
        "--export-json",
        str(throughput_path),
        "--warmup",
        "1",
        "--runs",
        "3",
        *[scenario_command for _, scenario_command in selected],
    ]


def write_filtered_plan(
    *,
    filtered_plan_path: pth.Path,
    rust_available: bool,
    command: list[str],
    selected: typ.Sequence[tuple[typ.Mapping[str, object], str]],
) -> None:
    """Write the filtered dry-run plan used by the benchmark ratchet."""
    filtered_payload = {
        "dry_run": True,
        "rust_available": rust_available,
        "command": command,
        "scenarios": [scenario for scenario, _ in selected],
    }
    filtered_plan_path.write_text(
        json.dumps(filtered_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _parse_args(argv: typ.Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-plan", type=pth.Path, required=True)
    parser.add_argument("--filtered-plan", type=pth.Path, required=True)
    parser.add_argument("--throughput", type=pth.Path, required=True)
    return parser.parse_args(argv)


def main(argv: typ.Sequence[str] | None = None) -> int:
    """Run the CI benchmark ratchet profile helper."""
    args = _parse_args(argv)
    full_payload = load_plan_payload(args.full_plan)
    selected = select_ci_ratchet_scenarios(full_payload)
    command = build_hyperfine_command(
        throughput_path=args.throughput,
        selected=selected,
    )
    subprocess.run(command, check=True)  # noqa: S603 - commands come from our dry-run plan
    write_filtered_plan(
        filtered_plan_path=args.filtered_plan,
        rust_available=bool(full_payload.get("rust_available", False)),
        command=command,
        selected=selected,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
