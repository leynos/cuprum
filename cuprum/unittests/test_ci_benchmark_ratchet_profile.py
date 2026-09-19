"""Unit tests for the CI benchmark ratchet helper.

The helper turns a dry-run benchmark plan into the hyperfine command the
`benchmark-ratchet` job measures: it keeps the scenarios that fit the CI
profile and rejects the rest. Nothing downstream notices when that filter
stops matching the plan it is given: the workload arrives from a separate
module, and a payload outside the band is dropped silently. If the workload
moved out of the band entirely, every scenario would be dropped and
`select_ci_ratchet_scenarios` would raise `ValueError` before `main` built or
ran any hyperfine command — a loud failure, but one paid for on the runner.
That is why the band, the selection, the plan rewriting, and the command
construction are all pinned here, and why the contract with the scenario
matrix that supplies the workload is
`test_ci_ratchet_profile_contract_matches_the_payload_matrix`.

Example
-------
pytest cuprum/unittests/test_ci_benchmark_ratchet_profile.py
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.benchmark_workload import (
    CI_RATCHET_WORKLOAD,
    THROUGHPUT_SWEEP_WORKLOAD,
    WORKLOAD_PLAN_KEY,
)
from benchmarks.ci_benchmark_ratchet_profile import (
    _CI_RATCHET_MAX_PAYLOAD_BYTES,
    _CI_RATCHET_MIN_PAYLOAD_BYTES,
    _CI_RATCHET_RUNS,
    build_hyperfine_command,
    load_plan_payload,
    main,
    select_ci_ratchet_scenarios,
    write_filtered_plan,
)
from benchmarks.pipeline_throughput_scenarios import (
    CI_RATCHET_PAYLOAD_BYTES,
    CI_RATCHET_WORKER_ITERATIONS,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


@dc.dataclass(frozen=True, slots=True)
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


#: Worker iterations the ratchet job plans with. Nothing in the profile module
#: pins this — the workflow passes it and the filtered plan echoes it — and the
#: value used below is `CI_RATCHET_WORKER_ITERATIONS`, the scenario module's own
#: constant, so these fixtures always describe the protocol the job runs.
_CI_PROFILE_SCENARIO_SPECS: tuple[_ScenarioSpec, ...] = (
    # Four scenarios at the measured payload, mirroring the matrix the CI job
    # plans, plus probes either side of the band and one that is too deep. Six
    # survive selection: the four tuned ones and the two probes that sit exactly
    # on the inclusive floor and ceiling.
    _ScenarioSpec(
        name="python-ratchet-single-nocb",
        backend="python",
        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
        stages=2,
    ),
    _ScenarioSpec(
        name="python-ratchet-single-cb",
        backend="python",
        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
        stages=2,
        with_line_callbacks=True,
    ),
    _ScenarioSpec(
        name="rust-ratchet-single-nocb",
        backend="rust",
        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
        stages=2,
    ),
    _ScenarioSpec(
        name="rust-ratchet-single-cb",
        backend="rust",
        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
        stages=2,
        with_line_callbacks=True,
    ),
    _ScenarioSpec(
        name="rust-at-the-floor",
        backend="rust",
        payload_bytes=_CI_RATCHET_MIN_PAYLOAD_BYTES,
        stages=2,
    ),
    _ScenarioSpec(
        name="rust-overhead-bound",
        backend="rust",
        payload_bytes=_CI_RATCHET_MIN_PAYLOAD_BYTES - 1,
        stages=2,
    ),
    _ScenarioSpec(
        name="rust-at-the-ceiling",
        backend="rust",
        payload_bytes=_CI_RATCHET_MAX_PAYLOAD_BYTES,
        stages=2,
    ),
    _ScenarioSpec(
        name="rust-over-ceiling",
        backend="rust",
        payload_bytes=_CI_RATCHET_MAX_PAYLOAD_BYTES + 1,
        stages=2,
    ),
    _ScenarioSpec(
        name="python-ratchet-multi-nocb",
        backend="python",
        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
        stages=3,
    ),
)


def test_select_ci_ratchet_scenarios_filters_for_ci_profile() -> None:
    """Only two-stage scenarios inside the payload band should remain."""
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
            "python ratchet nocb",
            "python ratchet cb",
            "rust ratchet nocb",
            "rust ratchet cb",
            "rust at the floor",
            "rust overhead bound",
            "rust at the ceiling",
            "rust over the ceiling",
            "python too deep",
        ],
        "scenarios": [_scenario(spec) for spec in _CI_PROFILE_SCENARIO_SPECS],
    }

    selected = select_ci_ratchet_scenarios(full_payload)

    # Validate name/command pairs (not names alone) so the test fails if the
    # scenario metadata and its hyperfine command were mismatched. The
    # overhead-bound and over-ceiling payloads must be dropped rather than
    # measured: below the floor the fixed per-run cost, not the pipeline,
    # decides most of the ratio (issue #219); above the ceiling one run is too
    # long to repeat twenty times inside the job's timeout. Both bounds are
    # inclusive, which is why the at-the-floor and at-the-ceiling probes stay.
    assert [(scenario["name"], command) for scenario, command in selected] == [
        ("rust-at-the-floor", "rust at the floor"),
        ("python-ratchet-single-nocb", "python ratchet nocb"),
        ("rust-ratchet-single-nocb", "rust ratchet nocb"),
        ("python-ratchet-single-cb", "python ratchet cb"),
        ("rust-ratchet-single-cb", "rust ratchet cb"),
        ("rust-at-the-ceiling", "rust at the ceiling"),
    ]


@pytest.mark.parametrize(
    ("scenario_kwargs", "last_command", "error_match"),
    [
        pytest.param(
            {
                "name": "python-ratchet-single-nocb",
                "backend": "python",
                "payload_bytes": CI_RATCHET_PAYLOAD_BYTES,
            },
            "python only",
            "must include Rust scenarios",
            id="no-rust-scenario",
        ),
        pytest.param(
            {
                "name": "rust-ratchet-single-nocb",
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


@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param("true", id="str"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
    ],
)
def test_select_ci_ratchet_scenarios_rejects_non_boolean_line_callbacks(
    bad_value: object,
) -> None:
    """Non-boolean with_line_callbacks metadata should raise TypeError."""
    scenario = _scenario(
        _ScenarioSpec(
            name="rust-ratchet-single-nocb",
            backend="rust",
            payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
            stages=2,
        )
    )
    scenario["with_line_callbacks"] = bad_value
    with pytest.raises(TypeError, match="scenario with_line_callbacks must be a bool"):
        select_ci_ratchet_scenarios({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", "rust only"],
            "scenarios": [scenario],
        })


@pytest.mark.parametrize(
    "non_finite_payload",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_select_ci_ratchet_scenarios_rejects_non_finite_payload(
    non_finite_payload: float,
) -> None:
    """NaN and infinity payloads are rejected rather than compared.

    `json.loads` accepts the bare `NaN` and `Infinity` literals, and every
    comparison against a NaN is false, so without the finiteness check a NaN
    payload passes the floor, the ceiling, and the non-negative check, and is
    measured. The plan is generated by our own dry run, so this is defensive
    rather than a live path — but the band is the module's whole contract, and
    a payload that is not a number has no place inside it.
    """
    scenario = _scenario(
        _ScenarioSpec(
            name="rust-ratchet-single-nocb",
            backend="rust",
            payload_bytes=typ.cast("int", non_finite_payload),
            stages=2,
        )
    )
    with pytest.raises(ValueError, match="scenario payload_bytes must be finite"):
        select_ci_ratchet_scenarios({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", "rust only"],
            "scenarios": [scenario],
        })


def test_select_ci_ratchet_scenarios_drops_oversized_integer_payload() -> None:
    """An integer too large for a float is dropped, not raised on.

    `json.loads` keeps arbitrary-precision integers, so a plan can carry a
    payload `math.isfinite` refuses to convert. Treating that as an error
    would report a malformed plan for a payload that is merely too large; the
    ceiling already rejects it, and the finiteness check stays on floats where
    it belongs.
    """
    oversized = 10**400
    with pytest.raises(ValueError, match="no scenarios selected"):
        select_ci_ratchet_scenarios({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", "rust only"],
            "scenarios": [
                _scenario(
                    _ScenarioSpec(
                        name="rust-ratchet-single-nocb",
                        backend="rust",
                        payload_bytes=oversized,
                        stages=2,
                    )
                )
            ],
        })


@pytest.mark.parametrize(
    "bad_backend",
    [
        pytest.param("wasm", id="wasm"),
        pytest.param("node", id="node"),
    ],
)
def test_select_ci_ratchet_scenarios_rejects_unknown_backend(bad_backend: str) -> None:
    """Scenarios with an unsupported backend should raise ValueError."""
    bad_scenario = _scenario(
        _ScenarioSpec(
            name="bad-ratchet-single-nocb",
            backend=bad_backend,
            payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
            stages=2,
        )
    )
    rust_scenario = _scenario(
        _ScenarioSpec(
            name="rust-ratchet-single-nocb",
            backend="rust",
            payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
            stages=2,
        )
    )
    with pytest.raises(ValueError, match="scenario backend must be one of"):
        select_ci_ratchet_scenarios({
            "dry_run": True,
            "rust_available": True,
            "command": ["a", "b", "c", "d", "e", "f", "g", "bad", "rust only"],
            "scenarios": [bad_scenario, rust_scenario],
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
                        name="python-ratchet-single-nocb",
                        backend="python",
                        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
                        stages=2,
                    )
                ),
                _scenario(
                    _ScenarioSpec(
                        name="rust-ratchet-single-nocb",
                        backend="rust",
                        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
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
    """The hyperfine command should tag each scenario and keep raw commands.

    Every selected scenario contributes exactly one ``--command-name`` option
    followed by its logical name; the raw worker commands remain present and
    ordered after all naming options.
    """
    throughput_path = tmp_path / "throughput.json"
    selected = [
        (
            _scenario(
                _ScenarioSpec(
                    name="python-ratchet-single-nocb",
                    backend="python",
                    payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
                    stages=2,
                )
            ),
            "python cmd",
        ),
        (
            _scenario(
                _ScenarioSpec(
                    name="rust-ratchet-single-nocb",
                    backend="rust",
                    payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
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
        str(_CI_RATCHET_RUNS),
        "--command-name",
        "python-ratchet-single-nocb",
        "--command-name",
        "rust-ratchet-single-nocb",
        "python cmd",
        "rust cmd",
    ]

    assert len(selected) == 2, "test fixture should cover two scenarios"
    name_pairs = [
        command[index + 1]
        for index, option in enumerate(command[:-1])
        if option == "--command-name"
    ]
    assert len(name_pairs) == len(selected), (
        "every selected scenario must contribute exactly one --command-name"
    )
    assert name_pairs == [scenario["name"] for scenario, _ in selected], (
        "command-name options must carry the selected scenario names in order"
    )

    name_options_end = command.index("python cmd")
    assert command[name_options_end:] == ["python cmd", "rust cmd"], (
        "raw worker commands must remain present and ordered after the "
        "command-name options"
    )


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
                    name="python-ratchet-single-nocb",
                    backend="python",
                    payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
                    stages=2,
                )
            ),
            "python cmd",
        ),
        (
            _scenario(
                _ScenarioSpec(
                    name="rust-ratchet-single-nocb",
                    backend="rust",
                    payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
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
            "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
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
        "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
        # The full plan above declares no workload, which describes the
        # throughput sweep. The filtered plan must carry the default through
        # rather than dropping the key: a summary reading it back needs an
        # answer, and dropping it would leave the summary to infer one.
        WORKLOAD_PLAN_KEY: THROUGHPUT_SWEEP_WORKLOAD,
    }


def test_write_filtered_plan_carries_the_declared_workload(
    tmp_path: pth.Path,
) -> None:
    """A filtering step must not relabel the workload it was handed.

    The filter selects scenarios from the plan it is given, so the workload
    that produced them is whatever that plan recorded. Restating it here
    instead would let the ratchet job's summary name a workload the plan
    never declared.
    """
    filtered_plan_path = tmp_path / "plan.json"
    selected = [
        (
            _scenario(
                _ScenarioSpec(
                    name="python-ratchet-single-nocb",
                    backend="python",
                    payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
                    stages=2,
                )
            ),
            "python cmd",
        ),
    ]

    write_filtered_plan(
        filtered_plan_path=filtered_plan_path,
        full_payload={
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "rust_available": True,
            "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
            WORKLOAD_PLAN_KEY: CI_RATCHET_WORKLOAD,
        },
        command=["hyperfine"],
        selected=selected,
    )

    payload = json.loads(filtered_plan_path.read_text(encoding="utf-8"))
    assert payload[WORKLOAD_PLAN_KEY] == CI_RATCHET_WORKLOAD


def test_write_filtered_plan_rejects_an_unknown_workload(tmp_path: pth.Path) -> None:
    """An unrecognized workload must fail rather than be summarized as a default."""
    with pytest.raises(ValueError, match="unknown benchmark workload"):
        write_filtered_plan(
            filtered_plan_path=tmp_path / "plan.json",
            full_payload={
                "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
                "rust_available": True,
                "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
                WORKLOAD_PLAN_KEY: "not-a-workload",
            },
            command=["hyperfine"],
            selected=[],
        )


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
            "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
            "command": ["a", "b", "c", "d", "e", "f", "g", "rust cmd"],
            "scenarios": [
                _scenario(
                    _ScenarioSpec(
                        name="rust-ratchet-single-nocb",
                        backend="rust",
                        payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
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
                "worker_iterations": CI_RATCHET_WORKER_ITERATIONS,
            },
            command=["hyperfine", "rust cmd"],
            selected=[
                (
                    _scenario(
                        _ScenarioSpec(
                            name="rust-ratchet-single-nocb",
                            backend="rust",
                            payload_bytes=CI_RATCHET_PAYLOAD_BYTES,
                            stages=2,
                        )
                    ),
                    "rust cmd",
                ),
            ],
        )


def test_ci_ratchet_profile_contract_matches_the_payload_matrix() -> None:
    """The profile's band must accept the workload the scenario matrix builds.

    The band and the payload are separate declarations in separate modules:
    the matrix decides what a plan offers, the profile decides what it will
    measure. Nothing but a test notices when one moves out from under the
    other. A payload below the floor or above the ceiling is dropped without
    comment, so the failure is only loud in the limit: once nothing is left,
    ``select_ci_ratchet_scenarios`` raises and the job fails on the runner
    instead of measuring. This test moves that failure to the suite.
    """
    assert _CI_RATCHET_MIN_PAYLOAD_BYTES == 32 * 1024 * 1024, (
        "the floor is the first payload above every measured crossover — the "
        "four backend/mode combinations reach it between about 4 MiB and "
        "22 MiB per iteration — so a scenario the band accepts has paid for "
        "its ratio with streaming work rather than with set-up"
    )
    assert _CI_RATCHET_MAX_PAYLOAD_BYTES == 128 * 1024 * 1024, (
        "the ceiling keeps one measured run to a few seconds, so the job still "
        "fits its timeout with confirmation re-measurement included"
    )
    assert CI_RATCHET_PAYLOAD_BYTES == 64 * 1024 * 1024, (
        "64 MiB is the tuned workload: streaming is 62% of the pure-Python "
        "no-callback run, 57% of the native one, and 87% of both callback "
        "runs on the reference host, so the pipeline rather than the worker's "
        "start-up is most of what is timed (issue #219)"
    )
    assert (
        _CI_RATCHET_MIN_PAYLOAD_BYTES
        <= CI_RATCHET_PAYLOAD_BYTES
        <= (_CI_RATCHET_MAX_PAYLOAD_BYTES)
    ), (
        "the measured workload must fall inside the band the profile accepts; "
        f"band={_CI_RATCHET_MIN_PAYLOAD_BYTES}..{_CI_RATCHET_MAX_PAYLOAD_BYTES}, "
        f"workload={CI_RATCHET_PAYLOAD_BYTES}"
    )
    assert _CI_RATCHET_RUNS == 20, (
        "twenty runs is the tuned count: at this payload it keeps the mean of "
        "each command stable while the whole measurement still costs about "
        "two minutes"
    )
