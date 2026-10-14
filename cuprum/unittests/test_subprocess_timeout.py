"""Unit tests for the subprocess timeout payload and the drain-once helper.

Covers the drain-once helper and the synchronous timeout handler. The wait and
teardown semantics these feed live in ``test_subprocess_wait_teardown``, and
the telemetry these paths emit is covered by ``test_subprocess_timeout_logging``
and ``test_subprocess_timeout_observe``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._subprocess_timeout import (
    _handle_stream_timeout,
    _SubprocessInvariantError,
    _SubprocessTimeoutError,
)
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
)
from cuprum.unittests._stream_drain_support import _NONE_CAPTURED, captured

# A consumer either completes with captured text or fails with an exception.
_CONSUMER_OUTCOME = st.one_of(
    st.text(max_size=16).map(lambda value: ("value", value)),
    st.just(("raise", None)),
)


@dc.dataclass(frozen=True, slots=True)
class _DrainCase:
    """Generated scheduling and outcome configuration for a consumer drain."""

    stdout: tuple[str, str | None]
    stdout_delay: int
    stderr: tuple[str, str | None]
    stderr_delay: int


_DRAIN_CASE = st.builds(
    _DrainCase,
    stdout=_CONSUMER_OUTCOME,
    stdout_delay=st.integers(min_value=0, max_value=3),
    stderr=_CONSUMER_OUTCOME,
    stderr_delay=st.integers(min_value=0, max_value=3),
)


async def _consumer(outcome: tuple[str, str | None], delay: int) -> _CapturedDrain:
    """Yield ``delay`` times, then either report text or fail."""
    for _ in range(delay):
        await asyncio.sleep(0)
    kind, value = outcome
    if kind == "raise":
        msg = "consumer failed"
        raise ValueError(msg)
    return captured(value)


def test_drain_stream_consumers_cancels_pending_and_decodes() -> None:
    """Draining cancels a still-pending consumer and preserves a completed one."""

    async def block() -> _CapturedDrain:
        """Remain pending until the drain cancels this task."""
        await asyncio.Event().wait()
        return _CapturedDrain()

    async def run_case() -> None:
        """Drain a still-pending consumer alongside an already-completed one."""
        pending = asyncio.create_task(block())
        completed = asyncio.create_task(asyncio.sleep(0, result=captured("stderr")))
        await completed  # the completed consumer must be genuinely done

        stdout_text, stderr_text = await _drain_stream_consumers(
            (pending, completed), _DrainContext(captures=_NONE_CAPTURED)
        )

        assert pending.cancelled(), (
            "a consumer left pending at drain must be cancelled, "
            f"but pending.done()={pending.done()}"
        )
        assert stdout_text.payload is None, (
            f"a cancelled consumer must decode to None, not {stdout_text!r}"
        )
        assert stderr_text.payload == "stderr", (
            f"a completed consumer's text must be preserved, got {stderr_text!r}"
        )

    asyncio.run(run_case())


# Number of event-loop turns the harness lets consumers settle before draining.
# ``_consumer`` needs ``delay + 1`` turns to reach its outcome, and each consumer
# is scheduled exactly once per turn, so a consumer whose ``delay`` is at least
# ``_SETTLE_TURNS`` is still pending when the drain runs (and must be cancelled
# and decoded to ``None``); one with a smaller delay has completed and keeps its
# outcome.
_SETTLE_TURNS = 2


@settings(max_examples=75, deadline=None, derandomize=True)
@given(case=_DRAIN_CASE)
def test_drain_stream_consumers_decodes_across_orderings(case: _DrainCase) -> None:
    """_drain_stream_consumers drains once, decoding completed and pending readers.

    Consumers settle for a fixed number of event-loop turns before the drain, so
    those whose delay fits the window complete (their text preserved, a failure
    mapping to ``None``) while those whose delay exceeds it are still pending at
    drain time and must be cancelled and decoded to ``None``.
    """

    async def run_case() -> None:
        """Drive the drain once and assert its decoding invariants."""
        consumers = (
            asyncio.create_task(_consumer(case.stdout, case.stdout_delay)),
            asyncio.create_task(_consumer(case.stderr, case.stderr_delay)),
        )
        # Let consumers settle for a bounded number of turns rather than awaiting
        # every one to completion, so readers slower than the window remain
        # pending when the drain runs and exercise its cancellation path.
        for _ in range(_SETTLE_TURNS):
            await asyncio.sleep(0)

        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers, _DrainContext(captures=_NONE_CAPTURED)
        )

        assert all(task.done() for task in consumers), (
            "every consumer task must be drained before the helper returns"
        )
        for label, task, payload, outcome, delay in (
            (
                "stdout",
                consumers[0],
                stdout_text.payload,
                case.stdout,
                case.stdout_delay,
            ),
            (
                "stderr",
                consumers[1],
                stderr_text.payload,
                case.stderr,
                case.stderr_delay,
            ),
        ):
            kind, value = outcome
            if delay >= _SETTLE_TURNS:
                assert task.cancelled(), (
                    f"{label} consumer with delay {delay} must still be pending at "
                    f"drain and be cancelled, but cancelled()={task.cancelled()}"
                )
                expected = None
            else:
                assert not task.cancelled(), (
                    f"{label} consumer with delay {delay} must complete within the "
                    "settling window rather than being cancelled"
                )
                expected = None if kind == "raise" else value
            assert payload == expected, (
                f"{label} capture must reflect its consumer outcome: "
                f"expected {expected!r}, got {payload!r}"
            )

    asyncio.run(run_case())


def test_handle_stream_timeout_preserves_timeout_and_output() -> None:
    """The handler raises with the configured timeout and pre-drained output."""
    with pytest.raises(_SubprocessTimeoutError) as exc_info:
        _handle_stream_timeout(
            TimeoutError(),
            stdout_text="captured-out",
            stderr_text=None,
            timeout=1.5,
        )

    assert exc_info.value.timeout == pytest.approx(1.5), (
        f"the configured timeout must survive unchanged: got {exc_info.value.timeout}"
    )
    assert exc_info.value.stdout == "captured-out", (
        "pre-drained stdout must be preserved on the timeout error, "
        f"got {exc_info.value.stdout!r}"
    )
    assert exc_info.value.stderr is None, (
        f"pre-drained stderr must be preserved, got {exc_info.value.stderr!r}"
    )


def test_handle_stream_timeout_requires_configured_timeout() -> None:
    """A timeout handler reached without a configured timeout fails loudly."""
    with pytest.raises(_SubprocessInvariantError):
        _handle_stream_timeout(
            TimeoutError(),
            stdout_text=None,
            stderr_text=None,
            timeout=None,
        )
