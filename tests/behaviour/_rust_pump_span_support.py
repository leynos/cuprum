"""Executor-hop fixtures shared by the tracing behaviour scenarios."""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum import ECHO, ScopeConfig, _rust_backend, scoped, sh
from cuprum._backend import _check_rust_available, get_stream_backend
from cuprum.pump_span_observation import observe_pump_span
from cuprum.unittests._rust_pump_test_helpers import (
    PumpTransfer,
    cancel_fake_pump,
)
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_memory import InMemoryTracer


def run_successful_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a public multi-stage pipeline through the installed Rust backend."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")

    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="rust-pump-span-behaviour",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = echo("-n", "rust-pump-span-behaviour") | python(
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )

    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    try:
        with (
            observe_pump_span(tracer),
            scoped(
                ScopeConfig(allowlist=frozenset((ECHO, python_program))),
            ),
        ):
            result = pipeline.run_sync()
    finally:
        _check_rust_available.cache_clear()
        get_stream_backend.cache_clear()

    if result.stdout != "rust-pump-span-behaviour":
        pytest.fail("the public pipeline must transfer its stdout through the Rust hop")


def run_cancelled_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Cancel a live worker and return its descriptor-ownership ordering."""
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Wait until cancellation permits the worker to return."""
        del reader_fd, writer_fd
        started.set()
        if not release.wait(5.0):
            pytest.fail("harness did not release the native worker")
        events.append("worker_returned")
        return 0

    with observe_pump_span(tracer):
        asyncio.run(
            cancel_fake_pump(
                PumpTransfer(events, started, release),
                pump,
                monkeypatch=monkeypatch,
            )
        )
    return events
