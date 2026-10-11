"""Shared scaffolding for the stdio failure-classification cases.

``cuprum.unittests.test_stdio_error_classification_runs`` drives every
standard-stream failure boundary through a real ``SafeCmd.run`` and asserts
what the run reported. The machinery that makes those runs observable is not
itself the subject of any case, so it lives here: the outcome record the cases
read, the doubles that provoke a boundary or record a metric, and the single
assertion that checks the three channels agree.

Keeping it separate is what lets the case module stay a readable list of
boundaries rather than a list interleaved with plumbing. Nothing here asserts
anything about cuprum's behaviour; a disagreement between the channels is
reported by the case that called it, with that case's own context.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
import uuid

from cuprum import sh
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import StdioTarget
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, StdioFailureCategory
    from cuprum.sh import (
        ExecutionContext,
        RunOutputOptions,
        SafeCmd,
        StdinStream,
    )

# Reads its stdin to EOF, so a producer that finishes cleanly leaves a run that
# can succeed. Every encoding case fails for a reason other than the child, and
# this is what keeps that legible: the child is not the variable.
DRAINS_STDIN = "import sys; sys.stdin.buffer.read()"

# Never reads, so the parent's write cannot fail on its own. Any exception the
# writer sees on this child came from the producer.
NEVER_READS = "import time; time.sleep(30)"

# The run deadline for every case here. Long enough that a loaded host cannot
# fail one, short enough that a regression fails the suite rather than wedging
# it.
DEADLINE_S = 15.0

# An over-long DNS label. ``idna`` holds a whole label until it is flushed and
# applies its length rule then, so this is what makes the final-encode call
# site the thing that fails, rather than the per-chunk one.
OVERLONG_IDNA_LABEL = "x" * 64

# The counter a boundary increment is reported on, named here rather than
# repeated at each assertion so a rename fails in one place.
STDIO_ERRORS = "cuprum_stdio_errors_total"


@dc.dataclass(frozen=True, slots=True)
class ExpectedClassification:
    """What a boundary is expected to have reported.

    The five fields travel together because they are one claim: a case names a
    boundary and the shape of its report in a single expression. Grouping them
    keeps each call site a description of *which* boundary and *what* it owes,
    rather than a run of positional-looking keywords whose order a reader has to
    hold in mind.
    """

    category: StdioFailureCategory
    operation: str
    error_type: str
    pid_present: bool
    raised: type[BaseException]


@dc.dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one run reported, read from every channel at once.

    The channels are recorded together because the diagnosis promised all of
    them and they are different projections of one payload: the observe event,
    the metric call, and the exception the caller received.
    """

    events: tuple[ExecEvent, ...]
    counters: tuple[tuple[str, float, dict[str, str]], ...]
    failure: BaseException | None

    def stdio_events(self) -> tuple[ExecEvent, ...]:
        """Return only the ``stdio_error`` events this run emitted."""
        return tuple(e for e in self.events if e.phase == "stdio_error")

    def stdio_counters(self) -> tuple[tuple[str, float, dict[str, str]], ...]:
        """Return only the counter calls for the standard-stream diagnostic."""
        return tuple(c for c in self.counters if c[0] == STDIO_ERRORS)


class RecordingMetrics:
    """Record every counter and histogram the metrics hook projects."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a counter increment and its labels."""
        self.calls.append((name, value, dict(labels)))

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram observation; no case here expects one."""


class UnstartableProducer:
    """A producer whose own ``__aiter__`` raises before yielding anything."""

    def __init__(self, exc: BaseException) -> None:
        """Raise *exc* from this producer's ``__aiter__``."""
        self._exc = exc

    def __aiter__(self) -> cabc.AsyncIterator[bytes]:
        """Raise the recorded exception, as a failing producer would."""
        raise self._exc


class FailingFlush:
    """A borrowed file object whose ``flush`` raises, immediately pre-fork.

    Deliberately not a *full* file object, because nothing production-side asks
    it to be one: the borrowed-flush guard reaches this only through
    ``flush()``, and the binding resolves the descriptor through ``fileno()``.
    The two methods below are the whole surface that is ever touched, and
    ``borrowed_flush_target`` states that substitution once so the annotation
    at the call site does not have to be widened to accommodate a double.
    """

    def flush(self) -> None:
        """Raise the failure this object exists to inject."""
        raise OSError

    def fileno(self) -> int:
        """Report a real descriptor, so the binding resolves to this object."""
        return 1


def borrowed_flush_target() -> StdioTarget:
    """Bind a borrowed standard stream to the failing flush double.

    The cast is the honest description of what is happening: the double
    implements the part of the file protocol cuprum uses and not the rest, so
    it is passed as the borrowed object it stands in for. Confining that to
    one function keeps every case's call site free of casts, and keeps the
    claim in one place if the double is ever completed.

    Returns
    -------
    StdioTarget
        An ``fd`` target whose borrowed object raises from ``flush``.
    """
    return StdioTarget.fd(typ.cast("typ.IO[str]", FailingFlush()))


async def chunks(*values: str | bytes) -> cabc.AsyncIterator[str | bytes]:
    """Yield *values* as an async producer, suspending between chunks.

    Yields
    ------
    str | bytes
        Each supplied value, in order, after yielding control to the loop.
    """
    for value in values:
        await asyncio.sleep(0)
        yield value


async def mistyped_chunk() -> cabc.AsyncIterator[str | bytes]:
    """Yield one value that is neither ``str`` nor ``bytes``.

    The annotation states what a producer *promises*; the value is what this
    one actually yields. cuprum must refuse that disagreement at runtime, and
    the cast below is deliberately the lie the case exists to test — it is what
    makes the mistyped chunk reach the writer as a real value rather than as a
    type error at the call site.

    Yields
    ------
    str | bytes
        A value that is neither, so the writer's own type check is what has to
        catch it.
    """
    await asyncio.sleep(0)
    yield typ.cast("str", 12345)


def run_capturing(
    *,
    code: str = DRAINS_STDIN,
    context: ExecutionContext | None = None,
    output: RunOutputOptions | None = None,
    stdin: StdinStream | None = None,
) -> RunOutcome:
    """Run a command once, recording its events, metrics, and any failure.

    The failure is recorded rather than propagated so a caller can assert
    against a run that raised. That is the query side of these cases: a
    ``pytest.raises`` block would put the assertions where a failure of the
    diagnosis itself skips them, and the point is to inspect a run that failed
    rather than merely to prove one did.

    ``code`` is passed to the interpreter with ``-c``, which is not optional
    detail. Without the flag the interpreter reads it as a *script path* and
    exits 2 with "can't open file", so every case would observe a child that
    failed before reaching any stdin code at all — and several of these cases
    would still pass, because the boundary they assert is reached in the
    parent and does not care what the child did. That is what makes this worth
    a comment rather than a bare flag: a wrong argv here is invisible to most
    of the assertions it feeds.

    Returns
    -------
    RunOutcome
        The events, the metric calls, and whatever the run raised.
    """
    events: list[ExecEvent] = []
    metrics = RecordingMetrics()
    catalogue, program = python_catalogue()
    command: cabc.Callable[..., SafeCmd] = sh.make(program, catalogue=catalogue)
    raised: BaseException | None = None
    try:
        with (
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(events.append),
            sh.observe(MetricsHook(metrics)),
        ):
            command("-c", code).run_sync(
                stdin=stdin,
                output=output,
                context=context,
                timeout=DEADLINE_S,
            )
    except BaseException as exc:  # ruff: ignore[blind-except] - the failure is this call's subject, not an error to handle.
        raised = exc
    return RunOutcome(tuple(events), tuple(metrics.calls), raised)


def _assert_event_names_the_boundary(
    event: ExecEvent,
    expected: ExpectedClassification,
) -> None:
    """Assert the diagnostic names the boundary the case expected."""
    assert event.error_category is expected.category, (
        f"the boundary must be named {expected.category!r}; "
        f"found {event.error_category!r}"
    )
    assert event.operation == expected.operation, (
        f"the boundary's operation must be {expected.operation!r}; "
        f"found {event.operation!r}"
    )
    assert event.error_type == expected.error_type, (
        f"the boundary must report {expected.error_type!r}; found {event.error_type!r}"
    )
    assert (event.pid is not None) is expected.pid_present, (
        "pid must be carried exactly when a child exists; "
        f"expected present={expected.pid_present}, found {event.pid!r}"
    )


def _assert_event_is_correlated(
    event: ExecEvent,
    outcome: RunOutcome,
    execution_id: uuid.UUID | None,
) -> None:
    """Assert the diagnostic carries the execution's own token.

    The correlation is checked against the run's *other* events rather than by
    asserting the token is a UUID. A UUID check alone is satisfied by a token
    the diagnostic minted for itself, which is precisely the defect this is
    meant to catch; every other event in the run carries the token the
    execution was started under, so a diagnostic that agrees with them is
    attributable and one that disagrees is not.

    ``execution_id`` pins the token to a specific execution the caller already
    observed, for cases that can name one. It is not required, because a
    pre-spawn boundary starts no child and the caller may have no token in
    hand — the sibling check still applies and is the one that matters.
    """
    assert isinstance(event.exec_id, uuid.UUID), (
        f"the correlation token must be a minted UUID; found {event.exec_id!r}"
    )
    run_tokens = {e.exec_id for e in outcome.events if e is not event}
    assert run_tokens, (
        "the run recorded no other event to correlate the diagnostic against, "
        f"so the check would be vacuous; found phases "
        f"{[e.phase for e in outcome.events]}"
    )
    assert run_tokens == {event.exec_id}, (
        "the diagnostic must carry the token the run was started under, not "
        "one of its own; the run's other events carry "
        f"{run_tokens} and the diagnostic carried {event.exec_id!r}"
    )
    if execution_id is not None:
        assert event.exec_id == execution_id, (
            "the diagnostic must carry the token minted for this run; "
            f"the run was started under {execution_id} but the diagnostic "
            f"carried {event.exec_id!r}"
        )


def _assert_metric_reports_the_boundary(
    outcome: RunOutcome,
    expected: ExpectedClassification,
) -> None:
    """Assert the counter agrees with the event, once, on a bounded label set."""
    counters = outcome.stdio_counters()
    assert len(counters) == 1, (
        "exactly one metric increment must be recorded for one failure; "
        f"found {counters}"
    )
    name, value, labels = counters[0]
    assert name == STDIO_ERRORS, (
        f"the increment must be reported on {STDIO_ERRORS!r}; found {name!r}"
    )
    assert value == 1.0, (  # ruff: ignore[float-equality-comparison] - a count is exact.
        f"one failure counts as one; found {value}"
    )
    assert labels.get("error_category") == str(expected.category), (
        "the metric must be labelled with the same boundary as the event; "
        f"expected {str(expected.category)!r} and found "
        f"{labels.get('error_category')!r} in {labels}"
    )
    assert set(labels) == {"program", "project", "error_category"}, (
        "the label set is bounded to the program, the project, and the "
        "boundary, so no unbounded value can become a metric series; found "
        f"{sorted(labels)}"
    )


def assert_classified_once(
    outcome: RunOutcome,
    expected: ExpectedClassification,
    *,
    execution_id: uuid.UUID | None = None,
) -> None:
    """Assert the boundary was reported once, on every channel, coherently.

    ``expected`` carries the boundary and the shape of its report; see
    ``ExpectedClassification`` for why those five fields are one value rather
    than five keyword arguments.

    The claims are checked together because they are one claim seen from
    several sides. A category on the event with no matching counter means the
    operator's dashboard and their event stream disagree; a counter without the
    event means the same in reverse; and either with a correlation token that
    does not belong to the execution makes the record unattributable.

    Each side is asserted by its own helper, so a failure names the channel it
    came from before it names the disagreement.
    """
    events = outcome.stdio_events()
    assert len(events) == 1, (
        f"a failing boundary must report exactly once; found {len(events)} "
        f"stdio_error events among {[e.phase for e in outcome.events]}"
    )
    event = events[0]

    _assert_event_names_the_boundary(event, expected)
    _assert_event_is_correlated(event, outcome, execution_id)
    _assert_metric_reports_the_boundary(outcome, expected)

    assert outcome.failure is not None, (
        "a boundary that failed must reach the caller, not be swallowed"
    )
    assert isinstance(outcome.failure, expected.raised), (
        f"the caller must receive {expected.raised.__name__}; found {outcome.failure!r}"
    )
