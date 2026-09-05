# Architectural decision record (ADR) 010: Rust-pump executor-hop spans

## Status

Accepted on 2026-09-04. Cuprum provides an opt-in, span-shaped observation
registry for Rust-pump executor hops without changing `PumpEvent`, `PumpPhase`,
or `ExecEvent`.

## Date

2026-09-04.

## Context and problem statement

[ADR-008](adr-008-rust-pump-observation-channel.md) deliberately records
Rust-pump routing and cancellation-cleanup facts on a narrow event channel.
Those events can decorate an existing execution span, but cannot model the
executor hop itself: the work begins before `run_in_executor` schedules its
worker and ends only when its done callback has restored descriptor ownership.

Operators need that full duration as an independently selectable trace surface.
It must remain opt-in, must not treat a Rust-pump decline as a hop, and must
not reveal descriptor numbers, command arguments, exception text, tracebacks,
or unbounded identifiers.

## Decision drivers

- Preserve the closed `PumpEvent`, `PumpPhase`, and `ExecEvent` contracts.
- Keep unregistered callers behaviourally unchanged.
- Cover the cancellation drain, while the worker still owns descriptors.
- Expose only stable bounded attributes: operation, buffer size, outcome, and
  successful total bytes.
- Avoid a trace-context bridge across the PyO3 boundary until its cost and
  ownership semantics are independently evaluated.

## Options considered

### Option A: extend `PumpPhase` and `PumpEvent`

Put executor-hop lifecycle state into the existing pump event channel.

This would mix counting-oriented routing facts with span lifetime and increase
the closed event surface documented by ADR-008. It also makes an observer infer
the executor duration from separate notifications rather than giving it the
span its backend expects.

### Option B: a separate span registry

Register `Tracer` instances on a dedicated `ContextVar`. Open one
`cuprum.rust_pump_hop` span per tracer immediately before the executor future
is created, then close it from that future's done callback.

## Decision outcome / proposed direction

Option B. `observe_pump_span` supplies token-restoring registration handles, and
`current_pump_span_tracers` exposes the context-local tuple for inspection.
With no tracer registered, opening returns an empty carrier and changes no
execution behaviour. A tracer failure is reported at `WARNING` with
`cuprum_action="pump_span_observer_failed"`; remaining tracers continue, while
non-`Exception` control-flow signals still propagate.

The span opens only after the Rust fast path has passed its decline checks. Its
callback sets one bounded outcome: `succeeded`, `failed`, `cancelled`, or
`failed_after_cancel`. Only successful spans receive `total_bytes` and status
`ok`. Every carrier span ends in the callback, after the worker has settled and
before descriptor restoration signals completion to the awaiting task.

The Rust-internal `stream_pump` span remains parentless. Passing Python trace
context through PyO3 would add a cross-language lifetime and propagation
contract to a hot executor boundary. This decision does not establish that cost
as acceptable; parentage remains a separately evaluated follow-up.

## Goals and non-goals

### Goals

- Provide one opt-in span per actual Rust-pump executor hop.
- Include the cancellation-drain lifetime and final bounded outcome.
- Keep descriptor and command payloads out of hop-span attributes.

### Non-goals

- Changing `PumpEvent`, `PumpPhase`, `ExecPhase`, or existing hooks.
- Opening spans for fast-path declines.
- Parenting the Rust `stream_pump` span, or crossing trace context through
  PyO3.

## Consequences

### Positive

- Tracing backends can measure a hop without relying on an arbitrary ambient
  execution span.
- Cancellation and worker-failure outcomes remain distinguishable without
  exposing error payloads.

### Negative

- Callers who want hop spans must make a second, explicit registration.
- The Rust and Python spans remain separate until cross-language context has a
  justified cost model.
