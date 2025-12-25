"""Example telemetry adapters for Cuprum's structured execution events.

This package provides reference implementations showing how to integrate
Cuprum's observe hook system with common telemetry backends:

- ``logging_adapter``: Enhanced structured logging via the standard library
- ``metrics_adapter``: Prometheus-style metrics (counters, histograms)
- ``tracing_adapter``: OpenTelemetry-style distributed tracing

All adapters are optional and non-blocking; they do not introduce runtime
dependencies on external telemetry libraries. Instead, they define protocols
and provide reference implementations that can be copied, adapted, or used
as inspiration for project-specific integrations.

Example usage::

    from cuprum import scoped, sh
    from cuprum.adapters.logging_adapter import structured_logging_hook
    from cuprum.adapters.metrics_adapter import MetricsHook, InMemoryMetrics
    from cuprum.adapters.tracing_adapter import TracingHook, InMemoryTracer

    # Structured logging
    with scoped(allowlist=my_allowlist), sh.observe(structured_logging_hook()):
        cmd.run_sync()

    # Metrics collection
    metrics = InMemoryMetrics()
    with scoped(allowlist=my_allowlist), sh.observe(MetricsHook(metrics)):
        cmd.run_sync()

    # Distributed tracing
    tracer = InMemoryTracer()
    with scoped(allowlist=my_allowlist), sh.observe(TracingHook(tracer)):
        cmd.run_sync()

"""

from __future__ import annotations

__all__: list[str] = []
