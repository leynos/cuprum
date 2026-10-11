"""Cancelling a run that is streaming stdin, and what its teardown leaves.

``StdinStream`` hands a run two things it must reclaim: a producer that may be
parked in its next pull, and a writer that may be blocked in ``drain()``. A
cancellation has to reach both, and it is control flow rather than a source
failure, so it must arrive at the caller as ``CancelledError`` rather than
wrapped in a ``StdinSourceError``.

These cases cancel against a child that is *still running*, so the cancellation
lands on the run's own wait for it. The sibling module
``cuprum.unittests.test_stdin_settle_cancellation`` cancels later, once the
child has exited and the run has entered its settle window; the two are
genuinely different moments and neither is a duplicate of the other. The
harness they share — how the writer is patched, what a run's aftermath is read
as, and the assertion both scenarios make about it — lives in
``cuprum.unittests._stdin_run_cancellation_support``.

Readiness is signalled rather than slept for. Each case parks the writer
through a patched seam and waits on the fact that seam reports — the producer
docked in its next pull, or the chunk write's ``drain()`` entered — so the
cancellation lands on a writer that is genuinely mid-flight rather than at a
moment the host happened to schedule. Both stances matter: a parked producer
is suspended in the run's *pull*, a blocked ``drain()`` in its *write*, and
those are different awaits for the teardown to reach.

The cases are not equally strong, and the differences were measured rather
than assumed. Two separate single-line deletions were tried, each restored
afterwards, and each case was recorded as failing or surviving:

- Deleting cuprum's ``aclose`` of the producer failed every blocked-``drain()``
  case and survived the parked-pull ones. A producer suspended at its ``yield``
  has no await left to receive the cancellation, so its ``finally`` runs only
  because cuprum closes it; a producer suspended in its own pull unwinds as a
  consequence of the cancellation itself. The blocked-``drain()`` pair is
  therefore what pins cuprum's own cleanup rather than the cancellation's.
- Deleting the cancel-path reclaim of the writer task failed
  ``capture-on``/``parked-pull``, repeatably across three runs, and survived
  the other three. So that reclaim is load-bearing for one combination here,
  and the assertion still has to be made for all four.

The point of asserting every combination is that neither deletion alone would
have been detected by all of them. A suite that only exercised the stance that
happens to unwind by itself would have recorded a green result for both.

The descriptor census was loosened once, from equality to a non-increase, and
the reason is recorded because a loosened assertion deserves one. Equality
failed once in 42 runs, in the direction of ``12 after 14 before`` — *fewer*
descriptors, which no leak can produce: it is another case's teardown, usually
a grandchild's, completing after this case's ``before`` reading. That reading
had been taken while a previous child was still being reaped. The census
exists to catch descriptors this run *fails* to release, so the loosened
predicate is the one that measures the intended property; the strict one was
measuring the shared process's timing.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum.unittests._stdin_run_cancellation_support import (
    _CAN_COUNT_FDS,
    _FINALIZED,
    _assert_teardown_is_complete,
    _cancel_when_ready,
    _install_probe,
    _open_fd_count,
    _StreamingStance,
    drain_blocking_payload_size,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd

# The child blocks far longer than any window here, so a run that has finished
# can only have finished because it was torn down, never because the child
# exited of its own accord.
_NEVER_READS = "import time; time.sleep(30)"


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


@pytest.mark.parametrize(
    "block_drain", [False, True], ids=["parked-pull", "blocked-drain"]
)
@pytest.mark.parametrize("capture", [False, True], ids=["capture-off", "capture-on"])
def test_cancelling_a_streaming_run_tears_everything_down(
    capture: bool,
    block_drain: bool,
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation reaches both awaits and leaves nothing behind.

    The producer parks after its first chunk, so the cancellation lands either
    on the run's next pull or on the write of the chunk it already has. Either
    way the producer is finalized, the child is reaped, the writer settles, the
    pipe closes, and no task survives the run — on both the capture-enabled and
    capture-disabled paths, which reconcile their work differently.

    The descriptor count is compared only where a ``/proc`` census is
    available; every other assertion is portable and runs everywhere.
    """
    command = python_builder("-c", _NEVER_READS)
    probe = _install_probe(monkeypatch, block_drain=block_drain)
    open_fds_before = _open_fd_count() if _CAN_COUNT_FDS else None

    async def producer() -> cabc.AsyncIterator[bytes]:
        """Yield one chunk, report the stance, then suspend indefinitely.

        Suspending after the first chunk is the point: it is what leaves the
        writer with nothing to fail against, so only the run's teardown can
        end this producer.

        Yields
        ------
        bytes
            One chunk, large enough to block the writer's ``drain()`` when the
            blocked-write stance asked for that.
        """
        try:
            await asyncio.sleep(0)
            if block_drain:
                # Oversized, so the writer's ``drain()`` cannot complete and
                # the signal below means a genuinely blocked write.
                yield b"x" * drain_blocking_payload_size()
            else:
                yield b"first\n"
            probe.docked.set()
            await asyncio.Event().wait()
        finally:
            probe.finalized.append(_FINALIZED)

    ready = probe.draining if block_drain else probe.docked
    evidence = asyncio.run(
        _cancel_when_ready(
            command,
            probe,
            _StreamingStance(producer(), ready),
            capture=capture,
        )
    )

    _assert_teardown_is_complete(evidence)
    if open_fds_before is not None:
        open_fds_after = _open_fd_count()
        # A non-increase, and the direction matters. This process is shared
        # with every other case in the module, so a *drop* is another case's
        # teardown finishing late — most often a grandchild's descriptors
        # going away once it is reaped. That is not this run's doing and not a
        # leak, and requiring equality reports it as one. Only growth is a
        # descriptor this run failed to release, which is what the census is
        # for.
        assert open_fds_after <= open_fds_before, (
            "a cancelled run must not leave descriptors open behind it; "
            f"{open_fds_before} were open before it and {open_fds_after} after"
        )
