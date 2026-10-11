"""Bounded operational diagnostics for every standard-stream failure boundary.

The emitter behind ``stdio_error`` is reached from five places, and each has to
satisfy the same three-part contract: the record names the boundary rather than
inferring it from the exception, nothing but bounded identifiers travels with
it, and ``pid`` appears only once a child exists.

Two properties matter enough to be tested rather than assumed. A caller's
secret must not reach an exported diagnostic, however the caller spelled it —
in an exception message, a path, or a borrowed object. And a diagnostic
consumer that fails must not change what the caller sees, because these run
where a failure or a cancellation is already in flight.

The emitter's own projections are driven directly here, which keeps each test
to the one call under inspection. The boundaries themselves are driven through
their real call sites — the owned-path open and the borrowed flush below, and a
live run in ``test_safe_cmd_stdin_stream`` and
``test_stdin_source_failure_classification``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import io
import logging
import time
import typing as typ

import pytest

from cuprum import sh
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum._stdio_diagnostics import _emit_stdio_error, _StdioFailure
from cuprum._stdio_plan import _resolve_stdio
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum._subprocess_spawn import (
    _flush_borrowed_stdio,
    _open_owned_stdio,
)
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.context import ScopeConfig, scoped
from cuprum.events import ExecEvent, StdioFailureCategory
from cuprum.sh import ExecutionContext, RunOutputOptions, SafeCmd
from cuprum.sh.stdio import StdioTarget
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

# A value that could not occur by accident, injected wherever caller data
# reaches a boundary. Every assertion below checks the *absence* of this string.
_SENTINEL = "sentinel-must-not-be-exported-4f1c"


class _FlushFailureError(Exception):
    """The failure a borrowed object's ``flush`` raises."""


class _ObserveFailureError(Exception):
    """The failure a broken observe hook raises."""


@functools.cache
def _probe_command() -> SafeCmd:
    """Return a real :class:`SafeCmd` for the emitter's observations to name.

    A stand-in object will not do. :meth:`_StageObservation.emit` reads the
    command's program, argv, and project name to build the event, and
    :func:`~cuprum._timeout_reporting._safe_emit` swallows whatever that
    raises. A stub would therefore produce no event at all, and every
    assertion below would pass vacuously against an empty list.

    Returns
    -------
    SafeCmd
        A real command whose fields the emitter can read.
    """
    catalogue, program = python_catalogue()
    builder = sh.make(program, catalogue=catalogue)
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
        return builder("-c", "pass")


def _observation(
    hooks: cabc.Iterable[cabc.Callable[[ExecEvent], None]] = (),
) -> _StageObservation:
    """Build a stage observation whose observe hooks are *hooks*."""
    return _StageObservation(
        cmd=_probe_command(),
        hooks=_ExecutionHooks(
            before_hooks=(),
            after_hooks=(),
            observe_hooks=tuple(hooks),
        ),
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=time.monotonic,
    )


def _recording_hooks(
    events: list[ExecEvent],
) -> tuple[cabc.Callable[[ExecEvent], None],]:
    """Return an observe hook that appends every event it receives."""

    def hook(event: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(event)

    return (hook,)


def _execution(
    *,
    stdout: StdioTarget | None = None,
    stderr: StdioTarget | None = None,
    hooks: cabc.Iterable[cabc.Callable[[ExecEvent], None]] = (),
) -> _SubprocessExecution:
    """Build the run bundle the pre-spawn standard-stream helpers take.

    Capture and echo are off, which is not incidental: a redirected stream
    beside either is refused at construction, because there is no parent-side
    pipe left to read. These helpers are reached only for runs that make that
    choice, so the fixture makes it too.

    Returns
    -------
    _SubprocessExecution
        The run bundle, with every stream the caller did not redirect
        inherited.
    """
    options = RunOutputOptions(
        capture=False,
        stdout=StdioTarget.inherit() if stdout is None else stdout,
        stderr=StdioTarget.inherit() if stderr is None else stderr,
    )
    return _SubprocessExecution(
        cmd=_probe_command(),
        ctx=ExecutionContext(),
        capture=False,
        echo_stdout=False,
        echo_stderr=False,
        max_echo_line_bytes=None,
        sink_session=None,
        timeout=None,
        observation=_observation(hooks),
        stdio=_resolve_stdio(None, options),
    )


class _RecordingHandler(logging.Handler):
    """Capture the records the ``cuprum.stdio`` logger emits."""

    def __init__(self) -> None:
        """Start with no captured records."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Retain one emitted record."""
        self.records.append(record)

    def fields(self, index: int = 0) -> dict[str, object]:
        """Return the bounded ``cuprum_`` fields attached to one record."""
        record = self.records[index]
        return {
            name: getattr(record, name)
            for name in (
                "cuprum_pid",
                "cuprum_operation",
                "cuprum_error_type",
                "cuprum_error_category",
            )
            if hasattr(record, name)
        }

    def rendered(self, index: int = 0) -> str:
        """Render every part of one record as text, for absence assertions.

        The message, the formatting arguments, and every attribute are included
        together. A secret search limited to the formatted message would miss a
        value smuggled in through ``extra`` or an unformatted ``%s`` argument,
        and those are precisely the routes this emits through.

        Returns
        -------
        str
            Every part of the record, rendered as text.
        """
        record = self.records[index]
        return " ".join((
            record.getMessage(),
            repr(record.args),
            repr(vars(record)),
        ))


@pytest.fixture
def stdio_log() -> cabc.Iterator[_RecordingHandler]:
    """Attach a capturing handler to the stdio logger for one test."""
    handler = _RecordingHandler()
    logger = logging.getLogger("cuprum.stdio")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class TestEveryBoundaryNamesItself:
    """Each boundary reports its own category, operation, and error class."""

    @pytest.mark.parametrize(
        ("category", "operation"),
        [
            (StdioFailureCategory.PRODUCER, "produce"),
            (StdioFailureCategory.INVALID_CHUNK, "write"),
            (StdioFailureCategory.ENCODER, "write"),
            (StdioFailureCategory.PIPE, "write"),
            (StdioFailureCategory.OWNED_PATH_OPEN, "open"),
            (StdioFailureCategory.BORROWED_FLUSH, "flush"),
        ],
    )
    def test_the_emitted_event_carries_the_supplied_boundary(
        self,
        category: StdioFailureCategory,
        operation: str,
    ) -> None:
        """The category is passed through, never inferred from the exception.

        Two boundaries raise ``TypeError`` and two raise ``OSError`` in the pipe
        family, so a record classified from the exception type alone would name
        the wrong boundary for half of them. This pins that the emitter reports
        what it was told rather than what it could guess.
        """
        events: list[ExecEvent] = []

        _emit_stdio_error(
            _observation(_recording_hooks(events)),
            _StdioFailure(
                category=category,
                operation=operation,
                error_type="ValueError",
                pid=4321,
            ),
        )

        assert len(events) == 1, f"expected exactly one event, found {events}"
        emitted = events[0]
        assert emitted.phase == "stdio_error", (
            f"the event must be a stdio_error, found {emitted.phase!r}"
        )
        assert emitted.error_category is category, (
            f"the event must carry {category!r}, found {emitted.error_category!r}"
        )
        assert emitted.operation == operation, (
            f"the event must carry operation {operation!r}, found {emitted.operation!r}"
        )
        assert emitted.error_type == "ValueError", (
            f"the event must carry the exception class, found {emitted.error_type!r}"
        )

    @pytest.mark.parametrize("pid", [None, 4321])
    def test_a_missing_child_is_absent_rather_than_invented(
        self,
        pid: int | None,
        stdio_log: _RecordingHandler,
    ) -> None:
        """``pid`` travels where a child exists and is omitted where none does.

        The pre-spawn boundaries cannot name a child. A defaulted parameter
        would let a call site omit it by accident and read as "no child"; the
        required parameter makes each site state which side of the fork it is
        on, and this pins that both values survive the projection.
        """
        events: list[ExecEvent] = []

        _emit_stdio_error(
            _observation(_recording_hooks(events)),
            _StdioFailure(
                category=StdioFailureCategory.OWNED_PATH_OPEN,
                operation="open",
                error_type="FileNotFoundError",
                pid=pid,
            ),
        )

        fields = stdio_log.fields()
        assert fields.get("cuprum_pid") == pid, (
            f"the record must carry pid {pid!r} exactly, found "
            f"{fields.get('cuprum_pid')!r}"
        )
        assert events[0].pid == pid, (
            f"the event must carry pid {pid!r}, found {events[0].pid!r}"
        )

    def test_the_log_record_renders_the_category_as_a_plain_string(
        self,
        stdio_log: _RecordingHandler,
    ) -> None:
        """A log consumer must read ``producer``, not the member's ``repr``.

        The category is a ``StrEnum``. Passing the member through would put
        ``StdioFailureCategory.PRODUCER`` on the record, which no operator can
        filter on and which changes if the class is renamed.
        """
        _emit_stdio_error(
            _observation(),
            _StdioFailure(
                category=StdioFailureCategory.PRODUCER,
                operation="produce",
                error_type="ValueError",
                pid=4321,
            ),
        )

        assert stdio_log.fields()["cuprum_error_category"] == "producer", (
            "the record must hold the plain category string"
        )
        assert stdio_log.records[0].getMessage().startswith("stdio_produce_failed"), (
            "the message must name the operation, found "
            f"{stdio_log.records[0].getMessage()!r}"
        )


class TestDiagnosticsReachEveryChannel:
    """The one event drives the metrics, tracing, and logging projections.

    The emitter writes to two channels itself — a ``cuprum.stdio`` record and
    an observe event — and the observe event is what the three adapters
    project. The chained case is the one worth pinning: adapters receive the
    event, not the emitter's own record, so an emitter that logged correctly
    and projected wrongly would pass a direct assertion on the logger alone.
    """

    def test_the_correlated_event_increments_the_boundary_counter(self) -> None:
        """One diagnostic increments ``cuprum_stdio_errors_total`` exactly once.

        The counter is the only signal an operator gets without log access, so
        a projection that dropped or double-counted this phase would be a
        silent blind spot. The label carries the boundary; ``program`` and
        ``project`` stay the only common labels, so the category cannot
        multiply every other series.
        """
        collector = _LabelRecordingMetrics()
        events: list[ExecEvent] = []
        observation = _observation(
            (*_recording_hooks(events), MetricsHook(collector)),
        )

        _emit_stdio_error(
            observation,
            _StdioFailure(
                category=StdioFailureCategory.ENCODER,
                operation="write",
                error_type="LookupError",
                pid=None,
            ),
        )

        assert [op.name for op in collector.calls] == ["cuprum_stdio_errors_total"], (
            f"expected exactly one counter increment, found {collector.calls!r}"
        )
        labels = collector.calls[0].labels
        assert labels["error_category"] == "encoder", (
            f"the boundary must label its own counter, found {labels!r}"
        )
        assert set(labels) == {"program", "project", "error_category"}, (
            "the boundary category must not widen the common label set; "
            f"found {sorted(labels)}"
        )
        assert not collector.histograms, (
            f"a stdio failure is counted, never timed; found {collector.histograms!r}"
        )

    def test_the_event_carries_the_observations_correlation_token(self) -> None:
        """Every event names its own execution, so channels can be joined.

        The token is minted once per stage observation and shared by every
        event it emits. Without it a consumer cannot tell which run a
        diagnostic belongs to when several are interleaved.
        """
        events: list[ExecEvent] = []
        observation = _observation(_recording_hooks(events))

        _emit_stdio_error(
            observation,
            _StdioFailure(
                category=StdioFailureCategory.PIPE,
                operation="write",
                error_type="BrokenPipeError",
                pid=99,
            ),
        )

        assert events[0].exec_id == observation.exec_id, (
            "the event must carry the observation's correlation token, found "
            f"{events[0].exec_id!r}"
        )
        assert events[0].exec_id is not None, (
            "a diagnostic without a token cannot be correlated at all"
        )

    def test_a_span_event_names_the_boundary_as_a_plain_string(self) -> None:
        """The tracing projection renders the category the same way logging does.

        Both adapters must agree on the wire form. A span carrying the
        ``StrEnum`` member while the log record carries its value would make
        the two channels disagree about what a backend receives.
        """
        tracer = InMemoryTracer()
        events: list[ExecEvent] = []
        observation = _observation(
            (*_recording_hooks(events), TracingHook(tracer)),
        )

        # ``_record_span_event`` attaches to a span opened by a ``start`` event
        # for the same token, so the span is opened the way a run opens it.
        observation.emit(
            "start",
            _EventDetails(pid=4321),
        )
        _emit_stdio_error(
            observation,
            _StdioFailure(
                category=StdioFailureCategory.INVALID_CHUNK,
                operation="write",
                error_type="TypeError",
                pid=4321,
            ),
        )

        span = tracer.spans[0]
        recorded = dict(span.events)
        assert "cuprum.stdio_error" in recorded, (
            f"the span must carry the diagnostic event, found {list(recorded)}"
        )
        attrs = recorded["cuprum.stdio_error"]
        assert attrs["error_category"] == "invalid_chunk", (
            f"the span must name the boundary as a string, found {attrs!r}"
        )
        assert attrs["error_type"] == "TypeError", (
            f"the span must name the exception class, found {attrs!r}"
        )


class TestCallerSecretsStayOutOfDiagnostics:
    """Caller data never reaches an exported diagnostic."""

    def test_a_secret_cannot_travel_through_the_emitter(
        self,
        stdio_log: _RecordingHandler,
    ) -> None:
        """The emitter takes the exception's *class*, so no message can travel.

        The parameter is a class name rather than an exception, which is the
        structural reason the message cannot leak. This pins the observable
        consequence: nothing the emitter wrote mentions the secret, on either
        channel.

        The command the observation names carries the same secret in its argv
        and in a caller tag, so the two routes *beside* the exception — the
        command line and the caller's own metadata — are exercised rather than
        assumed. The event itself legitimately carries both, as every event
        does; what must stay bounded is what the adapters project *out of* it,
        which is what the span-event projection below reads.
        """
        tracer = InMemoryTracer()
        events: list[ExecEvent] = []
        observation = _observation(
            (*_recording_hooks(events), TracingHook(tracer)),
        )
        observation.tags = {"caller_note": _SENTINEL}
        observation.cmd = dc.replace(observation.cmd, argv=(_SENTINEL,))
        observation.emit("start", _EventDetails(pid=4321))

        _emit_stdio_error(
            observation,
            _StdioFailure(
                category=StdioFailureCategory.PRODUCER,
                operation="produce",
                error_type="PermissionError",
                pid=4321,
            ),
        )

        assert events, "the emitter must have produced an event to inspect"
        # The command really does carry the secret, so the absence checks below
        # are about the projection rather than about a secret never supplied.
        assert _SENTINEL in repr(observation.cmd.argv), (
            "the probe must actually carry the secret in its argv"
        )
        assert _SENTINEL not in stdio_log.rendered(), (
            "no part of the log record may export caller data"
        )
        span_event = dict(tracer.spans[0].events)["cuprum.stdio_error"]
        assert _SENTINEL not in repr(span_event), (
            "no span-event attribute may export caller data; the projection is "
            f"bounded to its own field set, found {span_event!r}"
        )
        assert not any(key.startswith("cuprum.argv") for key in span_event), (
            "the diagnostic keeps its bounded field set, not the argv"
        )

    def test_a_secret_in_a_failing_path_stays_out(
        self,
        tmp_path: Path,
        stdio_log: _RecordingHandler,
    ) -> None:
        """The path reaches the caller's exception, not the diagnostic.

        ``_open_owned_path`` puts the failing path in the ``OSError`` it raises,
        which is right — the caller asked for that file and needs to know which
        one. The diagnostic beside it names the boundary only.
        """
        # A path whose *parent* does not exist, so the open cannot succeed and
        # the failed path is the one carrying the sentinel.
        unopenable = tmp_path / _SENTINEL / "target.txt"
        events: list[ExecEvent] = []
        execution = _execution(
            stdout=StdioTarget.path(unopenable),
            hooks=_recording_hooks(events),
        )

        with pytest.raises(OSError, match=_SENTINEL) as info:
            _open_owned_stdio(execution)

        assert _SENTINEL in str(info.value), (
            "the caller's exception must still name the path it could not open"
        )
        # Asserted before the absence checks, and separately: an empty channel
        # contains no sentinel either, so without this the test passes whether
        # or not the diagnostic was emitted at all.
        assert [event.error_category for event in events] == [
            StdioFailureCategory.OWNED_PATH_OPEN
        ], f"the boundary must be named, found {events}"
        assert [event.pid for event in events] == [None], (
            "no child exists before the fork, so no pid may be invented"
        )
        assert len(stdio_log.records) == 1, (
            "the boundary must log its own record, found "
            f"{[record.getMessage() for record in stdio_log.records]}"
        )
        assert _SENTINEL not in stdio_log.rendered(), (
            "the log record must not export the failing path"
        )
        assert _SENTINEL not in repr(events), (
            "the emitted event must not export the failing path"
        )

    def test_a_secret_in_a_borrowed_object_stays_out(
        self,
        tmp_path: Path,
        stdio_log: _RecordingHandler,
    ) -> None:
        """A borrowed object's own failure is diagnosed without its message.

        The borrowed object is the caller's, so whatever its ``flush`` raises is
        caller data. The boundary travels; the message does not, and the
        exception still propagates.
        """
        events: list[ExecEvent] = []
        borrowed = _FailingFlushFile(
            tmp_path / "caller-owned.txt",
            f"{_SENTINEL}: the caller's own failure",
        )
        execution = _execution(
            stdout=StdioTarget.fd(borrowed),
            hooks=_recording_hooks(events),
        )

        try:
            with pytest.raises(_FlushFailureError) as info:
                _flush_borrowed_stdio(execution)
        finally:
            borrowed.close()

        assert _SENTINEL in str(info.value), (
            "the caller's own exception must propagate unchanged"
        )
        # Asserted before the absence checks, for the reason the owned-path
        # case documents: an unemitted channel would satisfy them vacuously.
        assert [event.error_category for event in events] == [
            StdioFailureCategory.BORROWED_FLUSH
        ], f"the boundary must be named, found {events}"
        assert [event.pid for event in events] == [None], (
            "the flush runs before the fork, so no pid may be invented"
        )
        assert len(stdio_log.records) == 1, (
            "the boundary must log its own record, found "
            f"{[record.getMessage() for record in stdio_log.records]}"
        )
        assert _SENTINEL not in stdio_log.rendered(), (
            "the log record must not export the borrowed object's message"
        )
        assert _SENTINEL not in repr(events), (
            "the emitted event must not export the borrowed object's message"
        )


class TestFailingConsumersDoNotAlterTheOutcome:
    """A broken diagnostic sink never becomes the caller's outcome."""

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(_ObserveFailureError("hook exploded"), id="hook-raises"),
            pytest.param(asyncio.CancelledError(), id="hook-cancels"),
        ],
    )
    def test_a_failing_observe_hook_does_not_replace_the_diagnostic(
        self,
        failure: BaseException,
    ) -> None:
        """The hook's failure is swallowed; the emitter still returns.

        These run while a producer failure or a cancellation is already
        unwinding. A hook that raised from here would replace the exception the
        caller is owed with the diagnostic's own — turning a broken producer
        into an unexplained observe failure. ``CancelledError`` is included
        because the emitter runs on the cancellation path too.
        """

        def exploding_hook(event: ExecEvent) -> None:
            """Fail the way a broken diagnostic consumer would."""
            del event
            raise failure

        _emit_stdio_error(
            _observation((exploding_hook,)),
            _StdioFailure(
                category=StdioFailureCategory.PRODUCER,
                operation="produce",
                error_type="ValueError",
                pid=4321,
            ),
        )

    def test_a_failing_log_handler_does_not_stop_the_observe_event(self) -> None:
        """The channels are guarded separately, so one failure spares the other.

        The log record is emitted first. If the logger's failure escaped, a
        raising handler would take the observe event with it and a registered
        collector — the channel operators actually read — would see nothing.
        """
        events: list[ExecEvent] = []
        logger = logging.getLogger("cuprum.stdio")
        exploding = _ExplodingHandler()
        logger.addHandler(exploding)
        try:
            _emit_stdio_error(
                _observation(_recording_hooks(events)),
                _StdioFailure(
                    category=StdioFailureCategory.PIPE,
                    operation="write",
                    error_type="OSError",
                    pid=1,
                ),
            )
        finally:
            logger.removeHandler(exploding)

        assert events, "a failing log handler must not suppress the observe event"


class _FailingFlushFile(io.FileIO):
    """A borrowed file object whose ``flush`` fails until it is closed.

    Subclassing a real descriptor rather than standing in for one is what the
    boundary under test demands. ``StdioTarget.fd`` accepts only an ``int`` or
    an open file object, and resolution then takes the object's real
    ``fileno()``; a double carrying a ``fileno`` attribute would satisfy the
    test while exercising a path no caller can reach. Being a file also keeps
    the descriptor genuine, so the failure under test is the flush and nothing
    else.

    The failure is armed until :meth:`close`, because a real descriptor's
    ``close`` flushes too: a double that failed unconditionally would raise
    from the test's own cleanup and mask the assertions it had just made.
    """

    def __init__(self, path: Path, message: str) -> None:
        """Open *path* for writing and capture the failure's message."""
        super().__init__(path, mode="wb")
        self._message = message
        self._failing = True

    def flush(self) -> None:
        """Fail, carrying the caller's own message.

        Real descriptor close also flushes, so the failure is armed only while
        the test is still watching. Left armed, the ``finally`` cleanup would
        raise from ``close`` and mask whatever the assertions had established.
        """
        if self._failing:
            raise _FlushFailureError(self._message)

    def close(self) -> None:
        """Stop failing, then close the descriptor normally."""
        self._failing = False
        super().close()


class _ExplodingHandler(logging.Handler):
    """A handler whose every emit fails, as a broken sink would.

    Subclassed rather than given a patched ``emit``: the logger calls ``emit``
    on the handler instance, so overriding it here is the same call a broken
    sink makes while keeping the override a real method. Assigning a function
    onto an instance would work at runtime and leave the type of ``emit``
    wrong, which is the defect the type checker exists to catch.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Fail, so the emitter's separate guard is what this measures."""
        del record
        msg = "handler exploded"
        raise _ObserveFailureError(msg)


@dc.dataclass(frozen=True, slots=True)
class _CounterCall:
    """One counter increment as the hook applied it."""

    name: str
    value: float
    labels: cabc.Mapping[str, str]


class _LabelRecordingMetrics:
    """A collector that keeps each call's labels.

    ``InMemoryMetrics`` deliberately discards labels, which is enough for the
    phase-count oracles but cannot answer whether the boundary category
    reached the backend. This keeps them, so a label projected onto the wrong
    series or dropped entirely is visible rather than inferred.
    """

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[_CounterCall] = []
        # Recorded separately so a histogram written where only a counter was
        # expected is caught, rather than merged into the same list.
        self.histograms: list[str] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record one counter increment with its labels."""
        self.calls.append(_CounterCall(name=name, value=value, labels=dict(labels)))

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record that a histogram was observed, which this phase must not do."""
        del value, labels
        self.histograms.append(name)
