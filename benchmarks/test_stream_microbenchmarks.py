"""Microbenchmarks for stream pumping, consumption, and observe dispatch."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import os
import sys
import threading
import typing as typ

import pytest

from cuprum import (
    ECHO,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    RunOutputOptions,
    ScopeConfig,
    scoped,
    sh,
)
from cuprum._observability import _emit_exec_event
from cuprum.events import ExecEvent
from cuprum.unittests._rust_stream_test_support import _safe_close
from tests.helpers.parity import parity_catalogue

if typ.TYPE_CHECKING:
    from types import ModuleType

    from cuprum.events import ExecHook, ExecPhase
    from cuprum.sh import PipelineResult

if os.environ.get("CUPRUM_RUN_BENCHMARKS") != "1":
    pytest.skip(
        "benchmark module is opt-in; set CUPRUM_RUN_BENCHMARKS=1 to execute",
        allow_module_level=True,
    )

type _PipelineResultBenchmark = cabc.Callable[
    [cabc.Callable[[], PipelineResult]],
    PipelineResult,
]
type _IntBenchmark = cabc.Callable[[cabc.Callable[[], int]], int]
type _StrBenchmark = cabc.Callable[[cabc.Callable[[], str]], str]


def _write_and_close(fd: int, payload: bytes) -> None:
    """Write `payload` to `fd` and close the writer end.

    The reader closes its end first when consumption fails, so a broken pipe is
    the expected way for the write loop to stop with payload left over.
    """
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            assert written > 0, "expected os.write to make progress"
            view = view[written:]
    except BrokenPipeError:
        pass
    finally:
        _safe_close(fd)


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
    benchmark: _PipelineResultBenchmark,
) -> None:
    """Benchmark small-payload pipeline pump latency."""
    pipeline, allowlist, expected = _pump_pipeline(payload_bytes=1024)

    def run_once() -> PipelineResult:
        """Run the pipeline once with capture enabled."""
        with scoped(ScopeConfig(allowlist=allowlist)):
            return pipeline.run_sync(output=RunOutputOptions(capture=True))

    result = benchmark(run_once)
    exit_codes = [stage.exit_code for stage in result.stages]
    assert result.ok, f"pipeline failed with exit_codes={exit_codes}"
    assert result.stdout is not None, (
        f"pipeline captured no stdout with exit_codes={exit_codes}"
    )
    output = result.stdout
    assert output == expected, (
        f"expected output length={len(expected)}, got {len(output)}"
    )


@pytest.mark.benchmark(group="consume-throughput")
def test_benchmark_consume_throughput(
    benchmark: _IntBenchmark,
) -> None:
    """Benchmark captured stdout throughput for a medium payload."""
    command, allowlist, payload_bytes = _consume_command(payload_bytes=1024 * 1024)

    def run_once() -> int:
        """Run the consume command once and report its captured byte count."""
        with scoped(ScopeConfig(allowlist=allowlist)):
            result = command.run_sync(output=RunOutputOptions(capture=True))
        assert result.ok, f"command failed with exit_code={result.exit_code}"
        assert result.stdout is not None, (
            f"command captured no stdout with exit_code={result.exit_code}"
        )
        return len(result.stdout)

    consumed = benchmark(run_once)
    assert consumed == payload_bytes, (
        f"expected {payload_bytes} consumed bytes, got {consumed}"
    )


# Roadmap 5.2.2 removed the unconditional ``inspect.isawaitable`` call from
# the per-line dispatcher. Each benchmark call below dispatches this many
# events, so the reported mean is divided by it to get the per-event figure:
# one event per call would measure pytest-benchmark's own loop entry instead
# of the dispatcher.
_DISPATCH_BATCH = 1_000


def _dispatch_event() -> ExecEvent:
    """Return a per-line ``stdout`` event for the dispatcher benchmark."""
    return ExecEvent(
        phase=typ.cast("ExecPhase", "stdout"),
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=0,
        timestamp=0.0,
        line="benchmark line",
        exit_code=None,
        duration_s=None,
        tags={},
    )


def _sync_observe_hook(_event: ExecEvent) -> None:
    """Stand in for the synchronous hooks that dominate per-line dispatch."""


async def _async_observe_hook(_event: ExecEvent) -> None:
    """Stand in for an ``async def`` observe hook."""


@pytest.fixture(scope="module")
def dispatch_loop() -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide one event loop, so a batch amortizes loop entry, not dispatch."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def _dispatch_batch(
    loop: asyncio.AbstractEventLoop,
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> int:
    """Dispatch ``_DISPATCH_BATCH`` events and return the count dispatched."""

    async def _drive() -> None:
        """Dispatch one batch, then settle every scheduled task."""
        # Tasks accumulate, as they do in production, and settle once at the
        # end, so the batch times dispatch rather than task completion.
        tasks: list[asyncio.Task[None]] = []
        for _ in range(_DISPATCH_BATCH):
            tasks.extend(_emit_exec_event(hooks, event))
        if tasks:
            await asyncio.gather(*tasks)

    loop.run_until_complete(_drive())
    return _DISPATCH_BATCH


@pytest.mark.skipif(
    os.name == "nt",
    reason="raw Windows handles cannot establish the synchronous native-I/O capability",
)
@pytest.mark.benchmark(group="consume-invalid-utf8")
@pytest.mark.parametrize(
    "payload_bytes",
    [64 * 1024, 256 * 1024, 1024 * 1024],
    ids=["64KiB", "256KiB", "1MiB"],
)
def test_benchmark_consume_invalid_utf8(
    benchmark: _StrBenchmark,
    rust_streams: ModuleType,
    payload_bytes: int,
) -> None:
    """Benchmark end-to-end consumption of an all-invalid UTF-8 payload.

    A large payload cannot be written to a pipe before the consumer starts
    without blocking, so a writer thread feeds it while the extension reads.
    The buffer size equals the payload size, so the consumer's own buffering
    never caps a read below what the kernel delivers. Pipe capacity and writer
    scheduling still decide how the payload is split, so this measures
    throughput through the whole consumer loop rather than any single read.
    Each case asserts the decoded text still matches Python's ``replace``
    semantics, so the benchmark cannot silently measure a decoder that returns
    the wrong answer quickly.
    """
    payload = b"\xff" * payload_bytes
    expected = payload.decode("utf-8", errors="replace")

    def run_once() -> str:
        """Consume the payload once and return the decoded text."""
        read_fd, write_fd = os.pipe()
        writer = threading.Thread(
            target=_write_and_close,
            args=(write_fd, payload),
            daemon=True,
        )
        writer.start()
        try:
            return typ.cast(
                "str",
                rust_streams.rust_consume_stream(
                    read_fd,
                    buffer_size=payload_bytes,
                ),
            )
        finally:
            # Close the reader before joining. If consumption failed before
            # draining the pipe, the writer is blocked with payload left over,
            # and only losing its reader will stop it. The extension may have
            # closed the descriptor already, which `_safe_close` tolerates.
            _safe_close(read_fd)
            writer.join()

    output = benchmark(run_once)
    assert output == expected, (
        f"invalid UTF-8 payload of {payload_bytes} bytes must decode like "
        "Python's replace semantics"
    )


@pytest.mark.benchmark(group="observe-dispatch")
@pytest.mark.parametrize(
    "hook",
    [_sync_observe_hook, _async_observe_hook],
    ids=["sync", "async"],
)
def test_benchmark_observe_dispatch(
    benchmark: _IntBenchmark,
    dispatch_loop: asyncio.AbstractEventLoop,
    hook: ExecHook,
) -> None:
    """Benchmark per-line dispatch for a ``None`` and an ``async def`` hook."""
    event = _dispatch_event()

    def dispatch_batch() -> int:
        """Dispatch one batch for the hook under test."""
        return _dispatch_batch(dispatch_loop, (hook,), event)

    dispatched = benchmark(dispatch_batch)
    assert dispatched == _DISPATCH_BATCH, (
        f"expected {_DISPATCH_BATCH} dispatches, got {dispatched}"
    )
