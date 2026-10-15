"""Regression coverage for settlement tasks owned by replaced model handles."""

from __future__ import annotations

import asyncio
import signal

import pytest

if not hasattr(signal, "SIGKILL"):
    pytest.skip("the settlement doubles require POSIX signals", allow_module_level=True)

from cuprum.unittests._running_command_settlement_harness_support import (
    _SettlementHarness,
)
from cuprum.unittests._running_command_support import (
    _StubProcess,
)


def test_teardown_drains_settlements_from_replaced_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the current handle must not abandon an earlier settlement."""
    harness = _SettlementHarness()
    loop = harness._loop
    close_loop = loop.close
    abandoned: list[str] = []

    def close_after_rescuing_tasks() -> None:
        """Record leaks and drain them so a failing test leaves no orphan tasks."""
        pending = asyncio.all_tasks(loop)
        abandoned.extend(task.get_name() for task in pending)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        close_loop()

    monkeypatch.setattr(loop, "close", close_after_rescuing_tasks)
    try:
        original = harness._handle
        waiter = loop.create_task(original.wait())
        harness._drive(asyncio.sleep(0))
        waiter.cancel()
        harness._drive(asyncio.gather(waiter, return_exceptions=True))
        settling = original._settlement._task
        assert settling is not None
        assert not settling.done()
        harness._handle = harness._assembled(_StubProcess())
        harness.teardown()
        assert not abandoned, "every model-owned settlement must finish before close"
        assert settling.done()
    finally:
        if not loop.is_closed():
            harness.teardown()
