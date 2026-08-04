# Architectural decision record (ADR) 008: Rust-pump observation channel

## Status

Accepted on 2026-08-04. Cuprum reports Rust-pump routing decisions on a
dedicated observation channel with its own event type, hook registry, and
hook-failure policy, separate from `ExecEvent`.

## Date

2026-08-04.

## Context and problem statement

An inter-stage pipe hop may hand its raw descriptors to the Rust stream pump.
It may also decline to, for one of three reasons, and a hop cancelled while the
pump owns the descriptors may be hiding a worker failure. Both facts are
recorded as `DEBUG` log records carrying `cuprum_action` and, for a decline,
`cuprum_reason`. Neither had a counter beside it, so answering "what fraction
of hops still take the fast path?" meant aggregating debug logs.

The obvious route — a new `ExecPhase` member consumed by the existing
`MetricsHook` — is closed. `ExecPhase` is a closed `Literal` and
`_metric_operations` matches it exhaustively, raising
`_UnhandledMetricsPhaseError` from its `case _` arm. Hook exceptions are not
swallowed: `_emit_exec_event` re-raises, and `_StageObservation.emit` unwraps
and re-raises again, so a raising hook fails the caller's command. Adding a
phase would therefore start raising inside every `MetricsHook` a caller had
already registered, converting a library-side addition into a failure in code
that was correct when it was written.

A second constraint follows from where these events occur. The decline sites
sit inside live stream pumping, on the path that falls back to the Python pump
and completes the hop successfully. The cancellation site sits inside
cancellation unwinding, immediately before a `CancelledError` is re-raised. An
observer that raises at either point would change what the pipeline does.

## Decision drivers

- A caller who does not register the new observer must see no change in
  execution behaviour.
- An already-registered `ExecEvent` consumer must not be affected at all.
- Metric labels must stay bounded; no descriptor values, argument vectors, or
  exception text.
- No library-global metrics backend, and no telemetry-vendor dependency.
- Observer failures must be reported rather than silently discarded.
- The existing `DEBUG` records must survive; counters supplement them.

## Options considered

### Option A: add an `ExecPhase` member and extend `MetricsHook`

Add `rust_pump_declined` and `rust_pump_failed_after_cancel` to `ExecPhase` and
give `_metric_operations` arms for them.

This reuses the whole existing pipeline, but it is the option the problem
statement rules out: consumers that match `ExecPhase` exhaustively — including
Cuprum's own `MetricsHook` before it is updated, and any third-party hook
modelled on it — begin raising on the new values. Because hook exceptions
propagate, that failure reaches the caller's command. It also puts a routing
event on a channel whose events all describe a command lifecycle, and whose
`ExecEvent` fields (`program`, `argv`, `pid`, `exec_id`) are meaningless for a
pipe hop.

### Option B: a module-global counter registry inside Cuprum

Keep an internal counter table that callers read on demand.

This needs no new hook contract, but it makes the library own metrics state
process-wide, which the repository's observability policy forbids: libraries
emit instrumentation, they do not install recorders. It also has no scoping, so
concurrent test runs and nested scopes share one accumulator.

### Option C: a dedicated pump observation channel

A separate `PumpEvent` type, a separate `PumpHook` registry on its own
`ContextVar`, and a separate adapter that reuses the existing
`MetricsCollector` protocol.

This costs a second registration call for a caller who wants both channels, and
a second small public surface to document. It cannot affect `ExecPhase`
consumers, because it never touches `ExecPhase`.

## Decision outcome / proposed direction

Option C. `cuprum.pump_events` defines `PumpEvent`, the closed `PumpPhase`
literal, and the `RustPumpDeclineReason` enum that bounds the `reason` label.
`cuprum.pump_observation` owns the `ContextVar`, the `observe_pump`
registration handle, and emission.
`cuprum.adapters.pump_metrics.PumpMetricsHook` maps events to two counters
against a caller-supplied `MetricsCollector`:

Table 1: counters emitted by `PumpMetricsHook`

| Counter                                      | Labels   | Incremented when                                               |
| -------------------------------------------- | -------- | -------------------------------------------------------------- |
| `cuprum_rust_pump_declined_total`            | `reason` | a hop falls back from the Rust pump to the Python pump         |
| `cuprum_rust_pump_failed_after_cancel_total` | none     | a cancelled hop's Rust worker failure is consumed and recorded |

`reason` takes exactly the three `RustPumpDeclineReason` values, so the series
count is fixed by construction.

Pump hooks live on their own `ContextVar` rather than on `CuprumContext`. That
keeps `ScopeConfig`, `CuprumContext.narrow`, and every consumer that reads the
execution context unchanged, so the channel cannot alter how an existing
caller's commands execute. Registration still follows the repository's
token-restoration discipline.

### Hook-failure policy

The pump channel reports a failing hook at `WARNING` with its traceback under
`cuprum_action="pump_observer_failed"`, then runs the remaining hooks. It does
not re-raise. This diverges from `_emit_exec_event` deliberately, and the
divergence is the point: both emission sites are on paths contracted to
complete, so propagating would let a misconfigured metrics backend abort a pipe
hop that would otherwise have fallen back and succeeded, or displace the
`CancelledError` a caller is owed. The failure is recorded, not swallowed.

Anything that is not an `Exception` — `SystemExit`, `KeyboardInterrupt`,
`asyncio.CancelledError` — propagates untouched, on the same reasoning as
`_await_rust_pump`: a shutdown signal arriving here must still travel.

`PumpMetricsHook` ignores a phase it does not recognize instead of raising on
it, so a future phase cannot recreate inside this channel the breakage that
ruled Option A out.

## Goals and non-goals

### Goals

- Count both pump lifecycle events exactly once each, with bounded labels.
- Leave `ExecPhase` and every registered `ExecEvent` consumer untouched.
- Preserve the existing `DEBUG` records unchanged.

### Non-goals

- Changing Rust-pump transfer semantics, descriptor lifecycle, or fall-back
  behaviour.
- Counting successful hand-offs. There is no pump event for a hop that
  succeeds; success is the absence of a decline.
- Supporting asynchronous pump hooks. Both emission sites are synchronous, and
  one runs during cancellation unwinding, where there is no task to attach an
  awaitable to.

## Known risks and limitations

- A caller who wants both channels must register two hooks. They can share one
  collector, so the backend wiring is not duplicated.
- Hook *registration* is context-local, like every other Cuprum hook: a
  registration made outside the `contextvars.Context` that runs the pipeline
  will not see its events. Metric *scope* is not registration scope. The counts
  live in the caller-supplied `MetricsCollector`, so they are as wide as that
  collector is — a collector shared between contexts, or backed by an exporter
  shared across processes, aggregates across them.
- The two counters carry the `cuprum_` namespace prefix used by every other
  metric this library emits, so dashboards that expected bare
  `rust_pump_declined_total` names must qualify them.

## Consequences

### Positive

- Pump telemetry becomes countable without touching the `ExecEvent` contract.
- The `reason` label domain is published as an enum, so consumers can enumerate
  the series they will see.
- A broken metrics backend can no longer change what a pipeline does.

### Negative

- Two observation channels exist where there was one, and a reader must know
  which events travel on which.
- The hook-failure policy now differs between channels, which is a rule to
  remember. It is documented on both emitters and in the developers' guide.

This supersedes the position recorded in Proposal 3 of
[ADR-002](adr-002-additional-rust-components.md) for these two events only:
their stability and cardinality are now established by a closed enum and a
fixed label set. Rust-side buffer and throughput counters remain out of the
public runtime API.
