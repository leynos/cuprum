"""Support helpers for the context-hooks behaviour tests.

This module holds non-test helper code extracted from
``test_context_hooks.py`` so the collected test module stays within the
project's per-file line limit. It is deliberately named with a leading
underscore so pytest does not collect it as a test module.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import typing as typ

from cuprum.catalogue import ECHO, LS
from cuprum.context import CuprumContext, ScopeConfig, current_context, scoped

if typ.TYPE_CHECKING:
    from cuprum.context import AfterHook, BeforeHook
    from cuprum.program import Program

type AllowlistResults = dict[str, tuple[bool, bool]]


def build_before_hook_context(call_order: list[int]) -> CuprumContext:
    """Build a context whose before hooks append their index in order.

    Parameters
    ----------
    call_order
        A list that each hook appends its ordinal to when invoked.

    Returns
    -------
    CuprumContext
        A context registering three ordered before hooks.
    """

    def hook1(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(1)

    def hook2(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(2)

    def hook3(cmd: object) -> None:
        _ = cmd  # Unused
        call_order.append(3)

    return CuprumContext(
        before_hooks=(
            typ.cast("BeforeHook", hook1),
            typ.cast("BeforeHook", hook2),
            typ.cast("BeforeHook", hook3),
        ),
    )


def build_after_hook_context(call_order: list[int]) -> CuprumContext:
    """Build a context whose after hooks append their index in reverse.

    After hooks are stored in reverse order (inner-to-outer), so the
    resulting tuple runs ``hook3``, ``hook2`` then ``hook1``.

    Parameters
    ----------
    call_order
        A list that each hook appends its ordinal to when invoked.

    Returns
    -------
    CuprumContext
        A context registering three ordered after hooks.
    """

    def hook1(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(1)

    def hook2(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(2)

    def hook3(cmd: object, result: object) -> None:
        _, _ = cmd, result  # Unused
        call_order.append(3)

    return CuprumContext(
        after_hooks=(
            typ.cast("AfterHook", hook3),
            typ.cast("AfterHook", hook2),
            typ.cast("AfterHook", hook1),
        ),
    )


def run_threaded_allowlist_checks(
    thread_setup: dict[str, frozenset[Program]],
) -> AllowlistResults:
    """Run two threads that each scope and check their allowlist.

    Parameters
    ----------
    thread_setup
        Per-thread allowlists keyed by thread name.

    Returns
    -------
    dict[str, tuple[bool, bool]]
        Each thread's ``(echo_allowed, ls_allowed)`` result by name.
    """
    # The barrier holds both threads inside their own ``scoped`` block at the
    # same time, so the allowlists are genuinely concurrent rather than merely
    # sequential. Each worker owns its result and hands it back through its
    # Future, so no state is shared across the executor threads.
    barrier = threading.Barrier(2, timeout=5.0)

    def thread_worker(programs: frozenset[Program]) -> tuple[bool, bool]:
        with scoped(ScopeConfig(allowlist=programs)):
            barrier.wait()
            ctx = current_context()
            return (ctx.is_allowed(ECHO), ctx.is_allowed(LS))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            name: executor.submit(thread_worker, programs)
            for name, programs in thread_setup.items()
        }
        return {name: future.result() for name, future in futures.items()}


def run_async_allowlist_checks(
    async_setup: dict[str, frozenset[Program]],
) -> AllowlistResults:
    """Run two async tasks that each scope and check their allowlist.

    Parameters
    ----------
    async_setup
        Per-task allowlists keyed by task name.

    Returns
    -------
    dict[str, tuple[bool, bool]]
        Each task's ``(echo_allowed, ls_allowed)`` result by name.
    """

    # Each task owns its own result and returns it, mirroring the threaded
    # sibling above; the mapping is assembled by the awaiting coroutine so no
    # state is shared between the concurrent tasks.
    async def task_worker(
        name: str,
        programs: frozenset[Program],
    ) -> tuple[str, tuple[bool, bool]]:
        with scoped(ScopeConfig(allowlist=programs)):
            await asyncio.sleep(0.01)  # Allow interleaving
            ctx = current_context()
            return (name, (ctx.is_allowed(ECHO), ctx.is_allowed(LS)))

    async def run_tasks() -> AllowlistResults:
        collected = await asyncio.gather(
            task_worker("task1", async_setup["task1"]),
            task_worker("task2", async_setup["task2"]),
        )
        return dict(collected)

    return asyncio.run(run_tasks())
