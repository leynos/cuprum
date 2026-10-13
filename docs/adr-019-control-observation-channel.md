# Architectural decision record (ADR) 019: Owned-handle control observation channel

## Status

Accepted on 2026-10-13. Cuprum reports what an owned-handle control operation
did to its child on a dedicated observation channel with its own event type,
hook registry, and hook-failure policy, separate from `ExecEvent`, and joins it
to the run's existing execution span.

## Date

2026-10-13.

## Context and problem statement

`RunningCommand` gives a caller explicit control over a child: `signal()` to
deliver a POSIX signal, `terminate()` for the graceful stop, and `kill()` for
the immediate one. Each operation has an outcome an operator needs afterwards —
was the child asked to stop and did it comply, did the handle have to compel
it, or was the request refused because the child was already gone — and none of
that survives in the settled `CommandResult`, which reports how the child
*ended* rather than what was *done* to end it. A `-15` exit code is the same
whether the child took the first `SIGTERM` or needed the forced signal after
its grace window ran out.

The obvious route — a new `ExecPhase` member consumed by the existing
observation hooks — is closed for the same reasons
[ADR-008](adr-008-rust-pump-observation-channel.md) closed it for pump routing.
`ExecPhase` is a closed `Literal` and its consumers match it exhaustively, and
hook exceptions are not swallowed. Adding a member would therefore start
raising inside consumers a caller had already registered, converting a
library-side addition into a failure in code that was correct when it was
written.

A second constraint follows from where these events occur. A control operation
signals a live child, or compels one that outlived its grace window, and it
does so on the path that is concurrently settling the run. An observer that
raises at that point must not replace the refusal the caller is owed, nor
interrupt the reaping the operation is performing.

## Decision drivers

- A caller who does not register the new observer must see no change in
  execution behaviour.
- An already-registered `ExecEvent` consumer must not be affected at all.
- Metric labels must stay bounded; no PID, execution identifier, command
  argument, or free-form error text.
- No library-global metrics backend, and no telemetry-vendor dependency.
- Observer failures must be reported rather than silently discarded.
- A control operation must still be attributed to the run it acted on, so an
  operator reading a trace can place it in that run's story.

## Options considered

### Option A: extend `ExecPhase` and the existing hooks

Put control-operation state into the existing execution event channel.

Rejected for ADR-008's reasons: it widens the closed event surface, and it
makes every existing exhaustive consumer a failure site for a phase it never
asked about.

### Option B: a module-global control-observation registry

Register control hooks in a module-global list rather than a context-local one.

Rejected because it makes two concurrent runs in one process observable to each
other. A control event names the child it acted on, so a hook that can see
another run's operations can attribute one child's stop to the wrong run's
story.

### Option C: a dedicated context-local channel with its own event type

Emit a `ControlEvent` carrying the closed operation and outcome, the measured
duration, and an optional target; register hooks on a `ContextVar`; emit
fail-open.

### Option D: a control span of its own

Give each control operation its own span rather than decorating the run's.

Rejected on the two grounds [ADR-010](adr-010-rust-pump-hop-span.md) raised for
executor-hop spans. A control operation can complete *after* the run has
settled — a `kill()` on an already-exited child, or a stop whose reaping
outlives the run's own span — so a self-owned control span would either outlive
the execution it describes, reporting a duration the run never had, or have to
be parented across two channels to be placed correctly. Neither cost is
justified when the correct parent already exists.

## Decision outcome / proposed direction

Options C and the span *join* rather than a span of its own.
`cuprum.control_events` defines `ControlEvent`, `ControlOperation`,
`ControlOutcome`, and the `ControlHook` protocol; `cuprum.control_observation`
supplies `observe_control` and its `ContextVar`-backed registration handle.
With no hook registered, starting a measurement returns `None` immediately, so
an unobserved run pays no clock read and allocates nothing.

Emission is deliberately fail-open, matching ADR-008's hook-failure policy. A
hook that raises is reported at `WARNING` with
`cuprum_action="control_observer_failed"` and the remaining hooks continue; a
hook that returns a value rather than `None` is reported the same way, and an
accidental coroutine is closed so an incorrectly declared async hook cannot
leak a never-awaited warning. Neither is allowed to replace the control
operation's own error or interrupt the child cleanup the operation is
performing.

A control operation is part of its run's story rather than a run of its own, so
it is recorded as a **span event** on the execution span the run's
`TracingHook` has already opened, keyed by the run's `exec_id`.
`TracingHook.record_event_on(exec_id, name, attributes)` is that seam, and
`record_control_events(hook)` is the join that registers a recorder against it.
The seam takes a correlation token rather than an event because control events
have no phase to match on; the token is what every channel in this library
correlates by. An event for a token with no open span is dropped rather than
starting one, because a span outliving the execution it describes would report
a duration the run never had.

Two registrations are needed and both are explicit: a caller who wants control
events traced registers the tracing hook *and* the join.

Bounded metrics are projected by `cuprum.adapters.control_metrics`.
`cuprum_control_operations_total` and
`cuprum_control_operation_duration_seconds` carry exactly the closed
`operation` and `outcome` labels; `cuprum_control_refusals_total` carries only
`operation`, so a signal the handle never delivered is countable on its own.
The label set is re-validated against the enums at emission rather than trusted
from the annotation, because `ControlEvent` is dataclass-constructible with any
string in either field. PID is the one field that reaches the event and the
span event but never a metric label: its domain is unbounded, and a recycled
PID would fold two unrelated children into one series.

Refusal is a first-class outcome rather than an inference from a missing
delivery. `_TerminationOutcome` therefore carries a fourth fact, `refused`,
which the stop helpers in `cuprum/_process_lifecycle.py` never set: they cannot
tell a delivery the platform would not make from a child that had already gone,
because both reach them as the same `ProcessLookupError`. Only the child's own
status separates the two, and the handle reads that. One caller that can weigh
them against each other rewrites it.

## Goals and non-goals

### Goals

- Report one bounded event per completed control operation, naming the run it
  acted on.
- Distinguish a delivered stop from an escalated one and from both refusals.
- Keep PID, execution identifier, command arguments, and error text out of
  metric labels.
- Leave an unregistered caller behaviourally unchanged, and pay nothing for an
  unobserved run.

### Non-goals

- Changing `ExecEvent`, `ExecPhase`, or any existing hook.
- Opening a span for a control operation, or parent-posting one onto the
  execution span by a second channel.
- Replacing the existing `DEBUG` records or the `settled` event's description
  of how a run ended.
- Installing a global metrics backend or taking a telemetry-vendor dependency.

## Consequences

### Positive

- A stopped child can be attributed to the operation that stopped it, which the
  exit code alone cannot express.
- Control telemetry cannot disturb the operation it observes, so the guarantee
  that a stop reaches the child does not depend on any observer being correct.
- The label set is fixed by two enums rather than by how many children a
  process happens to control.

### Negative

- Callers who want control events traced or counted must make an additional,
  explicit registration.
- `_TerminationOutcome` carries a fourth field that only one caller sets, which
  is a deliberate asymmetry in exchange for keeping the stop helpers free of
  any dependency on the control vocabulary.
