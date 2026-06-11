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

import threading
import typing as typ

from cuprum.adapters._support import (
    _event_common_fields,
    _log_unhandled_phase,
    _prefixed,
    _project_tag,
)
from cuprum.adapters.tracing_memory import InMemorySpan, InMemoryTracer

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


class Span(typ.Protocol):
    """Protocol for a tracing span.

    Spans represent a unit of work (in this case, command execution) and
    can be enriched with attributes and events.
    """

    def set_attribute(self, key: str, value: object) -> None:
        """Set a span attribute.

        Parameters
        ----------
        key:
            Attribute name (e.g., ``cuprum.program``).
        value:
            Attribute value (string, int, float, bool, or list thereof).

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Add an event to the span.

        Parameters
        ----------
        name:
            Event name (e.g., ``cuprum.stdout``).
        attributes:
            Optional attributes for the event.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def set_status(self, *, ok: bool) -> None:
        """Set the span status.

        Parameters
        ----------
        ok:
            True if the operation succeeded, False otherwise.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def end(self) -> None:
        """End the span, recording its duration."""
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis


class Tracer(typ.Protocol):
    """Protocol for a tracing backend.

    Implementations must be thread-safe; hooks may be invoked from multiple
    threads or async tasks concurrently.
    """

    def start_span(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> Span:
        """Start a new span.

        Parameters
        ----------
        name:
            Span name (e.g., ``cuprum.exec echo``).
        attributes:
            Initial span attributes.

        Returns
        -------
        Span
            A span that must be ended by calling :meth:`Span.end`.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis


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
        self._active_spans: dict[int, Span] = {}
        self._lock = threading.Lock()

    def __call__(self, event: ExecEvent) -> None:
        """Process an execution event and update tracing."""
        match event.phase:
            case "plan" | "stdin" | "stdin_error":
                pass
            case "start":
                self._handle_start(event)
            case "stdout" | "stderr":
                if self._record_output:
                    self._handle_output(event)
            case "exit":
                self._handle_exit(event)
            case _:
                _log_unhandled_phase("tracing", event.phase)

    def _handle_start(self, event: ExecEvent) -> None:
        """Start a new span for command execution.

        Spans are keyed on ``event.pid``. PIDs are unique among *running*
        processes, so a live collision cannot occur; however, a recycled PID
        following a missed or out-of-order ``exit`` event would otherwise
        overwrite — and silently orphan — the previous, still-open span. Guard
        against that by ending any still-open span for the PID before
        installing the new one, so every span is terminated exactly once and
        later output/exit events attach to the correct execution.
        """
        if event.pid is None:
            return

        attributes = self._build_attributes(event)
        span_name = f"cuprum.exec {event.program}"
        span = self._tracer.start_span(span_name, attributes)

        with self._lock:
            stale = self._active_spans.get(event.pid)
            self._active_spans[event.pid] = span

        if stale is not None:
            # A span for this PID was never closed (missed/out-of-order exit).
            # End it as unknown rather than leaking it on the recycled PID.
            stale.set_status(ok=False)
            stale.end()

    def _handle_output(self, event: ExecEvent) -> None:
        """Record output as a span event."""
        if event.pid is None:
            return

        with self._lock:
            span = self._active_spans.get(event.pid)

        if span is None:
            return

        event_name = f"cuprum.{event.phase}"
        event_attrs: dict[str, object] = {}
        if event.line is not None:
            event_attrs["line"] = event.line
        span.add_event(event_name, event_attrs)

    def _handle_exit(self, event: ExecEvent) -> None:
        """End the span for command execution."""
        if event.pid is None:
            return

        with self._lock:
            span = self._active_spans.pop(event.pid, None)

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
