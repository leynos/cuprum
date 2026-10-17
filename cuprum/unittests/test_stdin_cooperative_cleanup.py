"""A cancelled run must *settle* cooperative cleanup, not cancel it part-way.

``cuprum.unittests.test_stdin_finalizer_bound`` pins the budget that makes this
possible as arithmetic: a writer's reclaim window is one pipe-release step plus
the finalization's two. Arithmetic is not the property a caller depends on,
though, and this module measures the one that is — that a producer which
releases promptly is allowed to *finish* releasing when the run that owns it is
cancelled, rather than being cut off at the first suspension.

The producer here is cooperative in the sense the budget was derived for: it
parks, and when it is finalized it releases over a bounded interval that fits
inside the window it is given. Cleanup that ignores cancellation is the
documented cooperation limit — cuprum bounds its own wait and says so — and is
measured by the bound module instead.

Two readings were tried against a collapsed budget, and only one of them is
worth asserting. The obvious one, a census of pending tasks when the cancelled
run returns, does not distinguish the budgets at all: a later reconcile path
sweeps the writer up under both, so the census reads empty either way and a
test built on it would be vacuous. The reading that does distinguish them is
whether the producer's release completed. Measured with the budget collapsed
back to a single window:

    budget 0.75  entered=True completed=True  cancelled=False
    budget 0.25  entered=True completed=False cancelled=True

The reason the small budget cannot cover it is structural rather than a matter
of margin. Release (close) costs a full window on its own, and it is the grand-
child holding the child's stdin read end that makes it do so: the pipe cannot
be closed while that descriptor lives, so the close waits its window out and
the collapsed budget is exhausted before the producer's cleanup is reached.

The model is a class rather than an async generator so that ``aclose`` is
plainly the producer's own hook, reachable exactly as the writer reaches it,
with no dependency on how the generator protocol's throw machinery behaves.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, RunOutputOptions, StdinStream
from cuprum.unittests._stdin_run_cancellation_support import (
    _CANCEL_GRACE_S,
    _FINALIZED,
    _READINESS_S,
    _RUN_DEADLINE_S,
    _UNWIND_S,
    _assert_teardown_is_complete,
    _CancelledRunEvidence,
    _install_post_exit_probe,
    _reclaim_run,
    _record_aftermath,
    _settlement_outcome,
)
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.stream_pipes import drain_blocking_payload_size

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd
    from cuprum.unittests._stdin_run_cancellation_support import _CancellationProbe

# How long the producer's release takes, handwritten rather than derived from
# the production constants: deriving it would restate the budget instead of
# measuring it. It has to satisfy two conditions, and both are the point.
#
# It must fit inside the finalization window, which is what makes the producer
# cooperative — a release longer than the window is one cuprum would be right to
# cancel, and the bound module already covers that case. It must also be long
# enough that a writer holding only the collapsed budget cannot reach it, which
# the close window's cost guarantees rather than this figure.
_FINALIZER_RELEASE_S = 0.10

# The child hands its own stdin read end to a grandchild and exits at once
# without reading a byte. Two things follow, and the test needs both. The writer
# has no failure to settle on, so the settle window is genuinely occupied when
# the cancellation lands — the same stance, and the same reason, as
# ``cuprum.unittests.test_stdin_settle_cancellation``. And the close cannot
# complete until its window expires, because the read end is still open, which
# is what stops the collapsed budget from reaching the producer's cleanup.
_GRANDCHILD_LIFETIME_S = 120
_EXITS_AND_HOLDS_STDIN = (
    "import subprocess, sys\n"
    "subprocess.Popen([\n"
    "    sys.executable,\n"
    "    '-c',\n"
    f"    'import time; time.sleep({_GRANDCHILD_LIFETIME_S})',\n"
    "])\n"
)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


class _CooperativeProducer:
    """A producer that parks, then releases when finalized, and records how.

    The record distinguishes *finishing* the release from being interrupted
    inside it, because that distinction is the whole subject: the two budgets
    differ in which of them happens, and not in what the loop looks like
    afterwards.
    """

    def __init__(self, probe: _CancellationProbe) -> None:
        """Record where the stance is signalled and where cleanup is recorded."""
        self.probe = probe
        self.yielded = False
        self.release_started = False
        self.release_completed = False

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Return this object's own iterator, so ``aclose`` is found on it."""
        return self

    async def __anext__(self) -> bytes:
        """Yield one oversized chunk, then park with nothing left to pull.

        The chunk is past the pipe buffer and a grandchild holds the read end,
        so the writer blocks in ``drain()`` and never comes back for a second
        pull: the producer stays suspended, and the writer task is genuinely
        parked when the settle window opens.

        Returns
        -------
        bytes
            One chunk, past the pipe buffer so the writer cannot drain it.
        """
        if self.yielded:
            self.probe.docked.set()
            await asyncio.Event().wait()
        self.yielded = True
        return b"x" * drain_blocking_payload_size()

    async def aclose(self) -> None:
        """Release over a bounded interval, recording whether it finished.

        ``release_completed`` is set only on the path that ran the whole
        interval, so it is the reading that separates being waited for from
        being cancelled at the first suspension. ``release_started`` is what
        keeps that honest: a run that never reached this hook would leave the
        flag clear too, and the two must not be confusable.
        """
        self.release_started = True
        await asyncio.sleep(_FINALIZER_RELEASE_S)
        self.release_completed = True
        self.probe.finalized.append(_FINALIZED)


async def _cancel_mid_settle_and_read(
    command: SafeCmd,
    probe: _CancellationProbe,
    producer: _CooperativeProducer,
    *,
    capture: bool,
) -> tuple[str, _CancelledRunEvidence]:
    """Cancel a run inside its settle, then read what it left behind.

    The reading is taken here, inside the loop, rather than after
    ``asyncio.run`` has returned: the loop is what tears down whatever is still
    pending, so a stranded task would be collected at loop close and leave no
    trace to assert against.

    ``capture`` selects the orchestration path this cancels into. With capture
    on the child's pipes are read back by consumers; with it off there are no
    readers and the writer drains into an unread pipe. The two reach the
    writer's teardown by different routes, so a reclaim window that covers one
    need not cover the other.

    Parameters
    ----------
    command : SafeCmd
        The command whose run is cancelled.
    probe : _CancellationProbe
        The post-exit probe the run reports into.
    producer : _CooperativeProducer
        The stdin producer whose release must complete.
    capture : bool
        Whether the run captures output through parent-side pipes.

    Returns
    -------
    tuple[str, _CancelledRunEvidence]
        The run's settlement outcome, and the aftermath recorded while the loop
        that ran it is still alive.
    """
    task: asyncio.Task[CommandResult] = asyncio.create_task(
        command.run(
            stdin=StdinStream(producer),
            output=RunOutputOptions(capture=capture),
            context=ExecutionContext(cancel_grace=_CANCEL_GRACE_S),
            timeout=_RUN_DEADLINE_S,
        )
    )
    try:
        await asyncio.wait_for(probe.settling.wait(), timeout=_READINESS_S)
        assert not task.done(), (
            "the run ended before its settle began, so this case would not "
            "have cancelled a live writer at all"
        )
        # Positive control for the census below. A writer that had already
        # finished would leave nothing for the cancellation to strand, so a
        # clean aftermath would say nothing about the reclaim.
        assert probe.writer_pending_at_settle, (
            "the stdin writer had already finished when the settle opened, so "
            "the window this cancels into was never occupied"
        )
        task.cancel()
        # Queried rather than awaited, so the outcome is recorded as a
        # diagnosis instead of being propagated — a catch wide enough to record
        # "ended some other way" is also wide enough to swallow the very
        # cancellation the assertion is about.
        await asyncio.wait({task}, timeout=_UNWIND_S)
        return _settlement_outcome(task), _record_aftermath(probe)
    finally:
        await _reclaim_run(task)


@pytest.mark.parametrize("capture", [True, False], ids=["capture", "direct"])
def test_a_cooperative_finalizer_is_settled_before_the_run_returns(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
    capture: bool,
) -> None:
    """A cancelled run lets a prompt release finish instead of cutting it off.

    The writer's teardown is real work rather than a formality: release the
    pipe, which costs its full window here because the child's read end is
    still held, and then finalize a producer that needs a bounded moment to let
    go. cuprum asks the writer to stop, the writer runs that teardown inside its
    own cancellation, and the reclaim window has to cover both — which is what
    deriving it from the steps it spans buys, and all that the collapsed figure
    fails to buy.

    The caller then sees a settled loop as well: the producer finalized, the
    child reaped, the writer done, the pipe closed, and no run-owned task
    pending. That reading is taken while the loop is still alive so a stranded
    task cannot be hidden by loop teardown — but it is the assertion beside it
    that carries the weight, and the two are deliberately reported separately
    because they are not equally sensitive. The census below reads the same
    under either budget; only the release's own completion record separates
    them.

    Both capture settings are exercised because they reach the teardown by
    different routes: with capture on the child's pipes have readers, and with
    it off the writer drains into an unread pipe and meets a different failure.
    A window sized for one is not evidence for the other.
    """
    command = python_builder("-c", _EXITS_AND_HOLDS_STDIN)
    probe = _install_post_exit_probe(monkeypatch)
    producer = _CooperativeProducer(probe)

    outcome, evidence = asyncio.run(
        _cancel_mid_settle_and_read(command, probe, producer, capture=capture)
    )

    assert outcome == "cancelled", (
        "cancelling a run that is settling its writer must propagate "
        "CancelledError unwrapped and promptly; got " + outcome
    )
    assert producer.release_started, (
        "the writer never reached the producer's cleanup, so this run settles "
        "nothing about finalization and the readings below would be vacuous"
    )
    assert producer.release_completed, (
        "the producer's release was entered but cut off before it finished, so "
        "the writer's reclaim window does not cover the cleanup a cancelled "
        "run owes its caller; a cooperative release must be waited out"
    )
    _assert_teardown_is_complete(evidence)
