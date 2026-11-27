"""A settled consumer is classified before the post-exit wait drops it.

A consumer can settle after ``asyncio.wait`` computes its completion set but
before the done callback that records completion order runs. The wait must
classify every settled consumer before removing it; otherwise a cancellation
or a preserving partial capture can pass as a quiet finish and surface later
through a gather as a raw ``CancelledError`` or as success.

These cases settle the consumer from inside the wait seam, so the gap is
deterministic rather than a race against the loop. The last four drive the
same seam through ``run()``'s and ``lines()``' post-exit collection.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ

import pytest

from cuprum import ConsumerCancelledError
from cuprum._line_stream import drain as line_drain
from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum._subprocess_stream_run import _collect_stream_output
from cuprum._subprocess_task_outcomes import (
    _await_consumer_outcomes,
    _wait_for_consumer_completion,
)
from cuprum._subprocess_wait_types import _RunTaskOwnership
from cuprum.unittests._rendezvous_support import CallbackError

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _ConsumerPair

# A watchdog for the entry-point cases, which must never wait on a real child.
_WATCHDOG_S = 5.0


def _future() -> asyncio.Future[_CapturedDrain]:
    """Return an unsettled future standing in for one consumer."""
    return asyncio.get_running_loop().create_future()


def _settle(future: asyncio.Future[_CapturedDrain], outcome: str) -> None:
    """Settle *future* in the named outcome, without yielding to the loop."""
    if outcome == "cancelled":
        future.cancel()
    elif outcome == "partial":
        future.set_result(_CapturedDrain(payload="partial", reached_eof=False))
    else:
        future.set_result(_CapturedDrain(payload="complete"))


def _settle_during_wait(
    monkeypatch: pytest.MonkeyPatch,
    target: asyncio.Future[_CapturedDrain],
    outcome: str,
) -> None:
    """Settle *target* from inside the wait, before its callback can run.

    The done callback is scheduled, not run, when the seam returns, so the
    wait sees the consumer as settled without having recorded it.
    """
    settled = False

    async def wait_then_settle(
        pending: set[asyncio.Task[object]],
        deadline: float | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Settle the target once, then delegate any further wait."""
        nonlocal settled
        if not settled:
            settled = True
            _settle(target, outcome)
            return
        await _wait_for_consumer_completion(pending, deadline, loop)

    monkeypatch.setattr(
        "cuprum._subprocess_task_outcomes._wait_for_consumer_completion",
        wait_then_settle,
    )


def _consumers(
    first: asyncio.Future[_CapturedDrain],
    second: asyncio.Future[_CapturedDrain] | None = None,
) -> tuple[asyncio.Task[object] | None, ...]:
    """Cast futures into the consumer pair the wait consumes."""
    pair = (first, second)
    return typ.cast("tuple[asyncio.Task[object] | None, ...]", pair)


@pytest.mark.parametrize("outcome", ["cancelled", "partial"])
def test_a_settled_consumer_without_its_callback_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A consumer that settled before its callback is still a failure."""

    async def scenario() -> BaseException | None:
        """Settle stdout inside the wait and leave stderr pending."""
        stdout = _future()
        stderr = _future()
        _settle_during_wait(monkeypatch, stdout, outcome)
        try:
            return await _await_consumer_outcomes(
                _consumers(stdout, stderr),
                budget=0.05,
            )
        finally:
            # The wait returns on the classification, so the pending sibling is
            # never cancelled by production here.
            stderr.cancel()
            await asyncio.gather(stderr, return_exceptions=True)

    failure = asyncio.run(scenario())
    assert isinstance(failure, ConsumerCancelledError), (
        f"a {outcome} consumer must be classified, got {failure!r}"
    )
    assert failure.stream == "stdout", (
        f"the failure must name the settled stream, got {failure.stream!r}"
    )


def test_a_clean_consumer_does_not_hide_a_later_unrecorded_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean stdout does not let stderr's cancellation escape."""

    async def scenario() -> BaseException | None:
        """Settle stdout cleanly, then cancel stderr inside the wait."""
        stdout = _future()
        stderr = _future()
        stdout.set_result(_CapturedDrain(payload="complete"))
        _settle_during_wait(monkeypatch, stderr, "cancelled")
        return await _await_consumer_outcomes(
            _consumers(stdout, stderr),
            budget=0.05,
        )

    failure = asyncio.run(scenario())
    assert isinstance(failure, ConsumerCancelledError), (
        f"the unrecorded cancellation must be classified, got {failure!r}"
    )
    assert failure.stream == "stderr", (
        f"the failure must name stderr, got {failure.stream!r}"
    )


def test_already_settled_consumers_are_attributed_in_stream_order() -> None:
    """Two consumers settled before the wait are attributed stdout-first."""

    async def scenario() -> BaseException | None:
        """Both consumers fail before the wait registers its callbacks."""
        stdout = _future()
        stderr = _future()
        stdout.set_exception(CallbackError("stdout failed"))
        stderr.set_exception(CallbackError("stderr failed"))
        try:
            return await _await_consumer_outcomes(
                _consumers(stdout, stderr),
                budget=0.05,
            )
        finally:
            # stdout's failure is retrieved by the wait; stderr's is not.
            await asyncio.gather(stderr, return_exceptions=True)

    failure = asyncio.run(scenario())
    assert isinstance(failure, CallbackError), (
        f"the first stream's failure must be selected, got {failure!r}"
    )
    assert str(failure) == "stdout failed", (
        f"the documented tie-break is stdout first, got {failure!r}"
    )


def _ownership(
    consumers: tuple[asyncio.Future[_CapturedDrain], ...],
) -> _RunTaskOwnership:
    """Build the task ownership a post-exit collection reads."""
    return _RunTaskOwnership(
        stdin_task=None,
        consumers=typ.cast("_ConsumerPair", consumers),
        discard_on_cancel=asyncio.Event(),
        relay_diagnostics=(_RelayDiagnostics(), _RelayDiagnostics()),
    )


def _execution() -> _SubprocessExecution:
    """Build the execution fields the post-exit collection reads."""
    return typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            timeout=None,
            idle=None,
            capture_stdout=None,
            capture_stderr=None,
            capture_bytes=False,
            observation=None,
        ),
    )


# RUF029: the collection awaits this reconciliation seam.
async def _discard_reconcile(  # ruff: ignore[unused-async]
    *_args: object,
    **_kwargs: object,
) -> object:
    """Return empty drains without touching a real process."""
    return (_CapturedDrain(), _CapturedDrain())


@pytest.mark.parametrize("outcome", ["cancelled", "partial"])
def test_run_collection_classifies_a_settled_consumer(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """run()'s post-exit collect raises the unrecorded consumer's failure."""
    monkeypatch.setattr(
        "cuprum._subprocess_stream_run._settle_group_and_reconcile_consumers",
        _discard_reconcile,
    )
    execution = _execution()

    async def drive() -> None:
        """Run the collection to the classified failure."""
        stdout = _future()
        stderr = _future()
        _settle_during_wait(monkeypatch, stdout, outcome)
        process = typ.cast("asyncio.subprocess.Process", object())
        await _collect_stream_output(
            process,
            execution,
            _ownership((stdout, stderr)),
            4242,
        )

    with pytest.raises(ConsumerCancelledError):
        asyncio.run(asyncio.wait_for(drive(), _WATCHDOG_S))


@pytest.mark.parametrize("outcome", ["cancelled", "partial"])
def test_lines_collection_classifies_a_settled_consumer(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """lines()' post-exit collect raises the unrecorded consumer's failure."""
    monkeypatch.setattr(line_drain, "_settle_and_reconcile", _discard_reconcile)
    execution = _execution()

    async def drive() -> None:
        """Run the collection to the classified failure."""
        stdout = _future()
        stderr = _future()
        _settle_during_wait(monkeypatch, stdout, outcome)
        run = typ.cast(
            "_LineStreamRun",
            types.SimpleNamespace(tasks=_ownership((stdout, stderr))),
        )
        await line_drain._collect_line_stream_after_exit(run, 4242, execution)

    with pytest.raises(ConsumerCancelledError):
        asyncio.run(asyncio.wait_for(drive(), _WATCHDOG_S))
