"""Scenario matrix construction for pipeline throughput benchmarks."""

from __future__ import annotations

from benchmarks._benchmark_types import BackendName, PipelineBenchmarkScenario

# Payload sizes for the benchmark scenario matrix.
_SMALL_PAYLOAD_BYTES = 1024  # 1 KB
_MEDIUM_PAYLOAD_BYTES = 1024 * 1024  # 1 MB
_LARGE_PAYLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

# Smoke-mode uses reduced payloads to keep validation fast.
_SMOKE_SMALL_PAYLOAD_BYTES = 1024  # 1 KB (same as normal)
_SMOKE_MEDIUM_PAYLOAD_BYTES = 64 * 1024  # 64 KB
_SMOKE_LARGE_PAYLOAD_BYTES = 1024 * 1024  # 1 MB

# The CI ratchet measures one payload and compares the Rust-to-Python ratio of
# the same scenario between runs, so the workload has to be one where the
# streaming work dominates the cost that every run pays regardless of payload:
# the interpreter start, the `cuprum` import, and the per-iteration pipeline
# set-up (about 17 ms an iteration, measured on the reference runner). At the
# smoke payloads that fixed cost was the bulk of the measurement, and its
# variance — not the ratio's — decided most comparisons, which made the gate
# flaky (issue #219). At 64 MiB the five iterations stream about 300 ms of
# pure-Python pipeline against about 190 ms of interpreter start, import and
# set-up, so streaming is most of what is timed and a swing in the fixed
# component moves the ratio far less than it did at the smoke payloads, while
# the measurement still fits the job's wall-clock budget (see the tuning
# record cited from docs/cuprum-design.md 13.9).
CI_RATCHET_PAYLOAD_BYTES = 64 * 1024 * 1024  # 64 MB

# Backward-compatible aliases.
_SMOKE_PAYLOAD_BYTES = _SMOKE_SMALL_PAYLOAD_BYTES
_DEFAULT_PAYLOAD_BYTES = _MEDIUM_PAYLOAD_BYTES


def _build_scenarios_for_backend(
    backend: BackendName,
    payloads: tuple[tuple[str, int], ...],
    depths: tuple[tuple[str, int], ...],
    callback_modes: tuple[tuple[str, bool], ...],
) -> list[PipelineBenchmarkScenario]:
    """Build benchmark scenarios for a single backend."""
    scenarios: list[PipelineBenchmarkScenario] = []
    for size_label, payload_bytes in payloads:
        for depth_label, stages in depths:
            for cb_label, with_line_callbacks in callback_modes:
                scenarios.append(
                    PipelineBenchmarkScenario(
                        name=f"{backend}-{size_label}-{depth_label}-{cb_label}",
                        backend=backend,
                        payload_bytes=payload_bytes,
                        stages=stages,
                        with_line_callbacks=with_line_callbacks,
                    ),
                )
    return scenarios


def default_pipeline_scenarios(
    *,
    smoke: bool,
    include_rust: bool,
    ci_ratchet: bool = False,
) -> tuple[PipelineBenchmarkScenario, ...]:
    """Build the default benchmark scenario matrix.

    Parameters
    ----------
    smoke : bool
        Whether to select the reduced smoke-workload payload sizes.
    include_rust : bool
        Whether to include Rust-backend scenarios in the matrix.
    ci_ratchet : bool
        Whether to build the single-payload matrix the CI ratchet measures.
        The ratchet compares one scenario's ratio between runs, so it needs
        the payload where streaming dominates the fixed per-run cost rather
        than the varying payloads a throughput sweep wants; this replaces the
        payload tiers with `CI_RATCHET_PAYLOAD_BYTES` alone.

    Returns
    -------
    tuple[PipelineBenchmarkScenario, ...]
        The full scenario matrix for the selected backends.

    Raises
    ------
    ValueError
        If both ``smoke`` and ``ci_ratchet`` are set, which select
        contradictory workloads.
    """
    if smoke and ci_ratchet:
        msg = "smoke and ci_ratchet select different workloads; pass one"
        raise ValueError(msg)
    if ci_ratchet:
        payloads: tuple[tuple[str, int], ...] = (("ratchet", CI_RATCHET_PAYLOAD_BYTES),)
    else:
        payloads = (
            ("small", _SMOKE_SMALL_PAYLOAD_BYTES if smoke else _SMALL_PAYLOAD_BYTES),
            ("medium", _SMOKE_MEDIUM_PAYLOAD_BYTES if smoke else _MEDIUM_PAYLOAD_BYTES),
            ("large", _SMOKE_LARGE_PAYLOAD_BYTES if smoke else _LARGE_PAYLOAD_BYTES),
        )
    depths: tuple[tuple[str, int], ...] = (
        ("single", 2),
        ("multi", 3),
    )
    callback_modes: tuple[tuple[str, bool], ...] = (
        ("nocb", False),
        ("cb", True),
    )

    backends: list[BackendName] = ["python"]
    if include_rust:
        backends.append("rust")

    scenarios: list[PipelineBenchmarkScenario] = []
    for backend in backends:
        scenarios.extend(
            _build_scenarios_for_backend(backend, payloads, depths, callback_modes),
        )
    return tuple(scenarios)
