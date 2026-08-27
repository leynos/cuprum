"""Unit tests for execution-context isolation across threads and tasks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import typing  # ruff: ignore[unconventional-import-alias] - This isolation test intentionally uses the direct module name.

from cuprum.catalogue import ECHO, LS
from cuprum.context import (
    ScopeConfig,
    current_context,
    scoped,
)

if typing.TYPE_CHECKING:
    from cuprum.program import Program


# =============================================================================
# CuprumContext Basics
# =============================================================================


def test_context_is_isolated_per_thread() -> None:
    """Each thread has its own context."""
    # Both workers rendezvous inside their scoped blocks so the two scopes
    # provably overlap; the bounded wait keeps a wedged worker from stalling
    # the suite indefinitely.
    scopes_overlap = threading.Barrier(2)

    def thread_worker(programs: frozenset[Program]) -> bool:
        """Return the allowlist decision observed inside the worker thread."""
        with scoped(ScopeConfig(allowlist=programs)):
            scopes_overlap.wait(timeout=5.0)
            return current_context().is_allowed(ECHO)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, frozenset([ECHO]))
        f2 = executor.submit(thread_worker, frozenset([LS]))
        results = {"thread1": f1.result(), "thread2": f2.result()}

    assert results["thread1"] is True, (
        "thread 1 must retain its ECHO allowlist in its isolated context"
    )
    assert results["thread2"] is False, (
        "thread 2 must retain its LS-only allowlist in its isolated context"
    )


def test_context_is_isolated_per_async_task() -> None:
    """Each async task has its own context."""

    async def task_worker(programs: frozenset[Program]) -> bool:
        """Return the allowlist decision observed inside the async task."""
        with scoped(ScopeConfig(allowlist=programs)):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            return current_context().is_allowed(ECHO)

    async def run_tasks() -> tuple[bool, bool]:
        """Run both task workers concurrently to interleave their scopes."""
        return await asyncio.gather(
            task_worker(frozenset([ECHO])),
            task_worker(frozenset([LS])),
        )

    task1_result, task2_result = asyncio.run(run_tasks())
    results = {"task1": task1_result, "task2": task2_result}

    assert results["task1"] is True, (
        "task 1 must retain its ECHO allowlist in its isolated context"
    )
    assert results["task2"] is False, (
        "task 2 must retain its LS-only allowlist in its isolated context"
    )
