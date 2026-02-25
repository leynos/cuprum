"""Microbenchmarks for stream pumping and stream consumption throughput."""

from __future__ import annotations

import collections.abc as cabc
import os
import sys

import pytest

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh
from tests.helpers.parity import parity_catalogue, run_parity_pipeline

if os.environ.get("CUPRUM_RUN_BENCHMARKS") != "1":
    pytest.skip(
        "benchmark module is opt-in; set CUPRUM_RUN_BENCHMARKS=1 to execute",
        allow_module_level=True,
    )

type _StrBenchmark = cabc.Callable[[cabc.Callable[[], str]], str]
type _IntBenchmark = cabc.Callable[[cabc.Callable[[], int]], int]


def _pump_pipeline(
    *,
    payload_bytes: int,
) -> tuple[sh.Pipeline, frozenset[Program], str]:
    """Build a two-stage payload pipeline for pump latency measurements."""
    payload = "x" * payload_bytes
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )
    catalogue, python_prog, cat_prog, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)
    pipeline = python_cmd("-c", script) | cat_cmd()
    return pipeline, frozenset([python_prog, cat_prog]), payload


def _consume_command(
    *,
    payload_bytes: int,
) -> tuple[sh.SafeCmd, frozenset[Program], int]:
    """Build a single command that emits a payload for consume benchmarks."""
    python_program = Program(sys.executable)
    catalogue = ProgramCatalogue(
        projects=(
            ProjectSettings(
                name="benchmark-microconsume",
                programs=(python_program,),
                documentation_locations=(),
                noise_rules=(),
            ),
        ),
    )
    python_cmd = sh.make(python_program, catalogue=catalogue)
    script = f"import sys; sys.stdout.write('x' * {payload_bytes})"
    return python_cmd("-c", script), frozenset([python_program]), payload_bytes


@pytest.mark.benchmark(group="pump-latency")
def test_benchmark_pump_latency(
    benchmark: _StrBenchmark,
    stream_backend: str,
) -> None:
    """Benchmark small-payload pipeline pump latency."""
    pipeline, allowlist, expected = _pump_pipeline(payload_bytes=1024)

    def run_once() -> str:
        result = run_parity_pipeline(pipeline, allowlist)
        assert result.ok
        assert result.stdout is not None
        return result.stdout

    output = benchmark(run_once)
    assert output == expected


@pytest.mark.benchmark(group="consume-throughput")
def test_benchmark_consume_throughput(
    benchmark: _IntBenchmark,
) -> None:
    """Benchmark captured stdout throughput for a medium payload."""
    command, allowlist, payload_bytes = _consume_command(payload_bytes=1024 * 1024)

    def run_once() -> int:
        with scoped(ScopeConfig(allowlist=allowlist)):
            result = command.run_sync(capture=True, echo=False)
        assert result.ok
        assert result.stdout is not None
        return len(result.stdout)

    consumed = benchmark(run_once)
    assert consumed == payload_bytes
