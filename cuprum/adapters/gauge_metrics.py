"""Optional gauge capability for metrics collectors.

:class:`~cuprum.adapters.metrics_adapter.MetricsCollector` is the surface every
backend must implement: counters and histograms. A gauge reports a current
absolute value, which some consumers want — the Rust-pump hand-off reports the
descriptor pairs it holds across pending drains — but requiring a third method
of every existing backend would break collectors that are correct today. The
capability is therefore a separate protocol: a consumer tests for it with
``isinstance`` and skips the gauge when the backend does not provide one.
:class:`~cuprum.adapters.metrics_adapter.InMemoryMetrics` implements it.

Examples
--------
::

    from cuprum.adapters.gauge_metrics import GaugeMetricsCollector

    if isinstance(metrics, GaugeMetricsCollector):
        metrics.set_gauge("cuprum_rust_pump_held_pairs", 0.0, {})

"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@typ.runtime_checkable
class GaugeMetricsCollector(typ.Protocol):
    """A metrics backend that can also set an absolute gauge value.

    Gauge support is optional, so a backend written against
    :class:`~cuprum.adapters.metrics_adapter.MetricsCollector` keeps working
    unchanged. Consumers test for this protocol rather than assuming a third
    method, so adding the capability never breaks an existing collector.
    """

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Set a gauge metric to an absolute value.

        Parameters
        ----------
        name:
            Metric name (e.g., ``cuprum_rust_pump_held_pairs``).
        value:
            The current absolute value.
        labels:
            Label key-value pairs for metric dimensions.

        """
        raise NotImplementedError


__all__ = ["GaugeMetricsCollector"]
