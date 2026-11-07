"""A reclaim interrupted by its caller must still finish before it unwinds.

A run whose startup failed is already unwinding when
:func:`~cuprum._subprocess_startup_abandon._abandon_started_process` runs, and
its caller may be cancelled while it does. The whole sequence therefore runs
behind one ``_shielded_cleanup``: a cancellation is held until the last step has
run and only then propagates. Without that, a cancellation arriving after the
child was stopped would leave its pipes, tasks, and spill files to an event loop
about to be torn down.

The gate sink below holds the sequence's final step open, which is what puts the
cancellations inside it. Without that hold the reclaim usually finishes before
the first cancellation is delivered, and a case built that way would pass
without exercising anything.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._subprocess_startup_abandon import _abandon_started_process
from cuprum.capture_policy import FileBackedCapture
from cuprum.unittests._startup_abandon_support import _execution, _spawn_sleeper
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum._capture_types import CaptureFinal


#: How long the case waits for the reclaim to reach its gated final step. The
#: child only has to be stopped and reaped, but this host is shared.
_HOLD_WAIT_S = 10.0


class _HeldSink:
    """A capture sink that blocks in ``cleanup`` until the case releases it."""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        """Start held: nothing is cleaned until *release* is set."""
        self._entered = entered
        self._release = release
        self.is_cleaned = False

    async def absorb(self, chunk: bytes) -> None:
        """Take one raw chunk, which this sink has no use for."""
        _ = chunk

    async def finalize(self) -> CaptureFinal:
        """Fail: the reclaim settles consumers, it never builds a result."""
        msg = "the reclaim must not finalize a capture"
        raise AssertionError(msg)

    async def cleanup(self) -> None:
        """Announce entry, wait to be released, then record the cleanup."""
        self._entered.set()
        await self._release.wait()
        self.is_cleaned = True


def test_repeated_cancellation_cannot_shorten_the_reclaim(
    tmp_path: pathlib.Path,
) -> None:
    """Three cancellations must not abandon the sequence part-way through.

    The child is stopped and reaped before the gated step is entered, so a
    sequence that unwound on the first cancellation would still have released
    the process while leaving the step it had reached unfinished. The
    cancellations are therefore repeated while that step is held, and the last
    step's completion is what the case reads: it can only be true if the shield
    kept every cancellation out of the sequence itself.
    """

    async def run_case() -> None:
        """Cancel the reclaim while its final step is held, then release it."""
        process = await _spawn_sleeper()
        entered = asyncio.Event()
        release = asyncio.Event()
        sink = _HeldSink(entered, release)
        execution = _execution(
            FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path),
        )
        reclaim = asyncio.create_task(
            _abandon_started_process(process, execution, captures=(sink,))
        )
        await asyncio.wait_for(entered.wait(), timeout=_HOLD_WAIT_S)

        for _ in range(3):
            reclaim.cancel()
            # Give the cancellation every chance to land and be acted on. If
            # the sequence were going to be cut short, it would have been.
            for _ in range(4):
                await asyncio.sleep(0)
            assert not reclaim.done(), (
                "a cancellation unwound the caller while the reclaim's final "
                "step was still running"
            )

        assert process.returncode is not None, (
            "the child must be reaped before the gated step is entered, so this "
            "case observes a sequence that is already past its first step"
        )
        assert not sink.is_cleaned, (
            "the final step must still be held when the cancellations arrive"
        )

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reclaim

        assert sink.is_cleaned, (
            "the reclaim must finish every step it started, however many "
            "cancellations arrive"
        )
        assert not pending_tasks(), (
            "a cancelled reclaim must leave no task behind for the loop to "
            f"tear down, got {pending_tasks()!r}"
        )

    asyncio.run(run_case())
