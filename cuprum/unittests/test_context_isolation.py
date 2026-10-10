"""Unit tests for execution-context isolation across threads and tasks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import typing as typ

from cuprum.catalogue import ECHO, LS, ProgramCatalogue
from cuprum.context import (
    EnvMode,
    ScopeConfig,
    current_context,
    env,
    scoped,
)
from cuprum.context.registration import bind_executable

if typ.TYPE_CHECKING:
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


def test_environment_policies_are_isolated_per_async_task() -> None:
    """Concurrent tasks retain only their own environment policy."""
    first = "CUPRUM_TEST_ASYNC_ENV_FIRST"
    second = "CUPRUM_TEST_ASYNC_ENV_SECOND"

    async def task_worker(name: str, value: str) -> dict[str, object]:
        """Return the policy visible after an intentional scheduling yield."""
        with env({name: value}, mode=EnvMode.REPLACE):
            await asyncio.sleep(0.01)
            context = current_context()
            return {
                "mode": context.env_mode,
                "overlay": dict(context.env_overlay or {}),
            }

    async def run_tasks() -> tuple[dict[str, object], dict[str, object]]:
        """Run two overlapping environment-policy scopes."""
        return await asyncio.gather(
            task_worker(first, "one"),
            task_worker(second, "two"),
        )

    first_result, second_result = asyncio.run(run_tasks())
    results = {"first": first_result, "second": second_result}

    assert results["first"] == {"mode": EnvMode.REPLACE, "overlay": {first: "one"}}, (
        "the first task must not observe the second task's replacement policy"
    )
    assert results["second"] == {"mode": EnvMode.REPLACE, "overlay": {second: "two"}}, (
        "the second task must not observe the first task's replacement policy"
    )


def test_scoped_catalogue_is_isolated_per_thread() -> None:
    """Each thread resolves the catalogue of its own scope."""
    echo_catalogue = ProgramCatalogue.from_programs(ECHO)
    ls_catalogue = ProgramCatalogue.from_programs(LS)
    # Both workers rendezvous inside their scoped blocks so the two scopes
    # provably overlap; the bounded wait keeps a wedged worker from stalling
    # the suite indefinitely.
    scopes_overlap = threading.Barrier(2)

    def thread_worker(catalogue: ProgramCatalogue) -> ProgramCatalogue | None:
        """Return the catalogue observed inside the worker thread."""
        with scoped(catalogue=catalogue):
            scopes_overlap.wait(timeout=5.0)
            return current_context().catalogue

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(thread_worker, echo_catalogue)
        f2 = executor.submit(thread_worker, ls_catalogue)
        observed = {"thread1": f1.result(), "thread2": f2.result()}

    assert observed["thread1"] is echo_catalogue, (
        "thread 1 must retain its own scoped catalogue"
    )
    assert observed["thread2"] is ls_catalogue, (
        "thread 2 must retain its own scoped catalogue"
    )
    assert current_context().catalogue is None, (
        "worker catalogues must not leak into the calling thread's context"
    )


def test_scoped_catalogue_is_isolated_per_async_task() -> None:
    """Each async task resolves the catalogue of its own scope."""
    echo_catalogue = ProgramCatalogue.from_programs(ECHO)
    ls_catalogue = ProgramCatalogue.from_programs(LS)

    async def task_worker(catalogue: ProgramCatalogue) -> ProgramCatalogue | None:
        """Return the catalogue observed inside the async task."""
        with scoped(catalogue=catalogue):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            return current_context().catalogue

    async def run_tasks() -> tuple[ProgramCatalogue | None, ProgramCatalogue | None]:
        """Run both task workers concurrently to interleave their scopes."""
        return await asyncio.gather(
            task_worker(echo_catalogue),
            task_worker(ls_catalogue),
        )

    task1_catalogue, task2_catalogue = asyncio.run(run_tasks())

    assert task1_catalogue is echo_catalogue, (
        "task 1 must retain its own scoped catalogue"
    )
    assert task2_catalogue is ls_catalogue, (
        "task 2 must retain its own scoped catalogue"
    )
    assert current_context().catalogue is None, (
        "task catalogues must not leak into the calling context"
    )


def test_executable_bindings_are_isolated_per_thread() -> None:
    """Each thread resolves only its own binding for the same program.

    Two workers bind the *same* program to different executables and overlap
    inside their scopes, so a binding that leaked through the ``ContextVar``
    would be observed as the other thread's path. Each worker additionally
    reports the binding for a program it did not bind, which must stay absent.
    """
    scopes_overlap = threading.Barrier(2)

    def thread_worker(executable: str) -> tuple[str | None, str | None]:
        """Return the worker's own binding and the other program's binding."""
        with (
            scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))),
            bind_executable(ECHO, executable),
        ):
            scopes_overlap.wait(timeout=5.0)
            context = current_context()
            return (
                context.resolve_executable(ECHO, cwd=None),
                context.resolve_executable(LS, cwd=None),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(thread_worker, "/opt/tools/thread-one")
        second = executor.submit(thread_worker, "/opt/tools/thread-two")
        results = {"first": first.result(), "second": second.result()}

    assert results["first"] == ("/opt/tools/thread-one", None), (
        "the first thread must see only its own binding"
    )
    assert results["second"] == ("/opt/tools/thread-two", None), (
        "the second thread must see only its own binding"
    )


def test_executable_bindings_are_isolated_per_async_task() -> None:
    """Concurrent tasks resolve only their own binding for one program.

    The async counterpart of the thread example, and not redundant with it:
    tasks share a thread but each runs in its own copied context, so this is
    the case where a binding stored on the thread rather than the context would
    still pass the thread example and fail here.
    """

    async def task_worker(executable: str) -> tuple[str | None, str | None]:
        """Return the task's own binding and the other program's binding."""
        with (
            scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))),
            bind_executable(ECHO, executable),
        ):
            await asyncio.sleep(0.01)  # Yield to allow interleaving
            context = current_context()
            return (
                context.resolve_executable(ECHO, cwd=None),
                context.resolve_executable(LS, cwd=None),
            )

    async def run_tasks() -> tuple[
        tuple[str | None, str | None],
        tuple[str | None, str | None],
    ]:
        """Run both task workers concurrently to interleave their scopes."""
        return await asyncio.gather(
            task_worker("/opt/tools/task-one"),
            task_worker("/opt/tools/task-two"),
        )

    first_result, second_result = asyncio.run(run_tasks())

    assert first_result == ("/opt/tools/task-one", None), (
        "the first task must see only its own binding"
    )
    assert second_result == ("/opt/tools/task-two", None), (
        "the second task must see only its own binding"
    )


def test_a_child_scope_binding_leaves_sibling_bindings_intact() -> None:
    """A nested binding for one program is absent again once it is left.

    Nesting, not concurrency: an inner scope rebinds ``ECHO`` for its own block
    while ``LS`` keeps its outer binding throughout, and leaving the inner block
    restores the outer executable rather than dropping the layer. The
    assertions read both programs at all three points, so a nested registration
    that replaced the whole layer instead of overlaid one key is caught.
    """
    with (
        scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))),
        bind_executable(ECHO, "/opt/tools/outer-echo"),
        bind_executable(LS, "/opt/tools/ls"),
    ):
        assert (
            current_context().resolve_executable(ECHO, cwd=None),
            current_context().resolve_executable(LS, cwd=None),
        ) == ("/opt/tools/outer-echo", "/opt/tools/ls"), (
            "the outer scope must bind both programs"
        )

        with bind_executable(ECHO, "/opt/tools/inner-echo"):
            assert (
                current_context().resolve_executable(ECHO, cwd=None),
                current_context().resolve_executable(LS, cwd=None),
            ) == ("/opt/tools/inner-echo", "/opt/tools/ls"), (
                "the inner scope must override ECHO and leave the sibling alone"
            )

        assert (
            current_context().resolve_executable(ECHO, cwd=None),
            current_context().resolve_executable(LS, cwd=None),
        ) == ("/opt/tools/outer-echo", "/opt/tools/ls"), (
            "leaving the inner scope must restore the outer binding"
        )
