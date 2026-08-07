"""Property coverage for the stdout/stderr consumer drain.

``test_timeout_capture_contract`` pins the capture contract at hand-picked
consumer states. These generalize over the state space instead: whatever the
pair of readers was doing when teardown reached them, the drain must leave
neither pending, must not displace the error it is cleaning up after, and must
report each stream as the run's capture setting promised.

Nothing here spawns a subprocess: every case drives a deterministic double.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._subprocess_drain import _drain_stream_consumers

_EXAMPLES = 25


class _ConsumerFailureError(RuntimeError):
    """Raised by a stream-consumer double that fails during the drain."""


async def _make_consumer(kind: str, text: str) -> str | None:
    """Behave as a stream consumer of the requested ``kind``.

    ``completed`` and ``failing`` yield once before settling: a task the loop
    has not scheduled yet is indistinguishable from a blocked one, and the
    drain cancels both.
    """
    if kind == "completed":
        await asyncio.sleep(0)
        return text
    if kind == "pending":
        await asyncio.Event().wait()
        return text
    if kind == "failing":
        await asyncio.sleep(0)
        raise _ConsumerFailureError
    msg = f"unsupported consumer kind: {kind!r}"
    raise ValueError(msg)


_CONSUMER_KINDS = st.sampled_from(("completed", "pending", "failing"))


async def _drain_while_raising(
    primary: BaseException,
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
) -> None:
    """Drain ``consumers`` while ``primary`` is propagating, as cleanup does."""
    try:
        raise primary
    finally:
        await _drain_stream_consumers(consumers, capture=False)


class TestConsumerDrainProperties:
    """Invariants of ``_drain_stream_consumers`` across consumer states."""

    @settings(deadline=None, max_examples=_EXAMPLES)
    @given(
        stdout_kind=_CONSUMER_KINDS,
        stderr_kind=_CONSUMER_KINDS,
        text=st.text(max_size=16),
    )
    def test_drain_leaves_no_pending_consumer(
        self, stdout_kind: str, stderr_kind: str, text: str
    ) -> None:
        """The drain settles both consumers whatever state they were in.

        Draining owns the consumers on this path, so nothing may be left pending
        afterwards; a stranded reader would outlive the run it belonged to.
        """

        async def run_case() -> None:
            """Drain an arbitrary pair of consumers and inspect what remains."""
            consumers = (
                asyncio.create_task(_make_consumer(stdout_kind, text)),
                asyncio.create_task(_make_consumer(stderr_kind, text)),
            )
            # A task the loop has not scheduled yet is indistinguishable from a
            # blocked one, and the drain cancels both. Yield until the finishing
            # consumers have settled so "completed" means what it says.
            for _ in range(4):
                await asyncio.sleep(0)
            stdout_text, stderr_text = await _drain_stream_consumers(
                consumers, capture=False
            )

            for task, kind, value in (
                (consumers[0], stdout_kind, stdout_text),
                (consumers[1], stderr_kind, stderr_text),
            ):
                assert task.done(), f"the {kind} consumer was left pending"
                expected = text if kind == "completed" else None
                assert value == expected, (
                    f"a {kind} consumer must decode to {expected!r}, got {value!r}"
                )

        asyncio.run(run_case())

    @settings(deadline=None, max_examples=_EXAMPLES)
    @given(stdout_kind=_CONSUMER_KINDS, stderr_kind=_CONSUMER_KINDS)
    def test_drain_absorbs_failures_without_replacing_primary_error(
        self, stdout_kind: str, stderr_kind: str
    ) -> None:
        """A failing consumer cannot displace the error being cleaned up after.

        The drain runs while a timeout or cancellation is already propagating, so
        it must absorb consumer failures rather than raise its own. Draining inside
        an active ``TimeoutError`` handler asserts exactly that: the original error
        is what leaves the block.
        """

        async def run_case() -> None:
            """Drain failing consumers while a TimeoutError is propagating."""
            consumers = (
                asyncio.create_task(_make_consumer(stdout_kind, "out")),
                asyncio.create_task(_make_consumer(stderr_kind, "err")),
            )
            primary = TimeoutError("primary")
            with pytest.raises(TimeoutError) as exc_info:
                await _drain_while_raising(primary, consumers)
            assert exc_info.value is primary, (
                "the drain must not replace the propagating error, got "
                f"{exc_info.value!r}"
            )

        asyncio.run(run_case())
