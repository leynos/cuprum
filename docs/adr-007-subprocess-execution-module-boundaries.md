# Architectural decision record (ADR) 007: Subprocess execution module boundaries

## Status

Accepted on 2026-07-18. Cuprum divides the private subprocess execution
implementation by runner orchestration, stdin handling, timeout handling, and
wait/stream-drain handling.

## Date

2026-07-18.

## Context and problem statement

`cuprum/_subprocess_execution.py` combined subprocess spawning, stdout/stderr
consumer coordination, supplied-stdin lifecycle management, timeout
translation, exit-event accounting, and stream-consumer teardown. The module
exceeded the project's module-size policy and carried a `too-many-lines`
suppression, obscuring the distinct lifecycles that maintainers need to modify
and test.

The split must preserve the private execution contract: `SafeCmd.run()` keeps
the same observable results, cancellation behaviour, timeout translation, and
event emission.

## Decision drivers

- Remove the module-size suppression by creating cohesive modules.
- Give stdin diagnostics and timeout translation clear ownership.
- Keep spawning and stream-consumer orchestration in one coordination module.
- Give process waiting, termination, and stream-drain teardown clear ownership.
- Preserve text output for capturing timeout results even when readers have not
  observed EOF at the first teardown check.
- Preserve existing private interfaces where they remain necessary.
- Make the specialized lifecycles independently testable.

## Options considered

### Option A: retain the combined module and suppression

Keep all execution concerns in `_subprocess_execution.py` and retain the
`too-many-lines` suppression.

This avoids import changes but keeps unrelated lifecycles coupled and leaves a
policy exception in a core implementation module.

### Option B: split by lifecycle concern

Keep runner orchestration, process spawning, and stream-consumer creation in
`_subprocess_execution.py`; move stdin writing and its logger to
`_subprocess_stdin.py`; move timeout translation plus shared exit-event
accounting to `_subprocess_timeout.py`; and move process waiting, termination,
and stream-consumer draining to `_subprocess_wait.py`.

This makes ownership explicit while retaining the runner as the composition
root.

### Option C: move all execution helpers into a generic utilities module

Create a broad helpers module without distinguishing the lifecycle each helper
belongs to.

This reduces the original file size but creates an ambiguous dumping ground and
does not improve ownership.

| Topic              | Combined module      | Lifecycle split | Generic helpers |
| ------------------ | -------------------- | --------------- | --------------- |
| Ownership          | Mixed                | Explicit        | Ambiguous       |
| Module-size policy | Suppression required | Compliant       | Likely to drift |
| Test isolation     | Coupled              | Focused         | Mixed           |
| Spawn coordination | Local                | Local           | Fragmented      |

_Table 1: Trade-offs for organizing private subprocess execution._

## Decision outcome / proposed direction

Choose Option B. `_subprocess_execution` remains the composition root for
spawning and stream-consumer creation/wiring. `_subprocess_stdin` owns
`_emit_stdin_error`, `_write_stdin`, and `_spawn_stdin_writer`, including the
`cuprum.stdin` logger. `_subprocess_timeout` owns timeout details/errors,
timeout translation, and the exit-event helpers shared by timeout and normal
completion paths. `_subprocess_wait` owns the deadline wait, process
termination, and the single stream-consumer drain. Its drain interface uses
`_RunTaskOwnership` to bundle the optional stdin-writer task with the stdout
and stderr consumer tasks, `_DrainContext` to carry capture and observability
settings, and `_reconcile_run_tasks(tasks, context)` to cancel stdin before
settling both consumers as one shielded cleanup unit.

The drain is capture-aware. A capturing drain waits for up to
`_CAPTURE_EOF_GRACE_S` for terminated-process readers to observe EOF, then
cancels anything still pending. It decodes a missing reader result as `""`, so
capturing timeout results always expose text in `.stdout` and `.stderr`.
Non-capturing and cancellation/error cleanup drains skip the grace window and
retain `None` for absent text, keeping those teardown paths prompt and
discarding output as intended.

The runner imports specialized helpers; the specialized modules do not create
subprocesses or expose public command APIs. `_resolve_timeout` remains defined
in `_subprocess_context`, and `cuprum.sh` imports it from that definition site
rather than through a redundant execution-module re-export.

## Goals and non-goals

### Goals

- Create coherent private module boundaries that remove the size suppression.
- Retain existing observable execution and error behaviour.
- Make stdin and timeout paths directly importable for focused tests.

### Non-goals

- Change the public `SafeCmd` or timeout API.
- Change process spawning or cancellation semantics beyond the capture-aware
  timeout-output contract described in this decision.
- Introduce a new public module surface.

## Known risks and limitations

- Private import paths used outside the package may need adjustment because
  specialized helpers now live in their owning modules.
- The modules remain coupled through private execution-context types; that is
  intentional because the runner remains the composition root.

## Consequences

### Positive

- Each lifecycle has an obvious implementation home and focused tests.
- The `too-many-lines` suppression is no longer necessary.
- The stdin logger lives with the stdin behaviour it reports.

### Negative

- Imports span several private modules instead of one.
- Maintainers must preserve the boundaries when adding execution behaviour.

## Addendum (2026-07-28): wait-helper decomposition and timeout observability

Enabling the Ruff `ASYNC` family (`ASYNC109`) prompted a follow-up refinement
of the timeout wait path inside `_subprocess_execution`, preserving the Option
B boundaries above.

- **Caller-owned deadlines.** The deadline is applied with `asyncio.timeout()`
  rather than threaded through a `timeout` parameter (which `ASYNC109` flags).
  `_wait_for_exit_code` awaits the process and terminates it on cancellation
  but no longer takes a timeout; `_wait_for_exit_code_within_timeout` wraps it
  and applies `execution.timeout`.
- **Non-positive fast path.** Because `asyncio.timeout()` only schedules its
  cancellation for the next event-loop iteration, a fast, already-exited
  process could race past a zero or negative deadline. A non-positive timeout
  is therefore special-cased to expire immediately and deterministically,
  preserving the behaviour of the superseded `asyncio.wait_for` implementation.
- **Terminate here, drain once there.** Both wait helpers terminate the process
  but never drain: stream consumers belong to the caller, which drains them
  exactly once through `_drain_stream_consumers`. Terminating first is what
  lets that single drain reach EOF, and draining in one place keeps the timeout
  and cancellation paths from reconciling the same tasks twice.
- **Capture-aware teardown.** The capturing drain gives terminated-process
  readers a bounded EOF grace window before cancellation. A reader that remains
  pending is then cancelled, and its missing result is decoded as an empty
  string. Non-capturing cleanup skips the window and preserves `None` for
  absent text. Consequently, timeout results retain partial output and always
  satisfy the capturing contract without allowing an inherited pipe to wedge
  teardown indefinitely.
- **No-orphan invariant.** Whether a run ends through external cancellation, an
  elapsed deadline, or an immediate non-positive expiry, that single drain
  cancels and drains every still-pending stream-consumer task before the
  exception propagates, so no pending stream-consumer task is ever left behind.
- **Observability.** These paths emit best-effort `timeout` and
  `teardown_error` `ExecEvent` observe events, plus a
  `capture_eof_grace_expired` event when a capturing drain exhausts its fixed
  EOF-grace budget with readers still pending. The latter carries only the
  correlated `exec_id`/`pid`, `operation="drain"`, `eof_grace_s`, and
  `pending_readers`; `MetricsHook` counts it as
  `cuprum_capture_eof_grace_expired_total` with only `program` and `project`
  labels, and `TracingHook` records a matching
  `cuprum.capture_eof_grace_expired` span event. Captured payloads are never
  emitted. Parallel `cuprum.timeout` log diagnostics and all event emission
  remain best-effort and never mask `TimeoutExpired`; unexpected reader
  failures retain the separate `teardown_error` signal.

This refinement changes no public API: `SafeCmd`, `Pipeline`, `TimeoutExpired`,
its payload of partial captured output, and timeout/exception precedence are
all unchanged. The telemetry above is additive new observable behaviour,
emitted best-effort alongside, never in place of, those existing results.
