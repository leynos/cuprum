"""The reference in-memory metrics collector.

This lives beside :mod:`cuprum.adapters.metrics_adapter` rather than inside it
because it is a complete, self-contained store: it knows nothing about
execution events, while the adapter module holds the hook that translates them
into metric operations. The public import path is unchanged — ``metrics_adapter``
re-exports this name.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.adapters._support import _LockedStore

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass
class InMemoryMetrics(_LockedStore):
    """Reference in-memory metrics collector for testing and examples.

    Storage and locking follow the shared
    :class:`~cuprum.adapters._support._LockedStore` contract: every mutator
    holds the lock, and ``reset()``
    clears the store under it.

    Attributes
    ----------
    counters:
        Dict mapping metric names to accumulated counter values.
    histograms:
        Dict mapping metric names to lists of observed values.
    gauges:
        Dict mapping gauge names to their most recently reported value.

    """

    counters: dict[str, float] = dc.field(default_factory=dict)
    histograms: dict[str, list[float]] = dc.field(default_factory=dict)
    gauges: dict[str, float] = dc.field(default_factory=dict)

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Increment a counter, ignoring labels for simplicity."""
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram observation, ignoring labels for simplicity."""
        with self._lock:
            if name not in self.histograms:
                self.histograms[name] = []
            self.histograms[name].append(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Set a gauge value, ignoring labels for simplicity."""
        with self._lock:
            self.gauges[name] = value

    @typ.override
    def _clear(self) -> None:
        """Clear all collected metrics; called under the store lock."""
        self.counters.clear()
        self.histograms.clear()
        self.gauges.clear()
