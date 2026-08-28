"""OpenTelemetry-style tracing adapter for Cuprum execution events.

This module provides an observe hook that creates distributed traces for
command execution. The adapter demonstrates how to:

- Create spans for command execution lifecycle
- Attach structured attributes to spans
- Handle parent-child span relationships for pipelines
- Record span events for output lines

The implementation uses protocol classes to remain decoupled from specific
tracing libraries. Projects can implement the protocols with their preferred
backend (OpenTelemetry, Jaeger, Zipkin, etc.).

Example with the in-memory reference implementation::

    from cuprum import ScopeConfig, scoped, sh
    from cuprum.adapters.tracing_adapter import TracingHook, InMemoryTracer

    tracer = InMemoryTracer()

    with scoped(
        ScopeConfig(allowlist=my_allowlist)
    ), sh.observe(TracingHook(tracer)):
        sh.make(ECHO)("hello").run_sync()

    print(tracer.spans)  # [Span(name='cuprum.exec echo', ...)]

Example with OpenTelemetry::

    from opentelemetry import trace
    from cuprum.adapters.tracing_adapter import Tracer, Span, TracingHook

    class OTelSpan:
        def __init__(self, otel_span):
            self._span = otel_span

        def set_attribute(self, key, value):
            self._span.set_attribute(key, value)

        def add_event(self, name, attributes=None):
            self._span.add_event(name, attributes=attributes or {})

        def set_status(self, *, ok):
            from opentelemetry.trace import StatusCode
            code = StatusCode.OK if ok else StatusCode.ERROR
            self._span.set_status(code)

        def end(self):
            self._span.end()

    class OTelTracer:
        def __init__(self, tracer):
            self._tracer = tracer

        def start_span(self, name, attributes=None):
            span = self._tracer.start_span(name, attributes=attributes)
            return OTelSpan(span)

    otel_tracer = trace.get_tracer("cuprum")
    hook = TracingHook(OTelTracer(otel_tracer))

"""

from __future__ import annotations

import collections
import dataclasses as dc
import threading
import typing as typ

from cuprum.adapters._support import (
    _event_common_fields,
    _log_span_eviction,
    _log_unhandled_phase,
    _prefixed,
    _project_tag,
)
from cuprum.adapters.tracing_memory import InMemorySpan, InMemoryTracer
from cuprum.adapters.tracing_protocols import Span, Tracer

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent, ExecHook, ExecId


# Ancillary span-event fields; the timeout pair distinguishes expiry modes and
# the grace pair describes the bounded drain outcome without stream contents.
_SPAN_FIELDS = (
    "line",
    "operation",
    "error_type",
    "note",
    "timeout_s",
    "timeout_mode",
    "eof_grace_s",
    "pending_readers",
)

# ``teardown_error`` can be the last event, so without a bound it could retain a
# span for the hook's lifetime. See ``_evict_overflow_locked`` for the ordering
# guarantee.
_MAX_ACTIVE_SPANS = 1024


@dc.dataclass(slots=True)
class _ActiveSpan:
    """One open span and the lock that serializes its callbacks."""

    span: Span
    lock: threading.Lock = dc.field(default_factory=threading.Lock)
    is_closed: bool = False


class TracingHook:
    """Project correlated execution events onto backend spans.

    Events without ``exec_id`` are ignored rather than correlated by a PID.
    Attributes include ``cuprum.program``, ``cuprum.argv``, ``cuprum.pid``,
    ``cuprum.cwd``, ``cuprum.exit_code``, ``cuprum.duration_s``,
    ``cuprum.project``, ``cuprum.pipeline_stage_index``, and
    ``cuprum.pipeline_stages``.

    Parameters
    ----------
    tracer:
        A :class:`Tracer` implementation for the target backend.
    record_output:
        If True, record stdout/stderr lines as span events. Default True.
    """

    __slots__ = (
        "_active_spans",
        "_lock",
        "_record_output",
        "_span_states",
        "_tracer",
    )

    def __init__(self, tracer: Tracer, *, record_output: bool = True) -> None:
        """Initialize the tracing hook with a tracer."""
        self._tracer = tracer
        self._record_output = record_output
        self._active_spans: collections.OrderedDict[ExecId, Span] = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()
        self._span_states: dict[ExecId, _ActiveSpan] = {}

    def __call__(self, event: ExecEvent) -> None:
        """Process an execution event and update tracing."""
        match event.phase:
            case "plan" | "stdin":
                pass
            case "start":
                self._handle_start(event)
            case "stdout" | "stderr":
                if self._record_output:
                    self._record_span_event(event)
            case (
                "stdin_error"
                | "timeout"
                | "teardown_error"
                | "capture_eof_grace_expired"
            ):
                self._record_span_event(event)
            case "pipeline_fail_fast":
                self._record_fail_fast(event)
            case "exit":
                self._handle_exit(event)
            case _:
                _log_unhandled_phase("tracing", event.phase)

    def _handle_start(self, event: ExecEvent) -> None:
        """Start a new span for command execution.

        Spans are keyed on ``event.exec_id`` (see the class ``Correlation``
        notes). Distinct executions always have distinct tokens, so keying by
        ``exec_id`` — rather than the recyclable PID — is what keeps a later
        execution's events off an earlier execution's span.

        Events without an ``exec_id`` cannot be correlated and are ignored, so
        no untracked span is created for them.

        A pre-existing span for the *same* ``exec_id`` should not occur for a
        well-formed event stream (tokens are unique per execution). If one is
        seen — a duplicated or reused token — it is detached from the map and
        ended as failed. The lookup and the installation of the replacement run
        together under ``self._lock`` so the exec_id→span mapping transitions
        atomically: a concurrent ``_record_span_event`` or ``_handle_exit`` for the
        same token observes either the old span or the replacement, never a
        missing or half-updated entry. The detached stale span is then marked
        failed and ended *outside* the lock — exactly once, since it is already
        unreachable via the map — so an arbitrary ``Span`` whose
        ``set_status``/``end`` blocks on I/O cannot serialize other executions'
        handlers. The unrelated tracer setup (building attributes and starting
        the span) likewise runs outside the lock.
        """
        exec_id = event.exec_id
        if exec_id is None:
            return

        # Tracer setup is independent of the span bookkeeping; do it before
        # taking the lock so unrelated handlers are not blocked on it.
        attributes = self._build_attributes(event)
        span_name = f"cuprum.exec {event.program}"
        active_span = _ActiveSpan(self._tracer.start_span(span_name, attributes))

        with self._lock:
            # Swap atomically: capture any span already mapped to this exec_id
            # and install the replacement in a single critical section, so a
            # concurrent handler for the same token sees either the old span or
            # the replacement, never a missing/partial entry.
            stale = self._span_states.get(exec_id)
            self._active_spans[exec_id] = active_span.span
            self._span_states[exec_id] = active_span
            abandoned = self._evict_overflow_locked()
            # Read the size here rather than after the lock: another handler
            # may register or end a span in between, and the record would then
            # report a total that never accompanied this eviction.
            active = len(self._active_spans)

        if abandoned:
            _log_span_eviction("tracing", evicted=len(abandoned), active=active)
        for span_to_close in abandoned:
            # Detached from the map, so exactly one handler ends each. Ended
            # outside the lock for the same reason as the stale span below.
            self._close_span(span_to_close, ok=False)

        if stale is not None:
            # Duplicated/reused exec_id: the prior span is now detached from the
            # map, so exactly one handler ends it. Mark and end it outside the
            # lock — a production Span may block on I/O in set_status()/end(),
            # and holding the lifecycle lock across that would serialize every
            # other execution's handler.
            self._close_span(stale, ok=False)

    def _evict_overflow_locked(self) -> list[_ActiveSpan]:
        """Detach least-recently-active overflow spans while holding the registry lock.

        The caller ends the detached spans outside the lock because a backend
        callback may block. Activity, rather than arrival, determines recency.

        Returns
        -------
        list[_ActiveSpan]
            Detached spans for the caller to close.
        """
        if len(self._active_spans) <= _MAX_ACTIVE_SPANS:
            return []
        overflow = len(self._active_spans) - _MAX_ACTIVE_SPANS
        # ``popitem(last=False)`` takes the front — the least recently active
        # end — without materializing the other thousand-odd keys the way a
        # ``list(...)`` slice would.
        abandoned: list[_ActiveSpan] = []
        for _ in range(overflow):
            exec_id, _ = self._active_spans.popitem(last=False)
            abandoned.append(self._span_states.pop(exec_id))
        return abandoned

    def _record_span_event(self, event: ExecEvent) -> None:
        """Record ``event``'s diagnostic fields as a span event, keyed by exec_id."""
        exec_id = event.exec_id
        if exec_id is None:
            return

        with self._lock:
            active = self._span_states.get(exec_id)
            if active is not None:
                # Activity is what keeps a span off the eviction end of the
                # registry; see _evict_overflow_locked.
                self._active_spans.move_to_end(exec_id)

        if active is None:
            return

        # The span is left open and unmarked. An ``exit`` event closes it when
        # one arrives; ``teardown_error`` carries no such guarantee, so a span
        # left open here is bounded by ``_evict_overflow_locked`` instead. An
        # ancillary event arriving after ``exit`` finds no entry and is dropped
        # by the ``span is None`` guard above rather than touching an ended
        # span.
        event_attrs: dict[str, object] = {}
        for field in _SPAN_FIELDS:
            value = getattr(event, field)
            if value is not None:
                event_attrs[field] = value
        with active.lock:
            if not active.is_closed:
                active.span.add_event(f"cuprum.{event.phase}", event_attrs)

    def _handle_exit(self, event: ExecEvent) -> None:
        """End the span for command execution, correlated by ``exec_id``."""
        exec_id = event.exec_id
        if exec_id is None:
            return

        with self._lock:
            self._active_spans.pop(exec_id, None)
            active = self._span_states.pop(exec_id, None)

        if active is None:
            return

        with active.lock:
            if active.is_closed:
                return
            active.is_closed = True
            if event.exit_code is not None:
                active.span.set_attribute("cuprum.exit_code", event.exit_code)
            if event.duration_s is not None:
                active.span.set_attribute("cuprum.duration_s", event.duration_s)

            ok = event.exit_code == 0 if event.exit_code is not None else True
            active.span.set_status(ok=ok)
            active.span.end()

    def _record_fail_fast(self, event: ExecEvent) -> None:
        """Note a pipeline fail-fast decision on the failing stage's span."""
        active = self._lookup_active_span(event)
        if active is None:
            return

        attrs: dict[str, object] = {}
        for field in ("stage_index", "stage_count", "exit_code", "duration_s"):
            value = getattr(event, field)
            if value is not None:
                attrs[field] = value
        with active.lock:
            if not active.is_closed:
                active.span.add_event("cuprum.pipeline_fail_fast", attrs)

    def _lookup_active_span(self, event: ExecEvent) -> _ActiveSpan | None:
        """Return the active span state for ``event``, when its token is known."""
        exec_id = event.exec_id
        if exec_id is None:
            return None
        with self._lock:
            return self._span_states.get(exec_id)

    @staticmethod
    def _close_span(active: _ActiveSpan, *, ok: bool) -> None:
        """End an active span exactly once without holding the registry lock."""
        with active.lock:
            if active.is_closed:
                return
            active.is_closed = True
            active.span.set_status(ok=ok)
            active.span.end()

    @staticmethod
    def _build_attributes(event: ExecEvent) -> dict[str, object]:
        """Build initial span attributes from an event."""
        attrs = dict(
            _event_common_fields(event, _prefixed("cuprum."), argv=list),
        )

        project = _project_tag(event)
        if project is not None:
            attrs["cuprum.project"] = project
        if "pipeline_stage_index" in event.tags:
            attrs["cuprum.pipeline_stage_index"] = event.tags["pipeline_stage_index"]
        if "pipeline_stages" in event.tags:
            attrs["cuprum.pipeline_stages"] = event.tags["pipeline_stages"]

        return attrs


def tracing_hook(tracer: Tracer, *, record_output: bool = True) -> ExecHook:
    """Create a tracing observe hook for the given tracer.

    This is a convenience factory that returns a :class:`TracingHook` instance
    cast to the :class:`~cuprum.events.ExecHook` type.

    Parameters
    ----------
    tracer:
        A :class:`Tracer` implementation.
    record_output:
        If True, record stdout/stderr lines as span events. Default True.

    Returns
    -------
    ExecHook
        A hook suitable for use with ``sh.observe()``.

    """
    return TracingHook(tracer, record_output=record_output)


__all__ = [
    "InMemorySpan",
    "InMemoryTracer",
    "Span",
    "Tracer",
    "TracingHook",
    "tracing_hook",
]
