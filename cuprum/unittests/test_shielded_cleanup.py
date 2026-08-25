"""Cleanup must finish before a cancellation arriving mid-cleanup propagates.

A run owns tasks — stream consumers, the stdin writer, background observe-hook
tasks — and reconciles them on the way out. If the caller's cancellation can
cut that reconciliation short, the run propagates ``CancelledError`` while its
own tasks are still live, which is precisely the leak the reconciliation
exists to prevent.

``asyncio.shield`` alone does not buy this. It keeps the cancellation off the
inner coroutine, but the *awaiting* coroutine resumes immediately, so the run
unwinds in parallel with its own cleanup. ``_shielded_cleanup`` owns the
cleanup in a task and re-enters the shielded wait until that task is done.

Every test here is coordinated by an ``asyncio.Event`` or a monkeypatched
cleanup boundary. None waits out a guessed interval: a sleep long enough to be
reliable is slow, and one short enough to be quick is flaky.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum._process_lifecycle import _shielded_cleanup
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecEvent


class _CleanupGate:
    """A cleanup boundary that reports when it starts and blocks until released."""

    def __init__(self) -> None:
        """Start with cleanup neither entered nor released."""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = False

    async def run(self) -> str:
        """Signal entry, block until released, then record completion."""
        self.entered.set()
        await self.release.wait()
        self.completed = True
        return "cleaned"


# -- The primitive ------------------------------------------------------------


class TestShieldedCleanupPrimitive:
    """Behaviour of ``_shielded_cleanup`` itself."""

    def test_shielded_cleanup_holds_the_caller_until_cleanup_finishes(self) -> None:
        """The awaiting coroutine must not unwind while cleanup is still running.

        This is the property a bare ``await asyncio.shield(coro)`` fails to
        provide: the shielded coroutine survives, but the awaiting task resumes at
        once, so the run propagates its cancellation alongside live cleanup.
        """

        async def run_case() -> None:
            """Cancel mid-cleanup and check the waiter stays put until released."""
            gate = _CleanupGate()
            task = asyncio.create_task(_shielded_cleanup(gate.run()))
            await gate.entered.wait()

            task.cancel()
            # Give the cancellation every chance to land and be acted on. If the
            # waiter were going to abandon cleanup, it would have done so by now.
            for _ in range(8):
                await asyncio.sleep(0)
            assert not task.done(), (
                "the caller unwound while cleanup was still running; cleanup's "
                "tasks would outlive the run"
            )
            assert not gate.completed, "the gate should still be held at this point"

            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.completed, "cleanup must have run to completion"

        asyncio.run(run_case())

    def test_shielded_cleanup_survives_repeated_cancellation(self) -> None:
        """Repeated cancellation must not cut the cleanup task short.

        Cancelling a task propagates to whatever future it is awaiting, so a
        handler that re-awaited the cleanup task unshielded would cancel the
        cleanup itself on the second interruption.
        """

        async def run_case() -> None:
            """Cancel three times while cleanup is gated, then release it."""
            gate = _CleanupGate()
            task = asyncio.create_task(_shielded_cleanup(gate.run()))
            await gate.entered.wait()

            for _ in range(3):
                task.cancel()
                for _ in range(4):
                    await asyncio.sleep(0)
                assert not task.done(), (
                    "a cancellation abandoned the cleanup task before it finished"
                )

            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.completed, (
                "cleanup must complete however many cancellations arrive"
            )

        asyncio.run(run_case())

    def test_shielded_cleanup_propagates_failure_when_not_cancelled(self) -> None:
        """An uncancelled caller still sees the cleanup's own failure.

        Callers that must absorb a cleanup failure say so themselves — the
        teardown path passes ``return_exceptions=True`` — so the primitive must not
        swallow anything on their behalf.
        """

        async def run_case() -> None:
            """Await a cleanup that raises and check the error is not swallowed."""

            async def failing() -> None:
                """Fail the way a broken cleanup step would."""
                await asyncio.sleep(0)
                msg = "cleanup boom"
                raise RuntimeError(msg)

            with pytest.raises(RuntimeError, match="cleanup boom"):
                await _shielded_cleanup(failing())

        asyncio.run(run_case())

    def test_shielded_cleanup_returns_the_cleanup_result(self) -> None:
        """The value cleanup produced reaches the caller unchanged."""

        async def run_case() -> None:
            """Await an ungated cleanup and check its return value."""
            gate = _CleanupGate()
            gate.release.set()
            result = await _shielded_cleanup(gate.run())
            assert result == "cleaned", (
                "the cleanup's return value must reach the caller unchanged, got "
                f"{result!r}"
            )

        asyncio.run(run_case())


# -- The single-command run ---------------------------------------------------


class TestSingleCommandRun:
    """A cancelled single-command run reconciles the tasks it owns."""

    def test_streamed_run_drains_its_tasks_before_cancellation_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancelled streamed run reconciles every task it owns first.

        The drain is monkeypatched at its boundary so the cancellation is delivered
        at a known point — after reconciliation has begun — rather than at whatever
        moment a sleep happened to pick.
        """
        from cuprum import _subprocess_wait

        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        cmd = python("-c", "import time; time.sleep(30)")

        gate = _CleanupGate()
        real_drain = _subprocess_wait._drain_stream_consumers
        observed: dict[str, tuple[asyncio.Task[str | None], ...]] = {}

        # Mirrors _drain_stream_consumers exactly — the fixed-length consumer pair
        # and both keyword-only arguments — so the stand-in needs no type
        # suppression to forward its call on.
        async def gated_drain(
            consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
            *,
            pid: int | None = None,
            observation: _StageObservation | None = None,
        ) -> tuple[str | None, str | None]:
            """Announce that the drain started, wait to be released, then drain."""
            observed["consumers"] = consumers
            gate.entered.set()
            await gate.release.wait()
            return await real_drain(consumers, pid=pid, observation=observation)

        monkeypatch.setattr(_subprocess_wait, "_drain_stream_consumers", gated_drain)

        async def run_case() -> None:
            """Cancel the run once its drain is under way, then release it."""
            task = asyncio.create_task(cmd.run(timeout=0.2))
            await gate.entered.wait()

            task.cancel()
            for _ in range(8):
                await asyncio.sleep(0)
            assert not task.done(), (
                "the run unwound while its consumers were still being drained"
            )

            gate.release.set()
            # Narrow deliberately: the shield holds the cancellation off until
            # cleanup finishes and then re-raises it, so that is the only outcome
            # expected. Suppressing BaseException would let a TimeoutExpired — or
            # anything else escaping the run — pass for the cancellation this test
            # is about, and the assertions below would then pass on the wrong path.
            with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
                await task

            for consumer in observed["consumers"]:
                assert consumer.done(), (
                    "every stream consumer the run owned must be settled before "
                    "the cancellation propagates"
                )
            assert not pending_tasks(), f"the run left tasks pending: {pending_tasks()}"

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            asyncio.run(run_case())


# -- Background observe-hook tasks --------------------------------------------


class TestObserveHookDrain:
    """Cancellation must not strand background observe-hook tasks."""

    def test_cancellation_during_hook_drain_leaves_no_hook_task_pending(self) -> None:
        """A cancellation landing on the hook drain must still settle the hooks.

        The observe-hook tasks are scheduled by the runner and owned by it, so
        returning from the drain early strands one task per pending hook.
        """
        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        cmd = python("-c", "print('hi')")

        gate = _CleanupGate()
        started: list[str] = []

        def blocking_hook(ev: ExecEvent) -> cabc.Awaitable[None] | None:
            """Block the ``exit`` phase's hook task until the gate is released."""
            if ev.phase != "exit":
                return None

            async def wait_for_gate() -> None:
                """Hold the hook task open until released."""
                started.append("exit")
                gate.entered.set()
                await gate.release.wait()
                gate.completed = True

            return wait_for_gate()

        async def run_case() -> None:
            """Cancel while the exit hook's task is still in flight."""
            task = asyncio.create_task(cmd.run())
            await gate.entered.wait()

            task.cancel()
            for _ in range(8):
                await asyncio.sleep(0)
            assert not task.done(), (
                "the run unwound while an observe-hook task was still in flight"
            )

            gate.release.set()
            # Narrow deliberately: the shield holds the cancellation off until
            # cleanup finishes and then re-raises it, so that is the only outcome
            # expected. Suppressing BaseException would let a TimeoutExpired — or
            # anything else escaping the run — pass for the cancellation this test
            # is about, and the assertions below would then pass on the wrong path.
            with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
                await task

            assert gate.completed, "the hook task must have been allowed to finish"
            assert not pending_tasks(), (
                f"the run left observe-hook tasks pending: {pending_tasks()}"
            )

        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(blocking_hook),
        ):
            asyncio.run(run_case())

        assert started == ["exit"], "the gated hook must have run exactly once"


# -- The success path ---------------------------------------------------------


class _ConsumerBoomError(RuntimeError):
    """Raised by a stream-consumer double that fails while the run succeeds."""


class TestSuccessPath:
    """Reconciliation on the path where the run itself succeeds."""

    def test_failing_consumer_settles_its_sibling_before_propagating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A consumer failure must not leave the other consumer running.

        The success path gathers both consumers. Without ``return_exceptions`` the
        first failure re-raises immediately and the sibling is never cancelled, so
        a reader blocked on a pipe outlives the run it belonged to — the same leak
        the timeout and cancellation paths reconcile against.
        """
        from cuprum import _subprocess_execution

        catalogue, python_program = python_catalogue()
        python = sh.make(python_program, catalogue=catalogue)
        cmd = python("-c", "print('hi')")

        spawned: dict[str, asyncio.Task[str | None]] = {}

        def failing_consumers(
            *_args: object, **_kwargs: object
        ) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
            """Return one consumer that fails and one that blocks indefinitely."""

            async def boom() -> str | None:
                """Fail the way a broken reader would."""
                await asyncio.sleep(0)
                raise _ConsumerBoomError

            async def blocked() -> str | None:
                """Block as a reader wedged on a pipe that never reaches EOF."""
                await asyncio.Event().wait()
                return None

            spawned["boom"] = asyncio.create_task(boom())
            spawned["blocked"] = asyncio.create_task(blocked())
            return spawned["boom"], spawned["blocked"]

        monkeypatch.setattr(
            _subprocess_execution, "_spawn_stream_consumers", failing_consumers
        )

        async def run_case() -> None:
            """Run to completion and inspect what the consumer failure left behind."""
            with pytest.raises(_ConsumerBoomError):
                await cmd.run()

            assert spawned["blocked"].done(), (
                "the surviving consumer was left running after its sibling failed"
            )
            assert not pending_tasks(), f"the run left tasks pending: {pending_tasks()}"

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            asyncio.run(run_case())
