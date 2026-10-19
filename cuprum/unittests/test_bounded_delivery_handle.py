"""Owned command handles settle bounded-delivery diagnostics before results."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

import cuprum._bounded_delivery as bounded_delivery
from cuprum import ScopeConfig, scoped, sh
from cuprum._process_handle import owned_start
from cuprum.echo_events import EchoErrorCategory, EchoStream, RelayFallback
from cuprum.sh import BoundedDelivery, ExecutionContext, RunOutputOptions
from cuprum.unittests._sink_test_support import BlockedSink, RecordingSink
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum._bounded_delivery import _DeliveryWorker
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd


def _python_command(script: str) -> tuple[Program, SafeCmd]:
    """Build a catalogue-bound Python command for one handle test."""
    catalogue, program = python_catalogue()
    return program, sh.make(program, catalogue=catalogue)("-c", script)


class _FailingSink(io.StringIO):
    """Fail one worker operation while leaving capture independent."""

    def __init__(self, failure_site: str) -> None:
        """Select which destination operation raises."""
        super().__init__()
        self.failure_site = failure_site

    def write(self, text: str, /) -> int:
        """Fail the configured write or retain the text."""
        if self.failure_site == "write":
            raise OSError
        return super().write(text)

    def flush(self) -> None:
        """Fail the configured flush or preserve StringIO semantics."""
        if self.failure_site == "flush":
            raise OSError
        super().flush()


@pytest.mark.parametrize("failure_site", ["write", "flush"])
def test_handle_result_and_after_hook_include_delivery_failure(
    failure_site: str,
) -> None:
    """Handle results and hooks include failures found during worker close."""
    program, command = _python_command("import sys; print('captured', flush=True)")
    sink = _FailingSink(failure_site)
    hook_results: list[CommandResult] = []

    async def run_handle() -> CommandResult:
        """Start and settle under the hook that records the exact result."""
        with scoped(
            ScopeConfig(
                allowlist=frozenset([program]),
                after_hooks=(lambda _cmd, result: hook_results.append(result),),
            ),
        ):
            handle = await command.start(
                output=RunOutputOptions(
                    capture=True,
                    echo_stdout=True,
                    echo_stderr=False,
                    delivery=BoundedDelivery(max_queued_bytes=128),
                ),
                context=ExecutionContext(stdout_sink=sink),
            )
            return await handle.wait()

    result = asyncio.run(run_handle())

    assert result.stdout == "captured\n"
    assert result.relay_fallbacks == (
        RelayFallback(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.DELIVERY_ERROR,
        ),
    )
    assert hook_results == [result]
    assert hook_results[0].relay_fallbacks == result.relay_fallbacks


def test_handle_reports_blocked_delivery_before_after_hook() -> None:
    """A handle result records a blocked write when its worker grace expires."""
    program, command = _python_command(
        "import time; print('blocked', flush=True); time.sleep(30)",
    )
    sink = BlockedSink()
    hook_results: list[CommandResult] = []

    async def settle_handle() -> CommandResult:
        """Stop the child after its presentation worker enters the sink."""
        with scoped(
            ScopeConfig(
                allowlist=frozenset([program]),
                after_hooks=(lambda _cmd, result: hook_results.append(result),),
            ),
        ):
            handle = await command.start(
                output=RunOutputOptions(
                    capture=True,
                    echo_stdout=True,
                    echo_stderr=False,
                    delivery=BoundedDelivery(
                        max_queued_bytes=128,
                        drain_grace_s=0.02,
                    ),
                ),
                context=ExecutionContext(
                    stdout_sink=sink,
                    cancel_grace=0.1,
                ),
            )
            assert await asyncio.to_thread(sink.started.wait, 3.0), (
                "the worker must enter the blocked destination"
            )
            return await handle.terminate()

    try:
        result = asyncio.run(settle_handle())
    finally:
        sink.release.set()
        assert sink.finished.wait(timeout=2.0), "the test must release its writer"

    assert result.stdout == "blocked\n"
    assert result.relay_fallbacks == (
        RelayFallback(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.UNDELIVERED,
        ),
    )
    assert hook_results == [result]


def test_handle_spawn_failure_closes_prepared_delivery_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after handle preparation closes its idle delivery worker."""
    program, command = _python_command("print('unused')")
    sink = RecordingSink()
    created_workers: list[_DeliveryWorker] = []
    original_worker = bounded_delivery._DeliveryWorker
    run_error = RuntimeError("handle spawn failed")

    def record_worker_creation(
        destinations: dict[EchoStream, typ.IO[str] | typ.BinaryIO],
        max_queued_bytes: int,
    ) -> _DeliveryWorker:
        """Keep a reference to the real worker created during preparation."""
        worker = original_worker(destinations, max_queued_bytes)
        created_workers.append(worker)
        return worker

    def fail_handle_spawn(*_args: object, **_kwargs: object) -> None:
        """Fail after preparation has returned the worker owner."""
        raise run_error

    monkeypatch.setattr(
        bounded_delivery,
        "_DeliveryWorker",
        record_worker_creation,
    )
    monkeypatch.setattr(owned_start, "_start_handle_run", fail_handle_spawn)

    with (
        scoped(ScopeConfig(allowlist=frozenset([program]))),
        pytest.raises(RuntimeError) as exc_info,
    ):
        asyncio.run(
            command.start(
                output=RunOutputOptions(
                    sink=sink,
                    delivery=BoundedDelivery(max_queued_bytes=128),
                ),
            ),
        )

    assert exc_info.value is run_error
    assert len(created_workers) == 1
    created_workers[0]._thread.join(timeout=1.0)
    assert not created_workers[0]._thread.is_alive()
    assert sink.last_session.closed == 1
