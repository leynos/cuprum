"""Unit tests for execution-context isolation across threads and tasks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import typing as typ

from hypothesis import settings

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    ScopeConfig,
    current_context,
    scoped,
)

if typ.TYPE_CHECKING:
    from cuprum.program import Program

_PROPERTY_SETTINGS = settings(derandomize=True, deadline=None, max_examples=50)


# =============================================================================
# CuprumContext Basics
# =============================================================================


def test_context_is_isolated_per_thread() -> None:
    """Each thread has its own context."""
    results: dict[str, bool] = {}

    def thread_worker(name: str, programs: frozenset[Program]) -> None:
        """Capture the allowlist decision observed inside the worker thread."""
        with scoped(ScopeConfig(allowlist=programs)):
            results[name] = current_context().is_allowed(ECHO)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, "thread1", frozenset([ECHO]))
        f2 = executor.submit(thread_worker, "thread2", frozenset([LS]))
        f1.result()
        f2.result()

    assert results["thread1"] is True
    assert results["thread2"] is False


def test_context_is_isolated_per_async_task() -> None:
    """Each async task has its own context."""
    results: dict[str, bool] = {}

    async def task_worker(name: str, programs: frozenset[Program]) -> None:
        """Capture the allowlist decision observed inside the async task."""
        with scoped(ScopeConfig(allowlist=programs)):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            results[name] = current_context().is_allowed(ECHO)

    async def run_tasks() -> None:
        """Run both task workers concurrently to interleave their scopes."""
        await asyncio.gather(
            task_worker("task1", frozenset([ECHO])),
            task_worker("task2", frozenset([LS])),
        )

    asyncio.run(run_tasks())

    assert results["task1"] is True
    assert results["task2"] is False
