r"""Build and execute the CI benchmark profile for the Rust ratchet.

Rewrites a dry-run benchmark plan down to the scenarios the CI ratchet is
allowed to compare, then runs hyperfine over them and writes the filtered
plan beside the throughput JSON. The filter is the profile: it keeps the
two-stage scenarios inside a payload band and discards the rest, so the
payload the ratchet compares from run to run is a property of this module
rather than of whatever matrix the plan happens to carry. See
``docs/cuprum-design.md`` 13.9 for why the band exists and the tuning
record it cites for where its bounds came from.

The command is normally invoked by the `benchmark-ratchet` job, after
``pipeline_throughput.py --ci-ratchet --dry-run`` has written the full
plan.

Example
-------
uv run python benchmarks/ci_benchmark_ratchet_profile.py \\
  --full-plan full-plan.json \\
  --filtered-plan plan.json \\
  --throughput throughput.json
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - helper intentionally invokes hyperfine
import typing as typ

from benchmarks._validation import (
    _require_bool,
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)
from benchmarks.benchmark_profile import require_worker_iterations
from benchmarks.benchmark_workload import WORKLOAD_PLAN_KEY, read_workload

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_HYPERFINE_PREFIX_ARGUMENT_COUNT = 7
# Kept separate from the module docstring for the same reason as in
# `pipeline_throughput.py`: argparse reflows a multi-paragraph description into
# one block, and `--help` reads better as a single line.
_CLI_DESCRIPTION = "Build and execute the CI benchmark profile for the Rust ratchet."
_CI_RATCHET_STAGE_COUNT = 2
# The ratchet only accepts payloads the streaming work dominates, so that the
# ratio it compares is a measurement of the pipeline rather than of the fixed
# per-run cost every scenario pays. The floor is the first payload above every
# crossover measured on the reference host — the point where the streaming work
# drawn across the five iterations outweighs the five iterations' set-up, which
# the four backend/mode combinations reach between about 4 MiB and 22 MiB — so
# a scenario the band accepts has paid for its ratio with streaming work; the
# ceiling keeps one measured run to a few seconds in callback mode, so a job
# still fits its timeout. The workload comes from
# `benchmarks.pipeline_throughput_scenarios.CI_RATCHET_PAYLOAD_BYTES`, which
# `test_ci_ratchet_profile_contract_matches_the_payload_matrix` holds inside
# this band.
_CI_RATCHET_MIN_PAYLOAD_BYTES = 32 * 1024 * 1024
_CI_RATCHET_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
#: Enough runs that the mean of each command is stable without, at the ratchet
#: payload, spending more than a couple of minutes on the whole measurement.
_CI_RATCHET_RUNS = 20
_SUPPORTED_BACKENDS = ("python", "rust")


def load_plan_payload(full_plan_path: pth.Path) -> cabc.Mapping[str, object]:
    """Load and validate the dry-run benchmark plan payload.

    Parameters
    ----------
    full_plan_path : pathlib.Path
        Filesystem path to the dry-run benchmark plan JSON to load and
        validate.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The validated plan payload mapping.

    Raises
    ------
    OSError
        If ``full_plan_path`` cannot be read.
    json.JSONDecodeError
        If the file does not contain valid JSON.
    TypeError
        If the payload or its ``scenarios``/``command`` entries are not of
        the required structural type.
    ValueError
        If the scenario count does not match the scenario command count.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError, JSONDecodeError, and TypeError propagate from the readers and validators
    payload = json.loads(full_plan_path.read_text(encoding="utf-8"))
    full_payload = _require_mapping(payload, name=f"plan payload from {full_plan_path}")
    scenarios = _require_list(full_payload.get("scenarios"), name="scenarios")
    plan_command = _require_list(full_payload.get("command"), name="command")
    scenario_commands = plan_command[_HYPERFINE_PREFIX_ARGUMENT_COUNT:]
    if len(scenarios) != len(scenario_commands):
        msg = "scenario count must match scenario command count"
        raise ValueError(msg)
    return full_payload


def _require_payload_bytes(value: object) -> int:
    """Return *value* as a whole, finite payload size in bytes, or raise."""
    # `json.loads` accepts the bare `NaN` and `Infinity` literals, and every
    # comparison against a NaN is false, so a NaN payload would pass the
    # non-negative check and both band bounds and be measured. An `int` is
    # always finite, and testing one against `math.isfinite` would raise
    # `OverflowError` on an absurd JSON integer rather than the `ValueError`
    # the caller expects — so only floats reach the check, and an out-of-range
    # integer is dropped by the ceiling like any other oversized payload.
    # `bool` is an `int` subclass, so it is excluded before the numeric case.
    #
    # A whole float is converted rather than passed through. The retained
    # scenarios are written out as the filtered plan and read back by
    # `benchmark_workload`, which requires an `int` and rejects a float; a
    # JSON payload written as `67108864.0` would otherwise pass every check
    # here and make the plan unreadable later. A fractional payload is not a
    # size any measurement could report, and truncating it would record a
    # payload the plan never declared, so it is refused outright.
    match value:
        case bool():
            msg = "scenario payload_bytes must be numeric"
            raise TypeError(msg)
        case int():
            return value
        case float() if not math.isfinite(value):
            msg = f"scenario payload_bytes must be finite, got {value!r}"
            raise ValueError(msg)
        case float() if value.is_integer():
            return int(value)
        case float():
            msg = (
                f"scenario payload_bytes must be a whole number of bytes, got {value!r}"
            )
            raise ValueError(msg)
        case _:
            msg = "scenario payload_bytes must be numeric"
            raise TypeError(msg)


def _require_backend(value: object) -> str:
    """Return *value* as a supported backend name, or raise ``ValueError``."""
    if value not in _SUPPORTED_BACKENDS:
        msg = f"scenario backend must be one of {_SUPPORTED_BACKENDS}, got {value!r}"
        raise ValueError(msg)
    return typ.cast("str", value)


def _select_scenario(
    scenario_value: object,
    scenario_command_value: object,
) -> tuple[cabc.Mapping[str, object], str] | None:
    """Return a filtered (scenario, command) pair, or ``None``."""
    scenario = _require_mapping(scenario_value, name="scenario")
    scenario_command = _require_non_empty_string(
        scenario_command_value,
        name="scenario command",
    )
    if scenario.get("stages") != _CI_RATCHET_STAGE_COUNT:
        return None
    payload_bytes = _require_payload_bytes(scenario.get("payload_bytes", 0))
    if payload_bytes < 0:
        msg = "scenario payload_bytes must be >= 0"
        raise ValueError(msg)
    if payload_bytes < _CI_RATCHET_MIN_PAYLOAD_BYTES:
        return None
    if payload_bytes > _CI_RATCHET_MAX_PAYLOAD_BYTES:
        return None
    # The validated size replaces whatever the plan spelled. A whole float is
    # the case that needs it: it passes the band checks, and the retained
    # scenarios are written out verbatim as the filtered plan, so leaving it
    # as a float would put the same value back into the JSON that
    # `benchmark_workload` then refuses to read as a payload size.
    return {**scenario, "payload_bytes": payload_bytes}, scenario_command


def select_ci_ratchet_scenarios(
    full_payload: cabc.Mapping[str, object],
) -> list[tuple[cabc.Mapping[str, object], str]]:
    """Return the benchmark scenarios retained by the CI ratchet profile.

    Parameters
    ----------
    full_payload : collections.abc.Mapping[str, object]
        The full benchmark plan payload to filter down to the CI
        ratchet scenarios.

    Returns
    -------
    list[tuple[collections.abc.Mapping[str, object], str]]
        The selected scenarios paired with their commands, sorted by
        payload size, callback usage, and backend.

    Raises
    ------
    TypeError
        If ``scenarios``/``command`` or a selected scenario's fields are not
        of the required structural type.
    ValueError
        If the scenario and command sequences are of unequal length (strict
        zip), a scenario field fails validation, or no scenarios are
        selected or the selection omits Rust scenarios.
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError and the zip/field ValueErrors propagate from the validators
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
    selected.sort(
        key=lambda entry: (
            _require_payload_bytes(entry[0].get("payload_bytes", 0)),
            _require_bool(
                entry[0].get("with_line_callbacks", False),
                name="scenario with_line_callbacks",
            ),
            _require_backend(entry[0].get("backend")) != "python",
        )
    )
    return selected


def build_hyperfine_command(
    *,
    throughput_path: pth.Path,
    selected: cabc.Sequence[tuple[cabc.Mapping[str, object], str]],
) -> list[str]:
    """Build the hyperfine command for the filtered CI scenario set.

    Every selected scenario is tagged with a ``--command-name`` pair so
    Hyperfine writes the logical scenario name into ``results[*].command``
    instead of the raw worker command string. The raw worker commands are
    preserved, in order, after all naming options so the ratchet can pair each
    result with its positionally matched scenario.

    Parameters
    ----------
    throughput_path : pathlib.Path
        Output path for the hyperfine throughput JSON the command will
        write.
    selected : collections.abc.Sequence[
        tuple[collections.abc.Mapping[str, object], str]
    ]
        The selected scenarios paired with their worker commands.

    Returns
    -------
    list[str]
        The hyperfine command line for the selected scenarios.

    Raises
    ------
    TypeError
        If a selected scenario's ``name`` is not a string.
    ValueError
        If a selected scenario's ``name`` is empty or whitespace-only.
    """  # ruff: ignore[docstring-extraneous-exception] - propagates from _require_non_empty_string
    return [
        "hyperfine",
        "--export-json",
        str(throughput_path),
        "--warmup",
        "1",
        "--runs",
        str(_CI_RATCHET_RUNS),
        *[
            option
            for scenario, _ in selected
            for option in (
                "--command-name",
                _require_non_empty_string(
                    scenario.get("name"),
                    name="scenario name",
                ),
            )
        ],
        *[scenario_command for _, scenario_command in selected],
    ]


def write_filtered_plan(
    *,
    filtered_plan_path: pth.Path,
    full_payload: cabc.Mapping[str, object],
    command: list[str],
    selected: cabc.Sequence[tuple[cabc.Mapping[str, object], str]],
) -> None:
    """Write the filtered dry-run plan used by the benchmark ratchet."""
    rust_available = _require_rust_available(full_payload)
    # The workload is carried through rather than restated: the filter selects
    # scenarios from the plan it was handed, so the workload that produced them
    # is whatever that plan recorded, and a summary rendering the filtered plan
    # must be able to name it. `read_workload` validates it on the way through,
    # so a plan naming an unknown workload is rejected here rather than being
    # summarized as the sweep.
    filtered_payload = {
        "benchmark_profile_version": _require_non_empty_string(
            full_payload.get("benchmark_profile_version"),
            name="benchmark_profile_version",
        ),
        "dry_run": True,
        "rust_available": rust_available,
        "worker_iterations": require_worker_iterations(full_payload),
        WORKLOAD_PLAN_KEY: read_workload(full_payload),
        "command": command,
        "scenarios": [scenario for scenario, _ in selected],
    }
    filtered_plan_path.write_text(
        json.dumps(filtered_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _require_rust_available(payload: cabc.Mapping[str, object]) -> bool:
    """Return validated ``rust_available`` metadata from a dry-run plan."""
    value = payload.get("rust_available", False)
    if not isinstance(value, bool):
        msg = "rust_available must be a bool"
        raise TypeError(msg)
    return value


def _parse_args(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION)
    parser.add_argument("--full-plan", type=pth.Path, required=True)
    parser.add_argument("--filtered-plan", type=pth.Path, required=True)
    parser.add_argument("--throughput", type=pth.Path, required=True)
    return parser.parse_args(argv)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the CI benchmark ratchet profile helper.

    Parameters
    ----------
    argv : collections.abc.Sequence[str] | None
        Optional CLI argument sequence; when ``None`` the process
        arguments are parsed.

    Returns
    -------
    int
        The process exit code (``0`` on success).

    Raises
    ------
    SystemExit
        If ``argv`` is invalid and ``_parse_args`` reports a usage error.
    subprocess.CalledProcessError
        If the hyperfine benchmark command exits non-zero.
    OSError
        If a plan file cannot be read or the filtered plan cannot be written.
    json.JSONDecodeError
        If a plan file does not contain valid JSON.
    TypeError
        If a plan payload fails structural validation.
    ValueError
        If plan loading, scenario selection, or writing the filtered plan
        rejects the payload.
    """  # ruff: ignore[docstring-extraneous-exception] - propagate from arg parsing, loader, selection, writer, subprocess
    args = _parse_args(argv)
    full_payload = load_plan_payload(args.full_plan)
    selected = select_ci_ratchet_scenarios(full_payload)
    command = build_hyperfine_command(
        throughput_path=args.throughput,
        selected=selected,
    )
    subprocess.run(command, check=True)  # ruff: ignore[subprocess-without-shell-equals-true] - commands come from our dry-run plan
    write_filtered_plan(
        filtered_plan_path=args.filtered_plan,
        full_payload=full_payload,
        command=command,
        selected=selected,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
