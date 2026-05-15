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
) -> tuple[PipelineBenchmarkScenario, ...]:
    """Build the default benchmark scenario matrix."""
    payloads: tuple[tuple[str, int], ...] = (
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
