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
scores the _base revision's_ copy of the file, and this branch's base already
scores 9.68 because it is a different lineage — the base revision carries the
six-argument `_build_subprocess_execution`, not the branch's seven-argument
one. The delta therefore reports `9.68 -> 10.00`, which is a comparison across
revisions rather than the branch's own 8.54 to 10.00 improvement. The `8.54`
figure is reproducible from the pre-extraction blob, which is byte-identical to
the pushed pre-extraction head (`git show a6751bd9:cuprum/sh.py`).

The local CLI and CodeScene's service disagree by 0.01 in the last digit on
every figure measured for this file: the service prints 8.55 and 9.69 where the
CLI prints 8.54 and 9.68, and both agree on 10.00. Figures quoted in this
addendum are the CLI's, matching the `cs check` command they came from.

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

## Addendum (2026-09-25): `cuprum.sh` becomes a package

`cuprum/sh.py` grew back over the module-size ceiling once Pylint 4.0.9 on PyPy
3.12 started parsing files that PyPy 3.11 had silently skipped. The 2026-09-19
addendum's statement that `cuprum/sh.py` holds "the value types it exchanges"
no longer describes the code. `cuprum.sh` is now a package whose submodules
each own one responsibility:

- `cuprum/sh/argv.py` — argv construction (`build_argv`, `_ArgValue`,
  `_stringify_arg`, `_serialize_kwargs`).
- `cuprum/sh/execution.py` — `ExecutionContext`, `TimeoutExpired`, and
  `StdinInput`.
- `cuprum/sh/results.py` — `CommandResult` and `PipelineResult`.
- `cuprum/sh/output.py` — `RunOutputOptions` and `IOOptions`.
- `cuprum/sh/safe_cmd.py` — `SafeCmd`, `Pipeline`, and `SafeCmdBuilder`.
- `cuprum/sh/factory.py` — the `make()` builder factory.

`cuprum/sh/__init__.py` contains only the package docstring and re-exports. It
exposes every name the former module did, with the same object identity, so
`cuprum.sh.SafeCmd`, `cuprum.sh.CommandResult`, and the rest of the public
surface are unchanged for importers. `cuprum._line_stream` follows the same
package layout. Its `coordinator` submodule holds the run, teardown, and
coordination steps, so tests that replace one of their collaborators patch
`cuprum._line_stream.coordinator`.

## Addendum (2026-09-27): split streaming stdin out of `_subprocess_stdin`

The #445 work made the stdin pipe handle a second kind of source: an async
producer (`StdinStream`) pulled one chunk at a time, as distinct from the
complete payload (`StdinInput`) the module already wrote in one go. The
encoder, the per-chunk helpers, and the source-error construction that came
with it pushed `cuprum/_subprocess_stdin.py` to 451 lines, back over the
400-line module ceiling the 2026-09-14 addendum had cleared.

The seam is the one the module's own docstring already drew.
`cuprum/_subprocess_stdin.py` keeps what the decision outcome above assigns it —
`_emit_stdin_error`, `_write_stdin`, `_close_stdin`, `_cancel_stdin_writer`,
the `cuprum.stdin` logger, and `_spawn_stdin_writer`, which stays the single
entry point both kinds of source are started from.
`cuprum/_subprocess_stdin_stream.py` now owns the producer path:
`_write_stdin_stream`, `_StdinCodec` and `_stdin_codec`, the `_StreamSink`
bundle, `_write_chunk`, `_flush_encoder`, `_finalize_stdin_source`, and the
`_stdin_source_error` / `_source_error` pair that builds the public
`StdinSourceError`.

The dependency runs one way. The streaming module imports the pipe primitives
(`_close_stdin`, `_emit_stdin_error`) from `_subprocess_stdin` at module scope;
the dispatcher in `_subprocess_stdin` imports `_write_stdin_stream` inside the
function body. A module-scope import in both directions would close a cycle at
load time, when neither module is complete, so the deferred import is
deliberate rather than incidental.

Ownership of the public surface is unchanged: `cuprum.sh` still exports
`StdinStream` and `StdinSourceError`, and the type a producer failure raises is
still resolved through `_subprocess_context._sh_module()`. The three spawn call
sites that pass an `ExecutionContext` to `_stdin_codec` —
`cuprum/_subprocess_execution.py`, `cuprum/_subprocess_stream_run.py`, and
`cuprum/_line_stream/spawn.py` — now import it from the new module. No public
API changes, and the module-size suppression remains unnecessary.

## Addendum (2026-09-27): split the spawn binding and the child-exit wait

The same #445 work carried two more branch-introduced overruns. Resolving stdio
and opening the caller's target files grew `cuprum/_subprocess_execution.py` to
523 lines, and the streaming-source and early-close work grew
`cuprum/_subprocess_wait.py` to 438; against `origin/main` the two modules sit
at 363 and 392. Both crossed the repository's 400-line `max-module-lines`
ceiling, whose suppression Option B removed, so two further extractions at
cycle-safe seams brought them to 367 and 327.

`cuprum/_subprocess_spawn.py` now owns everything the parent does around a
spawn without consuming a stream: the mapping of a resolved stdio onto the
value the spawn layer receives (`_output_stdio`, `_stdin_stdio`), the opening
of each cuprum-owned target file immediately before the fork and the closing of
cuprum's copy immediately after (`_open_owned_stdio`, `_close_owned_stdio`),
and the spawn call itself (`_spawn_subprocess`). Its ownership rule is narrow
on purpose: only library-owned resources are closed. A path target opens the
file, hands the descriptor to the child, and closes cuprum's copy in a
`finally` immediately after the spawn returns, whereas a borrowed descriptor or
file object is never closed — the caller's own later use of it is the only
witness that the rule held, since no exit code can distinguish a close cuprum
owed from one it did not. A borrowed file object is still flushed before the
spawn, in `_stdio_plan._resolve_stdio_binding`, so buffered caller-side bytes
reach the child. The mapping is where the two kinds are told apart, and that is
why pipe-ness travels as an explicit `pipes` frozenset: `Popen` treats `PIPE`,
a raw `int`, and a file object differently, and it leaves its own `stdin`/
`stdout`/`stderr` as `None` for everything that is not a pipe, so pipe-ness is
the one piece of the resolution the value cannot express for itself.

`cuprum/_subprocess_deadline.py` now owns the child-exit half of ending a run:
`_wait_for_exit_code`, which awaits the process and terminates it on
cancellation without a timeout of its own, and
`_wait_for_exit_code_within_timeout`, which applies the deadline and translates
expiry into the public timeout surface. The other half — cancelling the stdin
writer and draining the stream consumers exactly once — deliberately stayed in
`cuprum/_subprocess_wait.py`. Nothing in the new module touches the parent's
stream tasks, which is what keeps a reader wedged on a pipe from delaying the
termination that lets it reach EOF. The split is also where the earlier
addenda's division of labour is preserved unchanged: both helpers terminate the
child but never drain, so the caller's single drain through
`_drain_stream_consumers` still reaches EOF, and a non-positive deadline still
expires immediately rather than racing `asyncio.timeout`.

The seam was drawn on the drain's other side for a test-visible reason.
`cuprum/unittests/test_subprocess_drain_logging.py` pins
`_DRAIN_LOGGER = "cuprum._subprocess_wait"`, and `logging.getLogger(__name__)`
resolves to the module that _defines_ the helper, so moving the drain's debug
calls would have broken that interface even though the behaviour would have
been identical. The child-exit half is logger-name-neutral by construction:
`_report_timeout_expiry`, which the deadline module calls for both expiry
routes, takes the `_StageObservation` rather than a logger, so the timeout
records it emits are attributed to the observation the caller supplied and not
to the defining module.

The private import compatibility rule from the 2026-09-16 addendum applies
unchanged: both moved names are re-exported from their original modules, so
`_subprocess_execution` still resolves `_spawn_subprocess` and
`_subprocess_wait` still resolves `_wait_for_exit_code` and
`_wait_for_exit_code_within_timeout`. Direct private imports and monkeypatch
targets therefore resolve the same names as before, and the single-command run,
the line-stream coordinator, and the timeout test modules each keep one import
path. The design and developer guides' §8.1.5 rosters now name both modules,
correcting the earlier statements that placed the wait with the drain and the
spawn with the orchestration module; the 2026-09-27 stdin addendum above named
neither. No public API changes, and the module-size suppression remains
unnecessary.

## Addendum (2026-09-27): the stdin-writer rendezvous

The same #445 work found a third ending, and it belonged to neither module the
previous addendum had just separated. A producer that failed while the child
was still running was reported as a slow child: the writer ran alongside the
exit wait, but nothing raced them, so a producer that died at once was noticed
only when the child's own deadline expired and the caller received
`TimeoutExpired` about a failure already known. Awaiting the child first is
correct as long as a failed producer also stops the child, and it does not —
the writer's teardown closes the pipe, so a child that reads to EOF and then
works, or one that never reads at all, keeps running. That is what a
`head`-like child ignoring its input produces, not a hypothetical shape.

`cuprum/_subprocess_rendezvous.py` now owns that decision.
`_await_exit_or_writer_failure` waits on the exit wait and the stdin writer
together and returns the exit only once it is the sole survivor, so a writer
failure ends the run immediately. The resolution is deliberately narrow: only a
writer that _failed_ is an outcome, and one that merely _finished_ first is the
ordinary way a stream ends — a producer exhausted, the pipe closed, the child
still draining — so the run continues to the child's exit as before. No
termination policy is restated. The exit wait is cancelled, and
`_wait_for_exit_code`'s own cancellation handler already escalates through
`_terminate_all_shielded`.

The module exists because ending a run divides in three. The child's deadline is
`_subprocess_deadline`'s and the parent's task reconciliation is
`_subprocess_wait`'s, but "end because the input source died" is neither the
child's fault nor the parent task set's, so neither is the right place to
notice it. The ceiling made the same demand from the other side: adding the
race to `cuprum/_subprocess_wait.py` took it to 449 lines against the
repository's 400-line `max-module-lines`, whose suppression Option B removed.
Unlike the two overruns recorded above, this one was never committed — the
extraction was made in the same working session that introduced the race — so
the module reads 328 before and 336 after on the branch, re-exporting the moved
name. This was the third pass at the same ceiling in one milestone, and it
confirms the lesson the earlier addendum drew: a milestone adding _any_
behaviour to a module already near the line must budget for the extraction, not
just for the behaviour.

The private import compatibility rule from the 2026-09-16 addendum applies
unchanged: `_subprocess_wait` re-exports `_await_exit_or_writer_failure` from
the new module, and all three call sites — `_subprocess_execution`, the
line-stream coordinator, and `_subprocess_stream_run` — import it from there as
before. The helper takes its exit wait already constructed rather than
resolving it by name, which is what keeps the existing monkeypatch seams
working: `test_line_stream_exit.py` patches
`_wait_for_exit_code_within_timeout` on the coordinator, and
`test_safe_cmd_timeout.py` patches it with process doubles, so each caller must
still resolve that name from its own module namespace for the patch to land.

The design guide's §8.1.5 roster now names the new module, and this addendum is
the statement its `_subprocess_wait.py` entry points at for the race. No public
API changes, and the module-size suppression remains unnecessary.

## Addendum (2026-09-27): two more `cuprum.sh` submodules

The 2026-09-25 addendum's roster above is now incomplete, and one of its
entries is wrong. The same #445 work that split the private subprocess modules
also crossed the ceiling in two public-facing ones, and the splits are
unavoidable rather than discretionary because `max-module-lines` is an enabled
rule rather than a suppressible one.

`cuprum/sh/output.py` reached 584 lines against the 400-line ceiling. The
overrun was branch-introduced: against `origin/main` the module sits at 332
lines and holds no stdio vocabulary at all, because `StdioTarget` is new in
this work. `cuprum/sh/stdio.py` now owns that vocabulary — `StdioTarget`, its
four-variant kind, and the validation policing it (`_validate_stdio_targets`,
the `_reject_*` helpers, and `_share_one_owned_path`). `output.py` keeps
`RunOutputOptions` and `IOOptions` and stands at 363 lines. It re-exports
`StdioTarget` and `_validate_stdio_targets`, so `cuprum.sh.output` remains a
usable import path for both.

`cuprum/sh/safe_cmd.py` is the other overrun, and its shape differs. Against
`origin/main` it sits at 398 lines — two short of the ceiling with no
suppression — so the #445 work crossed the cap by adding 52 lines to a module
already at the line. The extraction took `Pipeline`, which at 138 lines was the
largest cohesive seam available rather than the thing that had grown; the same
class was 138 lines on `origin/main`. `cuprum/sh/pipeline.py` now owns it,
leaving `SafeCmd` and `SafeCmdBuilder` behind, and the two modules reference
each other. That reciprocal reference is why `Pipeline` is bound by a
module-level import at the _bottom_ of `safe_cmd.py` rather than at the top or
inside `__or__`: `SafeCmd.__or__` is annotated `-> "Pipeline"`, and the public
signatures are introspected with `typing.get_type_hints`, which evaluates a
quoted annotation against the defining module's namespace alone. A
function-local import would leave that annotation unresolvable from the first
composition onwards, and a top-of-file import would ask `pipeline` to import a
`SafeCmd` that did not exist yet, closing the cycle at load time.

So the roster above reads correctly except in two places. It omits
`cuprum/sh/stdio.py` and `cuprum/sh/pipeline.py`, and its `cuprum/sh/output.py`
and `cuprum/sh/safe_cmd.py` entries describe the code as it stood before these
splits: `output.py` no longer holds the standard-stream vocabulary, and
`safe_cmd.py` no longer holds `Pipeline`. The corrected roster is:

- `cuprum/sh/argv.py` — argv construction (`build_argv`, `_ArgValue`,
  `_stringify_arg`, `_serialize_kwargs`).
- `cuprum/sh/execution.py` — `ExecutionContext`, `TimeoutExpired`, and
  `StdinInput`.
- `cuprum/sh/results.py` — `CommandResult` and `PipelineResult`.
- `cuprum/sh/output.py` — `RunOutputOptions` and `IOOptions`.
- `cuprum/sh/stdio.py` — `StdioTarget` and the validation policing it.
- `cuprum/sh/safe_cmd.py` — `SafeCmd` and `SafeCmdBuilder`.
- `cuprum/sh/pipeline.py` — `Pipeline`, which the package re-exports.
- `cuprum/sh/factory.py` — the `make()` builder factory.

The public surface is unchanged: `cuprum.sh` still exports `StdioTarget` and
`Pipeline` under the same names with the same object identity, and the wheel
snapshot is regenerated for the two new modules. No public API changes, and the
module-size suppression remains unnecessary.
