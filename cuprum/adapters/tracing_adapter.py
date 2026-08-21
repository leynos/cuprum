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
import threading
import typing as typ

from cuprum.adapters._support import (
    _event_common_fields,
    _log_unhandled_phase,
    _prefixed,
    _project_tag,
)
from cuprum.adapters._tracing_protocols import Span, Tracer
from cuprum.adapters.tracing_memory import InMemorySpan, InMemoryTracer

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent, ExecHook, ExecId


# Ancillary span-event fields; the timeout pair distinguishes expiry modes.
_SPAN_FIELDS = ("line", "operation", "error_type", "note", "timeout_s", "timeout_mode")

# An execution's span is ended by its ``exit`` event, but not every execution
# emits one: cleanup also runs on external cancellation and on an unexpected
# stdin-writer failure, and on those paths the original exception propagates
# without an ``exit`` (see the ``ExecEvent.phase`` docstring, which documents
# ``teardown_error`` as possibly the last event a consumer sees). Without a
# bound, every such execution would leave an entry behind for the lifetime of
# the hook. The cap is generous enough that no realistic burst of concurrent
# executions is likely to evict a live span; see ``_evict_overflow_locked``
# for what the ordering does and does not promise.
_MAX_ACTIVE_SPANS = 1024


class TracingHook:
    """Observe hook that creates OpenTelemetry-style traces.

    The hook creates a span for each command execution, starting at the
    ``start`` event and ending at the ``exit`` event. Output lines are
    recorded as span events.

    Span attributes include:

    - ``cuprum.program``: The program being executed
    - ``cuprum.argv``: Full argument vector
    - ``cuprum.pid``: Process ID (when available)
    - ``cuprum.cwd``: Working directory (when set)
    - ``cuprum.exit_code``: Exit code (set on exit)
    - ``cuprum.duration_s``: Duration in seconds (set on exit)
    - ``cuprum.project``: Project name from tags (when present)
    - ``cuprum.pipeline_stage_index``: Pipeline stage index (when present)
    - ``cuprum.pipeline_stages``: Total pipeline stage count (when present)

    Correlation
    -----------
    Active spans are keyed by :attr:`~cuprum.events.ExecEvent.exec_id`, the
    stable per-execution token Cuprum mints once per execution and repeats on
    every lifecycle event. This is what lets ``stdout``/``stderr``/``exit``
    events find their span even when the operating system recycles a process
    identifier: two executions that share a PID have distinct ``exec_id``
    values, so their events never cross. The PID is still recorded as the
    ``cuprum.pid`` span attribute for observability, but it is not used to
    correlate events.

    Legacy or manually constructed events that carry no ``exec_id`` (``None``)
    cannot be correlated safely, so the hook deliberately ignores them rather
    than risk attaching output or exit to an unrelated execution's span. A
    ``start`` without an ``exec_id`` creates no span; a ``stdout``/``stderr``/
    ``exit`` without one is dropped.

    Parameters
    ----------
    tracer:
        A :class:`Tracer` implementation for the target backend.
    record_output:
        If True, record stdout/stderr lines as span events. Default True.

    Example
    -------
    ::

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with sh.observe(hook):
            cmd.run_sync()

        assert len(tracer.spans) == 1
        assert tracer.spans[0].attributes["cuprum.program"] == "echo"

    """

    __slots__ = ("_active_spans", "_lock", "_record_output", "_tracer")

    def __init__(self, tracer: Tracer, *, record_output: bool = True) -> None:
        """Initialize the tracing hook with a tracer."""
        self._tracer = tracer
        self._record_output = record_output
        self._active_spans: collections.OrderedDict[ExecId, Span] = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()

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
            case "stdin_error" | "timeout" | "teardown_error":
                self._record_span_event(event)
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
        ``set_status``/``end`` blocks on I/O cannot serialise other executions'
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
        span = self._tracer.start_span(span_name, attributes)

        with self._lock:
            # Swap atomically: capture any span already mapped to this exec_id
            # and install the replacement in a single critical section, so a
            # concurrent handler for the same token sees either the old span or
            # the replacement, never a missing/partial entry.
            stale = self._active_spans.get(exec_id)
            self._active_spans[exec_id] = span
            abandoned = self._evict_overflow_locked()

        for span_to_close in abandoned:
            # Detached from the map, so exactly one handler ends each. Ended
            # outside the lock for the same reason as the stale span below.
            span_to_close.set_status(ok=False)
            span_to_close.end()

        if stale is not None:
            # Duplicated/reused exec_id: the prior span is now detached from the
            # map, so exactly one handler ends it. Mark and end it outside the
            # lock — a production Span may block on I/O in set_status()/end(),
            # and holding the lifecycle lock across that would serialise every
            # other execution's handler.
            stale.set_status(ok=False)
            stale.end()

    def _evict_overflow_locked(self) -> list[Span]:
        """Detach the least recently active spans once the cap is exceeded.

        Must be called with ``self._lock`` held. Returns the detached spans so
        the caller can end them outside the lock — an arbitrary ``Span`` may
        block on I/O in ``set_status``/``end``, and holding the lifecycle lock
        across that would serialise every other execution's handler.

        Order is recency of activity, not of arrival: ``_record_span_event``
        moves a span to the back whenever one of its events lands, so a
        long-running execution that is still producing output is not evicted
        ahead of a short one that went quiet. That is a heuristic, not a
        guarantee — a live but silent execution can still be evicted, and its
        span is then ended as failed while the process runs on. Evicting is
        nonetheless preferred to refusing to register new spans: the cap exists
        because executions that never emit ``exit`` accumulate, and a registry
        that rejected newcomers instead would stop tracing *every* subsequent
        execution once the leaked ones filled it.
        """
        if len(self._active_spans) <= _MAX_ACTIVE_SPANS:
            return []
        overflow = len(self._active_spans) - _MAX_ACTIVE_SPANS
        return [
            self._active_spans.pop(stale_id)
            for stale_id in list(self._active_spans)[:overflow]
        ]

    def _record_span_event(self, event: ExecEvent) -> None:
        """Record ``event``'s diagnostic fields as a span event, keyed by exec_id."""
        exec_id = event.exec_id
        if exec_id is None:
            return

        with self._lock:
            span = self._active_spans.get(exec_id)
            if span is not None:
                # Activity is what keeps a span off the eviction end of the
                # registry; see _evict_overflow_locked.
                self._active_spans.move_to_end(exec_id)

        if span is None:
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
        span.add_event(f"cuprum.{event.phase}", event_attrs)

    def _handle_exit(self, event: ExecEvent) -> None:
        """End the span for command execution, correlated by ``exec_id``."""
        exec_id = event.exec_id
        if exec_id is None:
            return

        with self._lock:
            span = self._active_spans.pop(exec_id, None)

        if span is None:
            return

        if event.exit_code is not None:
            span.set_attribute("cuprum.exit_code", event.exit_code)
        if event.duration_s is not None:
            span.set_attribute("cuprum.duration_s", event.duration_s)

        ok = event.exit_code == 0 if event.exit_code is not None else True
        span.set_status(ok=ok)
        span.end()

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
