"""Post-exit scopes spend the original deadline's remaining budget."""

from __future__ import annotations

import asyncio
import types
import typing as typ

import pytest

from cuprum._line_stream import drain
from cuprum._subprocess_direct_run import _await_writer_within_deadline
from cuprum.unittests._stream_drain_support import captured

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution


def _execution(clock: cabc.Callable[[], float]) -> _SubprocessExecution:
    """Build only the execution fields the post-exit scopes read."""
    return typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            timeout=10.0,
            started_at=100.0,
            observation=types.SimpleNamespace(monotonic_clock=clock),
            idle=None,
        ),
    )


def test_direct_writer_scope_uses_only_the_unspent_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer gets 0.25 seconds when the exit already spent 9.75 of ten."""
    delays: list[float | None] = []
    real_timeout = asyncio.timeout

    def record_timeout(delay: float | None) -> asyncio.Timeout:
        """Record the scope budget while allowing its already-finished task."""
        delays.append(delay)
        return real_timeout(None)

    monkeypatch.setattr(asyncio, "timeout", record_timeout)

    async def drive() -> None:
        """Await a writer under the controlled execution clock."""
        writer = asyncio.create_task(asyncio.sleep(0))
        await _await_writer_within_deadline(_execution(lambda: 109.75), writer)

    asyncio.run(drive())
    assert delays == [0.25], f"the direct writer restarted its timeout: {delays!r}"


def test_lines_scope_and_consumer_wait_share_the_unspent_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line-stream scope and later reader wait debit the same start time."""
    delays: list[float | None] = []
    real_timeout = asyncio.timeout
    readings = iter([109.5, 109.75])

    def record_timeout(delay: float | None) -> asyncio.Timeout:
        """Record the outer remainder without introducing real elapsed time."""
        delays.append(delay)
        return real_timeout(None)

    async def settled(*_args: object) -> None:
        """Model already-settled work preceding the reader wait."""

    async def readers(_consumers: object, remaining: float | None) -> None:  # ruff: ignore[unused-async]
        """Record the later reader budget."""
        delays.append(remaining)

    async def drains(*_args: object) -> tuple[object, object]:  # ruff: ignore[unused-async]
        """Return the already-collected outputs."""
        return captured(None), captured(None)

    monkeypatch.setattr(asyncio, "timeout", record_timeout)
    monkeypatch.setattr(drain, "_stop_idle_monitor", settled)
    monkeypatch.setattr(drain, "_await_writer_within_scope", settled)
    monkeypatch.setattr(drain, "_await_consumers", readers)
    monkeypatch.setattr(drain, "_read_consumed_drains", drains)
    run = typ.cast(
        "_LineStreamRun",
        types.SimpleNamespace(
            tasks=types.SimpleNamespace(consumers=(None, None)),
        ),
    )
    asyncio.run(
        drain._collect_line_stream_after_exit(
            run,
            123,
            _execution(lambda: next(readings)),
        )
    )
    assert delays == [0.5, 0.25], (
        f"the line-stream drain restarted its timeout: {delays!r}"
    )


def test_direct_writer_timeout_error_preserves_the_producer_exception() -> None:
    """A producer's TimeoutError must not become command expiry or telemetry."""
    sentinel = TimeoutError("producer failed")

    async def fail() -> None:  # ruff: ignore[unused-async]
        """Raise the producer's own sentinel."""
        raise sentinel

    async def drive() -> None:
        """Drive the direct post-exit helper with the failed writer."""
        writer = asyncio.create_task(fail())
        with pytest.raises(TimeoutError) as caught:
            await _await_writer_within_deadline(_execution(lambda: 100.0), writer)
        assert caught.value is sentinel, (
            "the producer's exception identity must survive"
        )

    asyncio.run(drive())
