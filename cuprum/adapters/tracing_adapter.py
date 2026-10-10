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

import dataclasses as dc
import threading
import typing as typ

from cuprum.adapters._support import (
    _event_common_fields,
    _log_unhandled_phase,
    _prefixed,
    _project_tag,
)
from cuprum.adapters._tracing_fields import _SPAN_FIELDS, write_exit_attributes
from cuprum.adapters._tracing_line_stream import _LineStreamTracingMixin
from cuprum.adapters._tracing_native_pump_cleanup import _NativePumpCleanupTracingMixin
from cuprum.adapters.tracing_memory import InMemorySpan, InMemoryTracer
from cuprum.events import TerminalOutcome
from cuprum.tracing_protocols import Span, Tracer

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent, ExecHook, ExecId


@dc.dataclass(slots=True)
class _ActiveSpan:
    """One open span and the lock that serializes its callbacks."""

    span: Span
    lock: threading.Lock = dc.field(default_factory=threading.Lock)
    is_closed: bool = False


class TracingHook(_LineStreamTracingMixin, _NativePumpCleanupTracingMixin):
    """Project correlated execution events onto backend spans.

    Events without ``exec_id`` are ignored rather than correlated by a PID.
    Attributes include ``cuprum.program``, ``cuprum.argv``, ``cuprum.pid``,
    ``cuprum.cwd``, ``cuprum.exit_code``, ``cuprum.duration_s``,
    ``cuprum.project``, ``cuprum.pipeline_stage_index``, and
    ``cuprum.pipeline_stages``. Every terminal ``exit`` event also carries
    ``cuprum.resource_usage_mode``, naming how its child's resource figures
    were obtained — ``wait4_child``, ``aggregate_cpu_delta``, or
    ``unavailable``. A terminal ``exit`` event that measured its child's
    resource usage additionally carries ``cuprum.max_rss_bytes``,
    ``cuprum.user_cpu_seconds``, and ``cuprum.system_cpu_seconds``; those
    three figures alone are absent rather than null when no measurement
    applies.

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
        "_tracer",
    )

    def __init__(self, tracer: Tracer, *, record_output: bool = True) -> None:
        """Initialize the tracing hook with a tracer."""
        self._tracer = tracer
        self._record_output = record_output
        self._active_spans: dict[ExecId, _ActiveSpan] = {}
        self._lock = threading.Lock()

    def __call__(self, event: ExecEvent) -> None:
        """Process an execution event and update tracing."""
        match event.phase:
            case "plan" | "stdin":
                pass
            case "start":
                self._handle_start(event)
            case (
                "stdout"
                | "stderr"
                | "stdin_error"
                | "timeout"
                | "teardown_error"
                | "capture_eof_grace_expired"
            ):
                if event.phase not in {"stdout", "stderr"} or self._record_output:
                    self._record_span_event(event)
            case "pipeline_fail_fast":
                self._record_fail_fast(event)
            case "exit":
                self._handle_exit(event)
            case "settled":
                self._handle_settled(event)
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
            stale = self._active_spans.get(exec_id)
            self._active_spans[exec_id] = active_span

        if stale is not None:
            # Duplicated/reused exec_id: the prior span is now detached from the
            # map, so exactly one handler ends it. Mark and end it outside the
            # lock — a production Span may block on I/O in set_status()/end(),
            # and holding the lifecycle lock across that would serialize every
            # other execution's handler.
            self._close_span(stale, ok=False)

    def _record_span_event(self, event: ExecEvent) -> None:
        """Record ``event``'s diagnostic fields as a span event, keyed by exec_id."""
        active = self._lookup_active_span(event)
        if active is None:
            return

        # An ancillary event after a terminal event finds no entry and is dropped.
        event_attrs: dict[str, object] = {}
        for field in _SPAN_FIELDS:
            value = getattr(event, field)
            if value is not None:
                event_attrs[field] = value
        with active.lock:
            if not active.is_closed:
                active.span.add_event(f"cuprum.{event.phase}", event_attrs)

    def _handle_exit(self, event: ExecEvent) -> None:
        """Record child-exit details while leaving closure to ``settled``."""
        exec_id = event.exec_id
        if exec_id is None:
            return

        active = self._lookup_active_span(event)
        if active is None:
            return

        with active.lock:
            if active.is_closed:
                return
            write_exit_attributes(active.span, event)

    def _handle_settled(self, event: ExecEvent) -> None:
        """Close a remaining span using only the terminal category."""
        exec_id = event.exec_id
        outcome = event.terminal_outcome
        if exec_id is None or outcome is None:
            return

        with self._lock:
            active = self._active_spans.pop(exec_id, None)
        if active is None:
            return

        with active.lock:
            if active.is_closed:
                return
            active.is_closed = True
            active.span.set_attribute("cuprum.terminal_outcome", str(outcome))
            active.span.set_status(ok=outcome is TerminalOutcome.EXIT_ZERO)
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
        """Return the active span state for ``event``, when its token is known.

        Returns
        -------
        _ActiveSpan or None
            The open span for the event's token, or ``None`` when the event
            carries no token or no span is registered for it.
        """
        exec_id = event.exec_id
        if exec_id is None:
            return None
        with self._lock:
            return self._active_spans.get(exec_id)

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
