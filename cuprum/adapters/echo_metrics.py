"""Metrics adapter for stream-echo failure and truncation events.

Counts how often a text-only echo sink rejected the subprocess output, per
output stream, and how often a bounded echoed line was successfully truncated.
Those facts are otherwise visible only through a ``WARNING`` log record or the
echo observation hook, which dashboards cannot aggregate.

The hook consumes :class:`~cuprum.echo_events.EchoEvent` values from
:func:`~cuprum.echo_observation.observe_echo`. It reuses the
:class:`~cuprum.adapters.metrics_adapter.MetricsCollector` protocol, so one
collector can back the execution, pump, and echo hooks and no new telemetry
dependency is introduced.

Example
-------
::

    from cuprum import sh
    from cuprum.adapters.echo_metrics import EchoMetricsHook
    from cuprum.adapters.metrics_adapter import InMemoryMetrics
    from cuprum.echo_observation import observe_echo

    metrics = InMemoryMetrics()

    with sh.observe(MetricsHook(metrics)), observe_echo(EchoMetricsHook(metrics)):
        command.run_sync(output=RunOutputOptions(echo=True))

    print(metrics.counters["cuprum_echo_encoding_failures_total"])

"""

from __future__ import annotations

import typing as typ

from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream

if typ.TYPE_CHECKING:
    from cuprum.adapters.metrics_adapter import MetricsCollector
    from cuprum.echo_events import EchoHook

ECHO_ENCODING_FAILURES_TOTAL = "cuprum_echo_encoding_failures_total"
"""Echo writes rejected with ``UnicodeEncodeError``, labelled by ``stream``.

The only other label is ``error_category``, whose single value
(:data:`~cuprum.echo_events.EchoErrorCategory.UNICODE_ENCODE`) names the closed
category the counter counts. The label exists so a future failure category
extends the metric rather than silently mixing into this series — which is
what :data:`ECHO_BROKEN_PIPE_TOTAL` then did.
"""

ECHO_BROKEN_PIPE_TOTAL = "cuprum_echo_broken_pipe_total"
"""Echo writes abandoned after ``BrokenPipeError``, labelled by ``stream``.

A distinct series from :data:`ECHO_ENCODING_FAILURES_TOTAL`: both count a
handled echo disablement, but they report different faults with different
remedies. An encoding failure is inherent to the sink's configuration and
recurs for every run that reaches it; a broken pipe means a destination that
was expected to stay open did not, which is exactly the signal an operator
wants to alert on separately. Folding them together would make a permanently
misencoded sink indistinguishable from a downstream reader that keeps
disconnecting.

Only recorded when the caller opted in with
:data:`~cuprum.echo_events.BrokenPipePolicy.BEST_EFFORT`; under the default
policy the error propagates and no event is emitted.
"""

ECHO_TRUNCATIONS_TOTAL = "cuprum_echo_truncations_total"
"""Successfully written bounded echo lines, labelled by ``stream``."""


def _event_labels(event: EchoEvent) -> dict[str, str]:
    """Return the bounded label set for ``event``.

    Only the two closed-set values below reach a label. A sink's type, its
    encoding, any exception text, and the subprocess payload are all either
    unbounded as metric labels or a disclosure risk, and the structured
    ``cuprum_*`` extras on the ``cuprum.stream`` WARNING carry the diagnostic
    detail for the cases that need it.

    Returns
    -------
    dict[str, str]
        The fixed labels allowed for the event's metric series.
    """
    category = (
        str(event.error_category)
        if isinstance(event.error_category, EchoErrorCategory)
        else EchoErrorCategory.UNICODE_ENCODE.value
    )
    stream = str(event.stream) if isinstance(event.stream, EchoStream) else "unknown"
    return {"stream": stream, "error_category": category}


def _truncation_labels(event: EchoEvent) -> dict[str, str]:
    """Return the bounded stream label for a successful truncation."""
    stream = str(event.stream) if isinstance(event.stream, EchoStream) else "unknown"
    return {"stream": stream}


def _record_error_metric(collector: MetricsCollector, event: EchoEvent) -> None:
    """Record *event* on the counter its error category owns.

    The three categories are one dispatch because each owns a distinct series:
    an operator alerts on an unencodable payload and a closing destination
    separately, and confining that mapping to one place keeps a new category
    from silently inheriting another's counter.

    The collector is a parameter rather than the hook's own attribute because a
    method forwarding ``self._record_error_metric(event)`` is a trivial
    attribute wrapper, which this repository's ``R9104`` check rejects. Reading
    the collector through an argument keeps the dispatch callable without a
    hook and keeps :meth:`EchoMetricsHook.__call__` a real call site.

    Parameters
    ----------
    collector:
        The :class:`~cuprum.adapters.metrics_adapter.MetricsCollector` the
        counter is incremented on.
    event:
        The echo event whose error category selects the counter.
    """
    match event.error_category:
        case EchoErrorCategory.UNICODE_ENCODE:
            collector.inc_counter(
                ECHO_ENCODING_FAILURES_TOTAL, 1.0, _event_labels(event)
            )
        case EchoErrorCategory.BROKEN_PIPE:
            # A series of its own, not a relabelling of the encoding counter:
            # the two faults have different remedies and an operator alerts on
            # them separately.
            collector.inc_counter(ECHO_BROKEN_PIPE_TOTAL, 1.0, _event_labels(event))
        case EchoErrorCategory.TRUNCATED:
            collector.inc_counter(
                ECHO_TRUNCATIONS_TOTAL,
                1.0,
                _truncation_labels(event),
            )


class EchoMetricsHook:
    """Echo observation hook that records bounded echo outcomes.

    The hook emits ``cuprum_echo_encoding_failures_total``: incremented once
    per first ``UnicodeEncodeError`` on a drain's echo path, labelled ``stream``
    with ``stdout`` or ``stderr`` and ``error_category`` with
    ``unicode_encode``. It also emits ``cuprum_echo_truncations_total`` once
    per successfully written truncation, labelled only by ``stream``.

    A ``BrokenPipeError`` the caller chose to tolerate under
    :data:`~cuprum.echo_events.BrokenPipePolicy.BEST_EFFORT` increments
    ``cuprum_echo_broken_pipe_total`` instead, with the same label set and
    ``error_category`` of ``broken_pipe``. It is a separate series, so a sink
    that cannot encode is never confused with a destination that keeps
    closing.

    A drain that has already disabled echo emits nothing further, so repeated
    chunks after the first failure do not compound the count.

    Parameters
    ----------
    collector:
        A :class:`~cuprum.adapters.metrics_adapter.MetricsCollector`
        implementation for the target backend. It must be thread-safe: a
        failure can be recorded from any task draining a stream.

    Example
    -------
    ::

        metrics = InMemoryMetrics()

        with observe_echo(EchoMetricsHook(metrics)):
            command.run_sync(output=RunOutputOptions(echo=True))

        assert metrics.counters["cuprum_echo_encoding_failures_total"] == 1.0

    """

    __slots__ = ("_collector",)

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the echo metrics hook with a collector."""
        self._collector = collector

    def __call__(self, event: EchoEvent) -> None:
        """Increment the counter this echo event calls for.

        A collector that raises does not reach the drain: the emitter records
        the failure and lets the drain continue, so a broken metrics backend
        cannot fail a command that would otherwise have captured its output.
        See :func:`cuprum.echo_observation._emit_echo_event`.
        """
        _record_error_metric(self._collector, event)


def echo_metrics_hook(collector: MetricsCollector) -> EchoHook:
    """Create an echo observation hook for the given collector.

    Parameters
    ----------
    collector:
        A :class:`~cuprum.adapters.metrics_adapter.MetricsCollector`
        implementation.

    Returns
    -------
    EchoHook
        A hook suitable for use with :func:`cuprum.echo_observation.observe_echo`.
    """
    return EchoMetricsHook(collector)


__all__ = [
    "ECHO_BROKEN_PIPE_TOTAL",
    "ECHO_ENCODING_FAILURES_TOTAL",
    "ECHO_TRUNCATIONS_TOTAL",
    "EchoMetricsHook",
    "echo_metrics_hook",
]
