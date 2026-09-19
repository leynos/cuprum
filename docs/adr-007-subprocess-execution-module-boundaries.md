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
- Preserve existing private import compatibility where it remains necessary.
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

The original proposal exposed the drain helpers through `_subprocess_drain.py`
as a narrow compatibility boundary for focused tests and private imports rather
than a second live implementation. The 2026-08-30 addendum records that this
former boundary was removed. The 2026-09-16 addendum records that
single-command stream-consumer construction later moved to
`cuprum/_subprocess_streams.py`, while the execution module kept the
composition-root role.

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
spawning and stream-consumer wiring: it decides which streams a run consumes,
and it calls `_build_stream_config` and `_spawn_stream_consumers`, whose
single-command construction now lives in `cuprum/_subprocess_streams.py` (see
the 2026-09-16 addendum). `_subprocess_stdin` owns `_emit_stdin_error`,
`_write_stdin`, and `_spawn_stdin_writer`, including the `cuprum.stdin` logger.
`_subprocess_timeout` owns timeout details/errors, timeout translation, and the
exit-event helpers shared by timeout and normal completion paths.
`_subprocess_wait` owns the deadline wait, process termination, and remains the
single owner of stream-consumer draining. The formerly proposed
`_subprocess_drain` compatibility boundary was removed by the 2026-08-30
addendum.

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

## Addendum (2026-09-19): split single-command orchestration out of cuprum/sh.py

CodeScene reported a `Low Cohesion` finding on `cuprum/sh.py`. The file carried
at least four distinct responsibilities across its 31 functions, crossing
CodeScene's LCOM4 threshold of 4. Extraction was the remedy that worked for
this shape: CodeScene's code health for the file moved from 8.54 to 10.00.

Both figures are `cs check` scores of `cuprum/sh.py`, the first taken at the
pre-extraction tip of this branch and the second after the extraction. Do not
expect `cs delta` against the branch's base to reproduce them: `cs delta`
scores the _base revision's_ copy of the file, and `main`'s copy already scores
9.68 because it is a different lineage — the base revision carries the
six-argument `_build_subprocess_execution`, not the branch's seven-argument
one. The delta therefore reports `9.68 -> 10.00`, which is a comparison across
revisions rather than the branch's own 8.54 to 10.00 improvement. The `8.54`
figure is reproducible from the pre-extraction blob, which is byte-identical to
the pushed pre-extraction head (`git show a6751bd9:cuprum/sh.py`).

The single-command orchestration therefore moved to
`cuprum/_command_internals.py`: `_ExecutionTracking`,
`_prepare_execution_observation`, `_build_subprocess_execution`,
`_execute_with_hooks`, and `_run_prepared_command`. That cluster is the whole
of what a single command's execution owes — preparing one validated command's
observation, bundling everything the run needs before it spawns, driving that
bundle through the subprocess layer with after-hook dispatch, and finalizing
the run's presentation-sink session on every terminal path. What stays in
`cuprum/sh.py` is the public command surface, the value types it exchanges, and
pipeline orchestration.

That is why the split is a real seam rather than a size fix: it is the same
seam this ADR already draws, with the orchestration a run owes living in a
private module while the public surface, and the names callers import, stay in
`cuprum.sh`. Pipeline orchestration is the corresponding concern of
`cuprum/_pipeline_internals.py`, which already exists and stays as it is. The
two modules now mirror each other: one module per execution shape.

The private import compatibility rule from the 2026-09-16 addendum applies
unchanged: importers of `cuprum.sh` continue to resolve the public surface
without change, but a test that replaces one of the moved private helpers must
now target `cuprum._command_internals`, the module that resolves it.
`cuprum/unittests/test_stage_observation_builder.py` was the only such test,
and it was re-pointed to `cuprum._command_internals`.

`SafeCmd.run` and `SafeCmd.run_sync` keep their public signatures, and
allowlist ordering, stdin resolution timing, timeout precedence, plan-event
timing, before-hook timing, and the capture, echo, exit-code, cancellation, and
result semantics are all unchanged. The relocation is a behavioural no-op.
Unlike the 2026-09-14 addendum, it was not driven by the repository's
`max-module-lines` ceiling — a cohesion finding prompted it — although it also
reduces `cuprum/sh.py` by the moved cluster as a side effect.

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

## Addendum (2026-08-30): consolidate the drain compatibility boundary

The focused drain tests no longer require a second compatibility module.
`_subprocess_drain.py` was removed, leaving `_subprocess_wait.py` as the single
owner of the drain implementation and its private imports. The wait boundary
uses `_RunTaskOwnership` to bundle the optional stdin-writer task with the
stdout and stderr consumer tasks, `_DrainContext` to carry capture and
observability settings, and `_reconcile_run_tasks(tasks, context)` to cancel
stdin before settling both consumers as one shielded cleanup unit.

Cancellation preserves the capture-aware teardown contract: capturing drains
allow terminated-process readers the bounded EOF-grace window before settling
them, while non-capturing cleanup settles promptly without that window and
discards output. Cancellation during capture grace still settles the consumers
before propagating, so process cleanup cannot leave stream readers pending.

## Addendum (2026-09-15): streamed relay diagnostics ownership

The per-command echo fallback diagnostics introduced a small refinement to the
Option B boundaries while preserving the public execution contract.

- `cuprum/_subprocess_stream_run.py` owns streamed single-command execution.
  `_StreamConsumerSpawnContext` passes the stream configuration, process
  identifier, and per-stream `_RelayDiagnostics` collectors to the consumers.
  `_RunTaskOwnership` retains those consumers and collectors until the single
  success or teardown reconciliation point, so concurrent and nested runs do
  not share result state.
- `_process_lifecycle.py` uses `_SpawnedPipelineStages` while starting a
  pipeline. It retains each process, capture task, start timestamp, and the
  per-stage stderr/final-stage-stdout collector pair. The pipeline collection
  path settles and reads those collectors after output tasks settle, and the
  result builder assigns each stage only the records owned by its streams.
- `RelayFallback` is the result vocabulary: a frozen two-field record holding
  only `EchoStream` and `EchoErrorCategory` categorical values. A successful
  `CommandResult` exposes its records through the trailing defaulted
  `relay_fallbacks` field, in stdout-then-stderr order. A pipeline preserves
  stage order, with final-stage stdout records followed by that stage's stderr
  records; intermediate stages have no result stdout stream.
- A handled text-sink `UnicodeEncodeError` is a first-failure transition for
  one drain. It disables later echo writes, preserves capture, emits the
  existing `EchoEvent`, and appends one `RelayFallback`. The warning, event,
  and record use closed categorical values only; rejected payloads, sink
  metadata, exception data, and command arguments remain outside every
  reporting surface.
- Timeout and cancellation do not produce a `CommandResult`, so no
  `relay_fallbacks` tuple is surfaced on those paths. Reconciliation still
  settles the owned stream tasks, and an `EchoEvent` emitted before teardown
  remains observable through `observe_echo`.

This keeps execution ownership explicit: collectors are caller-owned state
handed into drains, not global observation state, while the existing
`observe_echo` channel remains the event projection for consumers that need
transition timing.

## Addendum (2026-09-16): split single-command stream-consumer construction

The idle heartbeat's option contract added two fields to the single-command
stream configs (`read_size`, and the `mirror` cursor) and grew the code that
assembles them. Carrying that growth inline pushed
`cuprum/_subprocess_execution.py` past the repository's `max-module-lines`
ceiling, which `make lint` does enforce per module.

The stream-consumer construction was therefore moved to
`cuprum/_subprocess_streams.py`: `_build_stream_config` (the stdout config),
`_spawn_stream_consumers` (the stderr config derived from it, and the pair of
consumer tasks), and `_create_stream_callback`. The names stay importable from
`_subprocess_execution`, so direct private imports remain compatible. Tests
that patch implementation dependencies must target `_subprocess_streams`:
patching the re-export does not replace what `_spawn_stream_consumers`
resolves. This is the single-command counterpart of the existing
`cuprum/_pipeline_stage_streams.py`.

The decision above originally assigned stream-consumer creation to
`_subprocess_execution`, and the preceding addendum records a differently
shaped boundary (`_subprocess_drain.py`) that was withdrawn. This split is
accepted on the same test that withdrew that one: it is a real seam, not a
compatibility shim. The module it feeds keeps the decisions — which streams are
consumed, whether stdin is written, and what the result is — rather than
delegating them. The execution module is still the composition root; only the
construction it calls moved. The earlier withdrawal does not apply to this
shape, and the boundary documented in §8.1.5 of the design and developer guides
now names it.

Line observation composes into the same seam: `_create_stream_callback` chains
the caller's `on_line` ahead of the observe-hook emission through
`cuprum._line_callbacks._compose_line_callbacks`, so `SafeCmd.run()` and
`SafeCmd.lines()` share one composition point. The line-iteration path is the
second importer of the builder, reaching `_build_stream_config` and
`_spawn_stream_consumers` from `cuprum._line_stream` at the definition site
rather than through the execution module's re-export, matching the
`_resolve_timeout` precedent above.

### Reconciliation with the 2026-09-15 addendum

Both extractions stand. The streamed run loop lives in
`cuprum/_subprocess_stream_run.py` (2026-09-15) and the consumer construction
it drives lives in `cuprum/_subprocess_streams.py` (2026-09-16), so
`cuprum/_subprocess_execution.py` is a composition root that re-exports both
for import compatibility. The patch-target rule above covers both modules: a
test that replaces an implementation dependency must target the module that
resolves it, which for the spawn context and consumer construction is
`cuprum._subprocess_streams`.

## Addendum (2026-09-19): split pipeline startup from pipeline termination

Merging the idle-heartbeat work with the relay-diagnostics work put two
independent additions into `cuprum/_process_lifecycle.py` at once and carried
the module past the repository's `max-module-lines` ceiling. The module had
accumulated two lifecycles with no shared state: _starting_ a pipeline, and
_terminating_ processes.

Pipeline startup therefore moved to `cuprum/_pipeline_spawn.py`:
`_spawn_pipeline_processes`, the `_SpawnedPipelineStages` accumulator it fills
through `_spawn_pipeline_stages`, `_build_spawn_observations`, and
`_cleanup_spawned_processes`. That last helper is why the split is a real seam
rather than a size fix: it exists only to tear down a _partial_ spawn — the
processes and capture tasks that a failed stage left running — and it is
reachable only from the startup path that can fail that way. Pipeline teardown
after a successful spawn is a different subject, decided by
`cuprum._pipeline_wait` and executed by `_terminate_timed_out_stages`,
`_terminate_pipeline_remaining_stages`, and `_cleanup_pipeline_on_error`, all
of which stay in `_process_lifecycle` next to `_shielded_cleanup`.

`_merge_env` also stays in `_process_lifecycle`: both the single-command and
pipeline spawn paths call it, so it belongs to neither side exclusively.
`_pipeline_spawn` imports it, along with `_terminate_all_shielded`, from its
previous definition site. The private import compatibility rule from the
2026-09-16 addendum applies unchanged: importers of `cuprum._process_lifecycle`
and `cuprum._pipeline_internals` continue to resolve
`_spawn_pipeline_processes` without change, but a test that replaces it must
target `cuprum._pipeline_spawn`, the module that now resolves it.

## Addendum (2026-09-14): stream-wiring split for the module-size ceiling

Routing mirrored output through an opt-in presentation-sink session (see
[ADR-013](adr-013-opt-in-github-actions-presentation-sink.md)) added sink
resolution to `_subprocess_execution`, which pushed that module back over the
400-line `max-module-lines` ceiling whose suppression Option B removed. The
wiring half moves to a new cohesive module rather than reintroducing an
exception:

- `cuprum/_subprocess_streams.py` owns destination selection
  (`_resolve_stream_sink`), the per-line observability callback
  (`_create_stream_callback`), the stdout `_StreamConfig`
  (`_build_stream_config`), and consumer-task creation
  (`_spawn_stream_consumers`).
- `_subprocess_execution` remains the composition root: it invokes that wiring
  for each run and re-exports `_build_stream_config`,
  `_create_stream_callback`, and `_spawn_stream_consumers`, so existing imports
  and the tests that monkeypatch them by module path keep resolving unchanged.
  `_resolve_stream_sink` is not re-exported; it lives only in
  `cuprum/_subprocess_streams.py`. Because `_spawn_stream_consumers` resolves
  `_consume_stream` from that module's own globals, tests must patch
  `cuprum._subprocess_streams._consume_stream`, as
  `cuprum/unittests/test_capture_eof_grace_observability.py` and
  `cuprum/unittests/test_timeout_capture_contract.py` do, rather than an
  `_subprocess_execution` binding.

No public API changes, and the module-size suppression is still unnecessary.
`_subprocess_wait` continues to own teardown through the unchanged drain
interface.
