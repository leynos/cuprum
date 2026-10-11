"""What a run that streams stdin owes the producer it was handed.

``StdinStream`` gives cuprum a generator it did not create but must close. A
run that abandons it leaks whatever that generator holds — a file, a socket, a
lock — for the lifetime of the process, and nothing the caller sees reports it,
because the run still returns, still exits, and still looks quick.

The observable is the generator's own ``finally``. This module drives one run
per way a streaming run can end and asserts that the ``finally`` ran, plus the
caller-visible outcome that proves the run took the path it was written for.
"Completed" and "finished quickly" are deliberately not treated as evidence:
an implementation that abandons the producer satisfies both.

Each script is chosen so only its own mechanism can end its run. The child that
never reads appears twice on purpose — once behind a failing producer and once
behind a deadline — because a child that exits on its own would let either run
finish for the wrong reason.

Exercised through ``run()`` only. The asserted ``finally`` is reached by
``run_sync()`` through the same path, and the two strategies are pinned
separately in ``test_safe_cmd_stdin_stream``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from cuprum import sh
from cuprum.sh import StdinSourceError, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.stream_pipes import drain_blocking_payload_size

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


_ECHO_STDIN_BYTES = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"

# The child sleeps far longer than any window here, so a run that has ended can
# only have ended because something other than the child ended it.
_NEVER_READS = "import time; time.sleep(30)"

# The child takes one byte and exits, so the writer soon meets a closed pipe.
_READS_ONE_BYTE = "import sys; sys.stdin.buffer.read(1)"

_FINALIZED = "finalized"

# A producer's own failure, distinct from anything the child or the pipe does.
_SOURCE_FAILURE = RuntimeError("producer exploded")

# The four ways a run that streams stdin can end, named by the outcome the
# caller sees. Each has its own reason for the writer to stop, and each owes
# the producer the same finalization, which none of them can satisfy by merely
# returning, raising a documented error, or finishing quickly.
_FINALIZATION_OUTCOMES = ("completed", "source-failure", "early-close", "timeout")

# One chunk past every pipe capacity the test suite probes for, so the child
# that reads a single byte and exits leaves the writer with more to write than
# the pipe will ever take and forces a genuine broken-pipe failure rather than
# a writer that merely parks.
_OVERSIZED_CHUNK = b"x" * drain_blocking_payload_size()


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


@dc.dataclass(frozen=True, slots=True)
class _FinalizationScenario:
    """One run outcome, the producer that brings it about, and its proof."""

    script: str
    chunks: tuple[bytes, ...]
    expected_outcome: str
    timeout: float | None = None
    raises: BaseException | None = None
    parks: bool = False
    observed_early_close: bool = False


class _FinalizationSpy:
    """A producer that records the one fact every run owes it: finalization.

    A generator's ``finally`` is the only place its ``aclose()`` is observable
    from outside cuprum, so it is the whole observable for "cuprum finalized
    the producer it owned". Recording it separately from the chunk values keeps
    the record independent of anything the child did with the bytes: the run
    can only fill this list by closing the generator.
    """

    def __init__(self) -> None:
        """Start with no finalization recorded."""
        self.finalized: list[str] = []

    async def producer(
        self, scenario: _FinalizationScenario
    ) -> cabc.AsyncIterator[bytes]:
        """Yield the scenario's chunks, then take the scenario's exit path.

        Yields
        ------
        bytes
            Each of the scenario's chunks, in order.
        """
        try:
            for chunk in scenario.chunks:
                await asyncio.sleep(0)
                yield chunk
            if scenario.raises is None:
                # Parking is for the outcomes that need the run to end *while*
                # the producer is suspended. An exhaustible producer would
                # either end the run the wrong way or leave the writer with
                # nothing to fail against.
                if scenario.parks:
                    await asyncio.Event().wait()
                return
            raise scenario.raises
        finally:
            self.finalized.append(_FINALIZED)


def _finalization_scenarios(
    python_builder: cabc.Callable[..., SafeCmd],
) -> dict[str, _FinalizationScenario]:
    """Build one scenario per outcome, keyed by the outcome's name.

    Returns
    -------
    dict[str, _FinalizationScenario]
        The scenario for each name in :data:`_FINALIZATION_OUTCOMES`.
    """
    return {
        # The child reads everything, so the producer exhausts and the run
        # succeeds: finalization here is the ordinary path, and the control
        # that keeps the three abnormal outcomes from being the only evidence.
        "completed": _FinalizationScenario(
            script=_ECHO_STDIN_BYTES,
            chunks=(b"first\n", b"second"),
            expected_outcome="completed",
        ),
        # The child never reads and outlives any deadline here, so nothing but
        # the producer's own failure can end the run.
        "source-failure": _FinalizationScenario(
            script=_NEVER_READS,
            chunks=(b"first\n",),
            expected_outcome="source-failure",
            raises=_SOURCE_FAILURE,
        ),
        # The child takes one byte and exits, so the writer's oversized chunk
        # meets a closed pipe: the run records the early close and continues to
        # the child's exit code. The chunk is oversized rather than merely
        # non-empty so the failure is the pipe's, which is the classification
        # this scenario exists to reach.
        "early-close": _FinalizationScenario(
            script=_READS_ONE_BYTE,
            chunks=(_OVERSIZED_CHUNK,),
            expected_outcome="completed",
            parks=True,
            observed_early_close=True,
        ),
        # The child outlives the deadline and the producer will never yield
        # again, so only the deadline can end the run.
        "timeout": _FinalizationScenario(
            script=_NEVER_READS,
            chunks=(b"first\n",),
            expected_outcome="timeout",
            timeout=0.3,
            parks=True,
        ),
    }


def _early_close_events(events: cabc.Iterable[ExecEvent]) -> list[ExecEvent]:
    """Return the ``early_close`` stdin-error observations in *events*."""
    return [
        event
        for event in events
        if event.phase == "stdin_error" and event.operation == "early_close"
    ]


@dc.dataclass(frozen=True, slots=True)
class _Observation:
    """One scenario's caller-visible outcome and its finalization record."""

    outcome: str
    finalized: tuple[str, ...]


async def _run_scenario(
    command: SafeCmd,
    spy: _FinalizationSpy,
    scenario: _FinalizationScenario,
) -> _Observation:
    """Run one scenario and read its records while the loop is still running.

    The record is read *inside* the loop on purpose. An abandoned async
    generator is not leaked past the loop: CPython's ``shutdown_asyncgens``
    closes whatever is still open when the loop is torn down, so a record read
    after ``asyncio.run`` returns would show a closed producer even for a run
    that never closed it. Reading here is what makes the record evidence about
    cuprum rather than evidence about the loop.
    """
    try:
        result = await command.run(
            stdin=StdinStream(spy.producer(scenario)),
            timeout=scenario.timeout,
        )
    except StdinSourceError:
        outcome = "source-failure"
    except TimeoutError:
        outcome = "timeout"
    else:
        outcome = "completed" if result.exit_code == 0 else f"exit {result.exit_code}"
    return _Observation(outcome=outcome, finalized=tuple(spy.finalized))


@pytest.mark.parametrize("outcome", _FINALIZATION_OUTCOMES)
def test_every_run_outcome_finalizes_the_producer(
    outcome: str,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Normal completion, source failure, early close, and timeout all finalize.

    The assertion is the finalization signal itself. "The run returned" and
    "the run was quick" are both satisfied by an implementation that abandons
    the producer, which is the leak this pins: a producer holding a file,
    socket, or lock keeps it for the lifetime of the process if its generator
    is never closed.

    The four cases do not carry equal weight, and the difference was measured
    rather than assumed. An exhausted producer terminates itself, so its
    ``finally`` runs as it finishes; a failing producer ends by raising, which
    also closes it. Both were observed to still record their finalization with
    cuprum's own ``aclose`` call deleted entirely, so they pin the mechanism
    but cannot detect a run that fails to close a producer it abandoned. Only
    the early-close case discriminates: its producer is suspended in a pull
    that will never resume, and it was observed to record nothing once that
    call was removed. The timeout case is not merely a second copy of it —
    the timeout tears the writer down as a task, a different route to the same
    obligation.

    Each scenario also asserts the path it was written for, because a
    finalization record on its own says nothing about which outcome produced
    it. The caller-visible outcome is checked for all four, and the early-close
    case additionally pins the ``early_close`` observation, so a writer that
    parked and was cancelled instead of meeting a closed pipe cannot pass as
    the early-close path.
    """
    scenario = _finalization_scenarios(python_builder)[outcome]
    spy = _FinalizationSpy()
    command = python_builder("-c", scenario.script)
    events: list[ExecEvent] = []

    with sh.observe(events.append):
        observation = asyncio.run(_run_scenario(command, spy, scenario))

    assert observation.outcome == scenario.expected_outcome, (
        f"the {outcome} scenario must reach its own outcome, or the "
        f"finalization assertion would be about a different path; "
        f"got {observation.outcome!r}"
    )
    if scenario.observed_early_close:
        assert _early_close_events(events), (
            "this scenario exists to reach the early-close classification, so "
            "the run must record it; without this the case would pass for a "
            "writer that merely parked and was cancelled"
        )
    assert observation.finalized == (_FINALIZED,), (
        f"the {outcome} outcome must finalize the producer cuprum owns; "
        f"got {observation.finalized!r}"
    )
