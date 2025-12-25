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

    from cuprum import scoped, sh
    from cuprum.adapters.tracing_adapter import TracingHook, InMemoryTracer

    tracer = InMemoryTracer()

    with scoped(allowlist=my_allowlist), sh.observe(TracingHook(tracer)):
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
        ...

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
        ...

    def set_status(self, *, ok: bool) -> None:
        """Set the span status.

        Parameters
        ----------
        ok:
            True if the operation succeeded, False otherwise.

        """
        ...

    def end(self) -> None:
        """End the span, recording its duration."""
        ...


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
        ...


@dc.dataclass(slots=True)
class InMemorySpan:
    """Reference in-memory span for testing and examples."""

    name: str
    attributes: dict[str, object] = dc.field(default_factory=dict)
    events: list[tuple[str, dict[str, object]]] = dc.field(default_factory=list)
    status_ok: bool | None = None
    ended: bool = False

    def set_attribute(self, key: str, value: object) -> None:
        """Set a span attribute."""
        self.attributes[key] = value

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Add an event to the span."""
        self.events.append((name, dict(attributes) if attributes else {}))

    def set_status(self, *, ok: bool) -> None:
        """Set the span status."""
        self.status_ok = ok

    def end(self) -> None:
        """End the span."""
        self.ended = True


@dc.dataclass(slots=True)
class InMemoryTracer:
    """Reference in-memory tracer for testing and examples.

    This tracer stores spans in memory for inspection. It is thread-safe
    and suitable for unit testing but not for production use.

    Attributes
    ----------
    spans:
        List of all spans created by this tracer.

    """

    spans: list[InMemorySpan] = dc.field(default_factory=list)
    _lock: threading.Lock = dc.field(default_factory=threading.Lock, repr=False)

    def start_span(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> InMemorySpan:
        """Start a new in-memory span."""
        span = InMemorySpan(
            name=name,
            attributes=dict(attributes) if attributes else {},
        )
        with self._lock:
            self.spans.append(span)
        return span

    def reset(self) -> None:
        """Clear all collected spans."""
        with self._lock:
            self.spans.clear()


class TracingHook:
    """Observe hook that creates OpenTelemetry-style traces.

    The hook creates a span for each command execution, starting at the
    ``start`` event and ending at the ``exit`` event. Output lines are
    recorded as span events.

    Span attributes include:

    - ``cuprum.program``: The program being executed
    - ``cuprum.argv``: Full argument vector
    - ``cuprum.pid``: Process ID
    - ``cuprum.exit_code``: Exit code (set on exit)
    - ``cuprum.duration_s``: Duration in seconds (set on exit)
    - ``cuprum.project``: Project name from tags
    - ``cuprum.pipeline_stage_index``: Pipeline stage index (if applicable)

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
            case "plan":
                pass
            case "start":
                self._handle_start(event)
            case "stdout" | "stderr":
                if self._record_output:
                    self._handle_output(event)
            case "exit":
                self._handle_exit(event)

    def _handle_start(self, event: ExecEvent) -> None:
        """Start a new span for command execution."""
        if event.pid is None:
            return

        attributes = self._build_attributes(event)
        span_name = f"cuprum.exec {event.program}"
        span = self._tracer.start_span(span_name, attributes)

        with self._lock:
            self._active_spans[event.pid] = span

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
        attrs: dict[str, object] = {
            "cuprum.program": str(event.program),
            "cuprum.argv": list(event.argv),
        }
        if event.pid is not None:
            attrs["cuprum.pid"] = event.pid
        if event.cwd is not None:
            attrs["cuprum.cwd"] = str(event.cwd)

        if "project" in event.tags:
            attrs["cuprum.project"] = str(event.tags["project"])
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
