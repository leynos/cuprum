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

> **Superseded by the 2026-10-03 addendum.** The removal recorded here is of the
> _earlier_ `_subprocess_drain.py`, the withdrawn compatibility boundary. The
> name was reinstated later for a different seam, so `_subprocess_wait.py` is no
> longer the single owner of the drain implementation. Read that addendum before
> treating this section as the current module layout.

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

## Addendum (2026-10-01): byte-exact runs share the execution seams

Byte-exact runs (issue #444) are not a second execution path.
`SafeCmd.run_bytes()` and `Pipeline.run_bytes()`, with their `run_bytes_sync()`
counterparts, reuse the existing runners and carry one extra fact —
`capture_bytes` — from the resolved execution state through the stream
configuration to the drains. The reusable parts of that refactor live in
separate modules:

- `cuprum/_result_assembly.py` — the rules a finished execution applies when
  reporting its result: `_RunMeasurements`, which carries the measured fields
  both result classes declare identically, and the `_require_bytes`/
  `_require_text` narrowing pair that re-establishes each class's declared
  payload type. Single-command and per-stage pipeline construction both read
  these rules from here, so a stage and a direct run of the same command differ
  only in what could actually be measured. `_pipeline_finalize.py` owns the
  completed-pipeline teardown ordering for the same reason, keeping
  `_pipeline_internals` on the spawn half of a run.
- `cuprum/_bytes_run.py` — the decisions the two byte-exact entry points owe
  regardless of which is called: `_validate_bytes_output` rejects the one
  combination bytes mode does not offer (a caller-supplied line observer)
  before anything is spawned, and the `_require_command_result`/
  `_require_pipeline_result` helpers re-take the narrower class a text-mode
  entry point promised. The refusal is policy, not capability: the drain serves
  byte-exact capture and decoded-line observation at once, so a registered
  `sh.observe()` hook stays supported.
- `cuprum/_result_types.py` — the unions the widened internal seam speaks
  (`_AnyCommandResult`, `_AnyPipelineResult`). It imports nothing from the
  assembly rules because `cuprum.context` publishes hook signatures that accept
  either result class, and the assembly rules reach back into `cuprum.context`,
  so the unions have to be nameable without closing that cycle.

The widening is deliberately confined to the internal seam. Each public
boundary re-takes the narrower class it promised, and `_require_bytes`/
`_require_text` raise `_ExecutionInvariantError` on a mode/type contradiction
rather than reporting a replacement character as the child's output. No
existing result class, and no `TimeoutExpired`, is subclassed or modified;
`BytesCommandResult` and `BytesPipelineResult` are separate frozen types.

### Observation and echo render a view, never the capture

Keeping both channels open costs one rule, and the rule is not optional: in a
byte-exact run, every decode that renders a _view_ of the child's bytes
replaces undecodable input, so it can never end the run it observes. That is
`OBSERVER_ERROR_POLICY` in `cuprum/_constants.py`, and it governs the byte-mode
line observer's decoder and both byte-mode echo decoders. The rule is scoped to
byte mode by `cuprum/_streams._StreamConfig.view_errors`: a text run's views
decode under `ExecutionContext.errors`, in step with its capture, exactly as
they did before byte mode existed. `ExecutionContext.errors` governs the
capture alone in a byte run, which decodes its own untouched buffer.

The scope is load-bearing rather than stylistic. Reading `config.errors` in a
byte run's view decoder would let a hook the caller never registered decide the
run's fate: under `errors="strict"` the observer's `UnicodeDecodeError` escapes
the drain's read loop and kills a `run_bytes()` that had already captured the
child's bytes, so the guarantee above — that a registered `sh.observe()` hook
stays supported — would hold only for the default error policy. The capture
still enforces a strict policy, because a text run that asked to reject
undecodable bytes keeps rejecting them; the byte-exact mode simply never
decodes its capture at all.

This is the same division the mode already relies on elsewhere: a renderer's
job is to show, and the capture's job is to report. The bounded-echo path needs
no equivalent change because
`cuprum/_echo_truncation._validate_bounded_echo_encoding` restricts it to
ASCII-compatible stateless codecs, and every encode on that path carries
ASCII-only marker text — a strict policy there has nothing to refuse.

### Guard against shadowed sibling definitions

Moving these seams left one hazard the refactor's own tools could not see. The
module split landed a second copy of `_build_stream_config` in
`cuprum/_subprocess_streams.py`, and every gate passed: by the time the
duplicate was noticed, the only definition reachable was the later one, and the
earlier body had been unreachable since the commit that added it.

Neither linter the project runs reports it, on exactly the nouns this codebase
uses. Ruff 0.16.4's F811 and Pylint 4.0.9's E0102 both decline to report a
redefinition of an underscore-prefixed name, and private helpers here are
underscore-prefixed by convention. Upstream Pyflakes _does_ report it
(`redefinition of unused '_foo' from line 1`), so this is a deviation in the
two linters actually installed rather than a defensible reading of the rule.

`cuprum/unittests/test_no_duplicate_module_definitions.py` closes the gap by
parsing each scanned module and reporting a name bound twice within one
statement list. The unit of the check is the statement list rather than the
module, which is what keeps the branch idiom out of the findings without
special-casing it: the two arms of an `if`/`else` are separate lists, so a
platform-specific `_probe` defined once per arm stays legal, while two
definitions in one arm — or two methods in one class body — are reported. The
rule is deliberately structural and said so in its own docstring, so a future
reader can tell which redefinitions it refuses and which it permits.

## Addendum (2026-10-03): reinstate `_subprocess_drain` on a real seam

The 2026-08-30 addendum read as though `_subprocess_drain.py` had been removed
for good. In fact, it records the removal of the _earlier_ module of that name
— the withdrawn compatibility boundary, which duplicated the drain helpers
behind a second import path. The name returns here for a different reason and a
different seam.

> **Superseded by the 2026-10-14 addendum.** The module described here never
> reached the target branch: a concurrent change had already split the same
> drain helpers onto `cuprum/_stream_drain.py` under its own name, and that
> module is the one the merged tree carries. Read the 2026-10-14 addendum for
> the layout that actually landed; the reasoning below still explains why the
> drain does not belong to a compatibility boundary.

Carrying opt-in process-group ownership through teardown pushed
`_subprocess_wait.py` past the 400-line module ceiling. The split is by
responsibility rather than by line count: `_subprocess_wait` keeps the wait
half of ending a run — applying the deadline and terminating the process — while
`_subprocess_drain.py` owns what happens to the tasks a run leaves behind:
`_drain_stream_consumers`, `_cancel_pending_consumers`, `_reconcile_run_tasks`,
`_await_capture_eof_grace`, `_settle_consumers`, and their supporting types
`_RunTaskOwnership`, `_DrainContext`, and `_EofGraceWaiter`. No compatibility
re-export is left on the old path, matching the `_pipeline_spawn` and
`_subprocess_streams` splits: importers name the module that defines what they
use.

The capture-aware teardown contract is unchanged by the move. A capturing drain
still gives terminated-process readers the bounded `_CAPTURE_EOF_GRACE_S`
window before settling them, non-capturing cleanup still settles promptly and
discards output, and cancellation during capture grace still settles the
consumers before propagating. Reconciliation remains one shielded unit.

No public API changes, and the module-size suppression is still unnecessary.

## Addendum (2026-10-11): typed `sh.make` argument contract

The 2026-09-25 addendum's inventory is historical for two entries.
`cuprum/sh/argv.py` no longer owns the private `_ArgValue`; it owns `ArgValue`,
the public alias naming the domain a builder accepts
(`str | int | float | bool | pathlib.Path | os.PathLike[str]`).
`SafeCmdBuilder` has moved out of `cuprum/sh/safe_cmd.py` into
`cuprum/sh/builder.py`, where it is a `typing.Protocol` describing the callable
`sh.make` returns. `SafeCmd` and `Pipeline` remain in `cuprum/sh/safe_cmd.py`,
and the rest of that addendum still stands.

Publishing the alias is what lets callers annotate their own wrappers with the
same domain the builders enforce, without repeating the union or reaching for
`object`. The contract itself, including which values are rejected at run time,
is recorded in [ADR-020](adr-020-typed-argument-contract-for-sh-make.md).

## Addendum (2026-10-11): the pipeline wait state split from its orchestration

The `_PipelineWaitState` command/query seam was drawn across two modules rather
than kept inside one. `cuprum/_pipeline_wait_state.py` now owns the pure
transition — `record_completion`, `should_terminate_others`, and the
`from_processes` constructor — while `cuprum/_pipeline_wait.py` keeps the
asyncio orchestration around it (`_wait_for_pipeline`,
`_process_completed_task`, and the `asyncio.wait` batch tie-break).

Two constraints met on the same line, which is why the split is drawn there and
not elsewhere. The module-size ceiling forced a decomposition:
`_pipeline_wait.py` stood at 415 lines against the repository's 400-line
`max-module-lines` limit. The command/query seam was already the natural
boundary, because the transition has different readers and different
verification needs from the orchestration — it has no I/O, no signals, and no
clock of its own, so Hypothesis and CrossHair can drive it directly where the
asyncio machinery around it cannot be cheaply exercised by those tools.

The compatibility rule is about the import target, not the definition.
`_PipelineWaitState` is imported into `cuprum/_pipeline_wait.py` from the state
module, so existing importers — `cuprum/_pipeline_wait_records.py` and the
symbolic, state-machine, async, batch, examples, and process-exit test modules
— continue to reach it through `cuprum._pipeline_wait` unchanged. That name is
retained for callers written before the split; new code imports from
`cuprum._pipeline_wait_state` directly, and re-exporting through the
orchestration module is not the preferred entry point going forward.

## Addendum (2026-10-12): `Pipeline` moves to its own module

The 2026-10-11 addendum closed with "`SafeCmd` and `Pipeline` remain in
`cuprum/sh/safe_cmd.py`"; that entry was accurate when it landed and is
historical for the `Pipeline` half. The byte-exact work split the two execution
primitives into one module each, so `Pipeline` now lives in
`cuprum/sh/pipeline.py`.

That brings the addendum's own rule to `Pipeline`: `cuprum/sh/safe_cmd.py` holds
`SafeCmd` and `cuprum/sh/pipeline.py` holds `Pipeline`, so each execution
primitive owns a module rather than sharing one. Each module imports the other
— a command composes into a pipeline through `SafeCmd.__or__`, which returns
`Pipeline.concat`, and a pipeline is built from commands — so each defers the
import it needs to the foot of its own file, after the class it defines exists:
`cuprum/sh/safe_cmd.py` reaches for `Pipeline` there, and
`cuprum/sh/pipeline.py` reaches for `SafeCmd`. The annotations are quoted
strings, but `typing.get_type_hints` resolves them from module globals, so the
deferred name is a genuine runtime import rather than a `TYPE_CHECKING` one.
The 2026-10-11 addendum's `SafeCmdBuilder` half still stands.

`cuprum/sh/__init__.py` continues to re-export both classes unchanged, so
`cuprum.sh.Pipeline` is the same object as `cuprum.sh.pipeline.Pipeline` and
importers of the facade see no difference.

## Addendum (2026-10-11): split the spawn seam into `cuprum/_subprocess_spawn.py`

`cuprum/_subprocess_execution.py` had grown past the repository's 400-line
module ceiling. Rather than compress the comments that explain the timing
invariants, the spawn step moved into its own module, taking with it the three
names that describe it: `_SubprocessExecution`, the bundle that decides how a
child is wired; `_spawn_subprocess`, the seam that creates it; and
`_start_owned_process`, which spawns and announces a child for the direct run.

Announcing is not owned by this module. `_start_owned_process` is the only
place _it_ announces a child, and the direct run is its only caller. The other
two entry points call `_spawn_subprocess` directly and emit their own `start`
event, because each needs the process in hand before announcing it: the
line-stream coordinator to attach its consumers, and the owned-handle start to
attach the tasks that will drive and settle the run. That deviation is
deliberate rather than an oversight of the split — the seam is shared, the
announcement is not.

These belong together because the spawn is what the bundle describes. A test
that needs to refuse a spawn patches `_spawn_subprocess`, and keeping the
bundle in the same module means such a test never has to reach across a module
boundary to build the value it refuses to spawn with.

`_subprocess_execution` re-exports `_SubprocessExecution` and the stream-wiring
names it no longer defines, so importers keep working. It does not re-export
`_spawn_subprocess`: `_start_owned_process` resolves that name from
`_subprocess_spawn`'s globals, so a re-exported copy would be a name no call
path reads. Patching it there would rebind the copy and leave the real spawn
running — a test that still passes while exercising nothing. A test that
refuses a spawn therefore patches the module whose globals its entry point
resolves the name from: `cuprum._subprocess_spawn` for `SafeCmd.run()`,
`cuprum._line_stream.coordinator` for `SafeCmd.lines()`, and
`cuprum._process_handle.tasks` for `SafeCmd.start()`. Each binds the seam into
its own globals, so patching the defining module reaches none of the importers,
and one patch per entry point is the rule rather than one patch for all of them.

The module boundary is by responsibility, not by call depth:
`_subprocess_spawn` creates a child and announces it; `_subprocess_execution`
samples the clocks, waits, and assembles the result. The one invariant the
split must not disturb is the pre-spawn sampling order, which is why
`_start_owned_process` still takes `started_at` as an argument instead of
reading a clock itself — the callers keep owning _when_ their reading is taken.

## Addendum (2026-10-13): `SafeCmd.start` moves to `cuprum/_process_handle/owned_start.py`

The spawn-seam split above was prepared for `SafeCmd.start()`, and once the
byte-exact work landed its siblings the `SafeCmd` module was one method away
from the repository's 400-line ceiling. `start` moved to
`cuprum/_process_handle/owned_start.py`, where `_OwnedStart` contributes it to
the class by inheritance rather than by delegation, so `SafeCmd.start` is still
an ordinary bound method and `typing.get_type_hints` still resolves its
`-> RunningCommand` annotation — from the new module's globals, which is where
the class that annotation names is imported.

The move is by responsibility rather than by size. `start` resolves a different
set of run inputs from `run()`: capture is forced on, because a handle exists
so its caller can inspect the child after the fact, and an explicit deadline
becomes the default for every `wait()` on the handle rather than a limit on the
child's life. Keeping that resolution in `_process_handle` means the package
that owns the handle owns the whole answer to "what does a handle start from?"
instead of borrowing half of it from the command module.

`_OwnedStart` is deliberately not a dataclass, and declares `__slots__ = ()`.
`dataclasses.dataclass` refuses a base that already declares `__slots__`, and
an un-slotted mixin would donate a `__dict__` to the slotted dataclass that
inherits it, so the empty declaration is what keeps `SafeCmd` a slotted
dataclass with the field set its callers already rely on.

## Addendum (2026-10-14): the drain lands in `_stream_drain.py`

Two changes drew the same boundary at the same place while neither could see
the other, and the merged tree keeps the one whose module already existed. The
branch proposed reinstating `cuprum/_subprocess_drain.py` (2026-10-03
addendum); the target branch had already moved the same helpers to
`cuprum/_stream_drain.py` under the byte-exact work (2026-10-01 addendum). Both
splits are by responsibility and both name the same responsibility, so the
surviving module is the pre-existing one and `cuprum/_subprocess_drain.py` does
not exist in this tree.

`cuprum/_stream_drain.py` owns the consumer-side half of ending a run —
`_CAPTURE_EOF_GRACE_S`, `_drain_stream_consumers`, `_await_capture_eof_grace`,
`_await_eof_grace`, `_settle_consumers`, `_cancel_pending_consumers`,
`_decode_consumer_result`, and `_report_drain_failures`. Its three rules are
about _finishing_ rather than reading: what an absent reader reports, how a
capturing drain's bounded grace behaves when it expires, and how a reader
failure is reported without replacing the error that ended the run.

The 2026-10-03 addendum's own reasoning survives the rescoping. It argued the
drain must not return to a mere compatibility boundary, and that the ceiling on
`_subprocess_wait.py` — not a preference for a particular filename — is what
forces the split. Both hold: the confinement is inward from `cuprum/_streams`,
which owns the reading machinery the drain drives, and neither module
re-exports the other's names.

The wait half is what `cuprum/_subprocess_wait.py` keeps, and it kept more than
the 2026-08-30 addendum expected. `_wait_for_exit_code`,
`_wait_for_exit_code_within_timeout`, `_deadline_for_collection`,
`_drain_context`, and `_reconcile_run_tasks` all define there.
`_deadline_for_collection` and `_drain_context` are new since the 2026-08-30
addendum and are placed beside `_reconcile_run_tasks`, its only consumer,
because the context exists to serve that one reconciliation. The two supporting
types live in `cuprum/_subprocess_wait_types.py`, which the 2026-08-30 addendum
already establishes as the shared home for `_RunTaskOwnership`,
`_DrainContext`, and `_EofGraceWaiter`.

The import path is what the split changes, and it does so deliberately rather
than by compatibility shim. `cuprum/_subprocess_wait.py` imports
`_CAPTURE_EOF_GRACE_S`, `_await_eof_grace`, and `_drain_stream_consumers` from
`cuprum/_stream_drain.py` and keeps them bound in its own namespace, because
callers still import them by that path — but importing from there is supported
while _patching_ there is not. The drain resolves each name from its own
module's globals, so rebinding the copy in `_subprocess_wait` is invisible to
it. A test that needs to replace the grace waiter patches
`cuprum._stream_drain._await_eof_grace`; a test that needs to hold the drain
open patches `_drain_stream_consumers` on whichever module its entry point
resolves the name from, which for the streamed `run()` path is
`cuprum._subprocess_wait`, where `_reconcile_run_tasks` calls it.

The drain's log records are emitted under the historical channel name
`cuprum._subprocess_wait`, spelled explicitly in `_stream_drain` rather than
derived from `__name__`, so moving the code did not move the channel operators
filter on.

No public API changes, and no module in this tree needs a module-size
suppression.

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
still resolved through `_subprocess_context._sh_module()`. The three call sites
that pass an `ExecutionContext` to `_stdin_codec` — the direct run in
`cuprum/_subprocess_stream_run.py`, the shared builder in
`cuprum/_subprocess_streams.py` that both the streamed run and the line
iterator go through, and the owned handle's own construction in
`cuprum/_process_handle/tasks.py` — now import it from the new module. No
public API changes, and the module-size suppression remains unnecessary.

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
spawn, by `_subprocess_spawn._flush_borrowed_stdio`, so buffered caller-side
bytes reach the child. That flush sits beside the fork rather than in the
resolver because resolution and the fork coincide only on the `run()` path:
`lines()` resolves its bindings when it is called and forks at first iteration,
so a resolver-side flush would drop anything the caller wrote in between. The
mapping is where the two kinds are told apart, and that is why pipe-ness
travels as an explicit `pipes` frozenset. `Popen` treats `PIPE`, `DEVNULL`, a
raw `int`, and a file object differently, but it exposes a parent-side stream
object only for `PIPE`: the other three all leave `Popen.stdin`,
`Popen.stdout`, and `Popen.stderr` as `None`, so the `wait4` path cannot
recover pipe-ness from the child object it holds. The computed value cannot
supply the answer either, because a pipe nothing consumes has already been
folded down to `DEVNULL` and is indistinguishable there from a borrowed
descriptor the caller owns. Pipe-ness is therefore the one piece of the
resolution the value cannot express for itself.

`cuprum/_subprocess_deadline.py` now owns the child-exit half of ending a run:
`_wait_for_exit_code`, which awaits the process and terminates it on
cancellation without a timeout of its own, and
`_wait_for_exit_code_within_timeout`, which applies the deadline and translates
expiry into the public timeout surface. The remainder that bounds what follows
the wait, `_deadline_for_collection`, lives beside it for the same reason: the
subtraction is only meaningful against the reference that wait was scheduled
from. The other half — cancelling the stdin writer and draining the stream
consumers exactly once — deliberately stayed in `cuprum/_subprocess_wait.py`,
and the drain rules that reconciliation runs were later split out again into
`cuprum/_stream_drain.py` by the `origin/main` reconciliation. Nothing in the
new module touches the parent's stream tasks, which is what keeps a reader
wedged on a pipe from delaying the termination that lets it reach EOF. The
split is also where the earlier addenda's division of labour is preserved
unchanged: both helpers terminate the child but never drain, so the caller's
single drain through `_drain_stream_consumers` still reaches EOF, and a
non-positive deadline still expires immediately rather than racing
`asyncio.timeout`.

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
- `cuprum/sh/execution.py` — `ExecutionContext`, `TimeoutExpired`,
  `StdinInput`, and the streaming-stdin types this work added: `StdinStream`
  (the producer), `StdinSource` (the `StdinInput | StdinStream` union callers
  pass), and `StdinSourceError` (what a producer or encoder failure raises).
  All three joined the module's `__all__` and the `cuprum.sh` re-export list
  alongside `StdinInput`, which is why this roster entry — not just the
  addendum prose — had to grow.
- `cuprum/sh/results.py` — `CommandResult` and `PipelineResult`.
- `cuprum/sh/output.py` — `RunOutputOptions` and `IOOptions`.
- `cuprum/sh/stdio.py` — `StdioTarget` and the validation policing it.
- `cuprum/sh/safe_cmd.py` — `SafeCmd` and `SafeCmdBuilder`.
- `cuprum/sh/pipeline.py` — `Pipeline`, which the package re-exports, plus
  the deprecated flat `capture`/`echo` adapter `_resolve_pipeline_output` and
  its `_DeprecatedOutputFlags` payload. Those two moved here from `output.py`
  when the rebase onto `origin/main` pushed that module back over the ceiling:
  `Pipeline.run`/`run_sync` are their only callers, so colocating them with the
  class costs nothing and leaves `output.py`'s documented standard-stream
  options untouched.
- `cuprum/sh/factory.py` — the `make()` builder factory.

The public surface is unchanged: `cuprum.sh` still exports `StdioTarget`,
`Pipeline`, and (for internal callers) the two relocated helpers under the same
names with the same object identity, and the wheel snapshot is regenerated for
the two new modules. No public API changes, and the module-size suppression
remains unnecessary.

## Addendum (2026-10-01): a third `cuprum.sh` split, for the same ceiling

The 2026-09-27 addendum records `cuprum/sh/stdio.py` taking `StdioTarget` and
"the validation policing it" when `output.py` crossed the ceiling. That
description was accurate when written, and is now out of date for the same
reason as its predecessors: this round's review fixes pushed `stdio.py` itself
to 427 lines, against the same unsuppressible `max-module-lines` rule.

The overrun is branch-introduced. Against `origin/main` the module does not
exist at all — it is new in this work — so nothing here is a pre-existing
violation being inherited; it is the same class of growth the two prior addenda
record, one module further along.

The split follows the seam the 2026-09-27 addendum did not have to name,
because at that point the two responsibilities were still in one file. A
`StdioTarget` _is_ a statement about one target: which of the four variants it
is, which payload that variant may carry, and how a `path` payload is
normalized. Policing a _combination_ of targets is a statement about a whole
run: two answers to where stdin comes from, one stream told to be both captured
and redirected, one file named for both streams. The first is per-variant and
answerable from a single target; the second needs the assembled
`RunOutputOptions` and cannot be answered from any one target at all.

`cuprum/sh/stdio_rules.py` now owns the second. It holds
`_validate_stdio_targets`, the four `_reject_*` helpers,
`_share_one_owned_path`, and the `_STDIN_KINDS` set those rules consult.
`cuprum/sh/stdio.py` keeps the vocabulary and the per-variant rules and falls
from 427 to 246 lines; `stdio_rules.py` is 213. The corrected roster entries
are:

- `cuprum/sh/stdio.py` — `StdioTarget` and the per-variant rules: which payload
  each kind carries, and the normalization a `path` payload undergoes.
- `cuprum/sh/stdio_rules.py` — the rules policing _combinations_ of targets
  (`_validate_stdio_targets` and the `_reject_*` helpers, including the
  contested-stdin guard), which read a whole `RunOutputOptions`. Split out of
  `stdio.py` when the #445 review fixes pushed that module to 427 lines against
  the 400-line ceiling; `output.py` re-exports `_validate_stdio_targets`, so
  `__post_init__` still calls it by its old name.

The public surface is unchanged, and so is the call graph. `output.py` imports
`_validate_stdio_targets` from the new module and re-exports it under the same
name, so `RunOutputOptions.__post_init__` — which names that helper directly —
is untouched and the rules still fire at construction rather than at spawn.
`_command_internals.py` now imports `_reject_contested_stdin` from
`stdio_rules.py` instead of `stdio.py`; it is the one rule in the module that
cannot run at construction and is therefore called from the run's preparation.
The new module imports `StdioTarget`, `PipeStream`, and `RunOutputOptions` only
under `typing.TYPE_CHECKING`, so it adds no runtime import edge: no cycle is
introduced between `stdio`, `stdio_rules`, and `output`. The wheel-manifest
snapshot is regenerated for the new module, and `cuprum.sh`'s exports are
unchanged. No public API changes, and the module-size suppression still remains
unnecessary.

## Addendum (2026-10-02): RFC 0001's seam, reviewed against the rendezvous

`origin/main` advanced by one commit, `a592b50c`, while this branch was open:
RFC 0001 proposes an execution-interception backend that would lift the spawn
and "the wait that follows it" out of `_execute_subprocess()` and into a
`_DirectBackend.execute()`. The RFC is `Proposed`, no task depends on it, and
this branch ships no part of it. It is reviewed here because this branch
changed what the wait _does_, and the review's result is that the RFC needs no
amendment — recorded so the next implementer does not have to reach that
conclusion independently.

Three statements in the RFC remain accurate at this branch's head, and they are
checked rather than assumed. The entry point is unchanged:
`_execute_subprocess()` in `cuprum/_subprocess_execution.py` still takes the
pre-spawn readings and calls `_spawn_subprocess()`. The ordering the RFC
depends on is intact: `_spawn_subprocess()` returns the process, and the caller
then emits `start` with the real `pid`, waits, emits `exit`, and assembles the
`CommandResult`. The isolation claim holds too — the moved code _is_ already
behind `_spawn_subprocess()`.

What the RFC's step 1 would lift is "the spawn, the wait that follows it, and
the two reads of the live process": `start` with the real `pid`, and the rusage
measurement off the process object. It does not name the wait, but the phrase
resolves against the code without ambiguity — the wait `_execute_subprocess()`
awaits after the spawn is `_await_direct_completion()`, and the RFC's own
sentence that "the wait is what drives the streams, the timeout and the idle
monitor" describes exactly the dispatch, timeout translation and idle-monitor
settle that function owns.

Underneath it, this branch put a _caller_ around the child's exit wait, in
`cuprum/_subprocess_rendezvous.py`, which races that wait against the stdin
writer's task. Two of the three call sites sit below the function the RFC would
lift: `_await_direct_completion()` dispatches to
`_run_subprocess_without_streams()` for a direct run and to
`_run_subprocess_with_streams()` for one that consumes stdout or stderr. The
streaming branch reaches the race one level further down, through
`_wait_for_streamed_process_exit()` in `cuprum/_subprocess_stream_run.py`, and
both leaves construct the exit wait and hand it to the race. A backend that
lifts `_await_direct_completion()` therefore lifts the race for both.

The third call site does not sit below it, and that is worth naming rather than
eliding. Line iteration is a parallel entry point, not a branch of the direct
one: `cuprum/_line_iteration.py` calls `_start_line_stream_run()` and
`_coordinate_line_stream()`, and `cuprum/_line_stream/coordinator.py` calls
`_spawn_subprocess()` itself. Neither `_execute_subprocess()` nor
`_await_direct_completion()` is on that path, and the race is reached from
`_wait_for_line_stream_exit()` inside the coordinator. So the RFC's "one branch
in `_execute_subprocess()`" covers every run that goes through
`_execute_subprocess()` — direct and streamed, which is the case it argues for
— but line iteration is outside its reach entirely.

That is not a defect, because the RFC is explicit about its iteration boundary
and answers for it with an error rather than silence: "The first iteration
covers direct commands only; a pipeline whose scope has a substitute backend
raises `NotImplementedError`". The RFC does not name line iteration in that
non-goal, so the honest reading is that a `lines()` run is a path the proposal
leaves unaddressed rather than one it resolves. The resolution above should
therefore not be read as "one seam covers every run".

The writer is the part the RFC does not mention, and it does not need to.
`cuprum/_subprocess_stdin.py` is byte-identical at this branch's exclusive
boundary `c65d843c` and at `a592b50c`, and main's
`_run_subprocess_without_streams()` already spawned a writer through
`_spawn_stdin_writer()` and already reconciled it after the exit. So the RFC
could leave the writer unmentioned and still be right, because at main the
writer was already inside the thing it calls "the wait that follows" the spawn.

What this branch added to the writer's _lifecycle_ is a bounded settle,
`_settle_stdin_writer()`, so that a producer parked in `anext` cannot outlast
the run's own deadline. That too sits below the wait and follows it, so it is
lifted by the same move. Nothing here constrains the RFC's passthrough case,
which it lists as a first-class goal: a `_DirectBackend.execute()` that owns
the spawn and the wait owns the race and the settle by construction.

This addendum therefore records a _review_, not a design change, and it
resolves rather than defers: RFC 0001's "Where it is called" section is
accurate against this branch's head, and its implementer may proceed as
written. The only thing worth carrying forward is the resolution above — "the
wait" is `_await_direct_completion()`, not the child's exit wait it dispatches
to, and lifting it brings the stdin-writer arbitration along.

## Addendum (2026-10-02): split the chunk write out of `_subprocess_stdin_stream`

The 2026-09-27 addendum above records streaming stdin splitting out of
`_subprocess_stdin`. The module that split produced has since split once more,
and the second split was not recorded here when it landed — this addendum
closes that gap rather than leaving it to be rediscovered from the imports.

The seam is the one the code already drew. `cuprum/_subprocess_stdin_write.py`
owns _how_ a chunk reaches the pipe: the `_StreamSink`, the per-chunk write,
the incremental encoder and its flush, and the predicate that decides whether a
pipe error came from the child closing its end — none of which needs to know
about producers or about error types. `cuprum/_subprocess_stdin_stream.py`
keeps the pull loop and the failure handling that turns those events into the
public `StdinSourceError`. The two halves had already separated when the
source-failure fix landed: once a producer's own `OSError` had to be told apart
from the pipe's, the write side and the pull side stopped sharing state.

The precedent the 2026-09-27 addenda set is that each of these splits is
recorded with an overrun figure. None of the ordinary ones is available here.
Across every commit that holds `cuprum/_subprocess_stdin_stream.py` its peak is
353 lines, and the two modules stand at 347 and 156 now; neither was near the
ceiling once the split landed. The overrun itself was a working-tree
measurement, and the working tree it was taken in is not recoverable from any
commit — the same limitation the execution plan records for peak figures
generally.

What _is_ recoverable is a bound that decides the question the overrun figure
would have answered. Replaying the split commit's own diff shows that 79 of the
156 lines in the new module are lines that same commit deleted from the stream
module, so the code would have grown the stream module by at least those 79
lines had it stayed: 327 + 79 = 406, already past the 400-line ceiling before
counting any of the 26 lines the commit did not relocate. That is a
demonstration from the commit rather than a working-tree figure, and it holds
however the peak is read.

The split changed no public surface. `cuprum/_subprocess_stdin_stream.py`
reaches the write side as `_write`, and the maturin wheel snapshot gained
exactly one line for the new module, which is the same one-line-per-module
shape every other split here records.

## Addendum (2026-10-11): split command finalization out of `_command_internals`

`cuprum/_command_internals.py` carried 392 lines on the main branch before this
work and 424 after it, so the module crossed the 400-line ceiling and
`make lint` failed at its `pylint-classic` leaf with `C0302` on the branch
head. The trigger is narrow — resolving the stdio plan at spawn, and the
contested stdin check that has to sit beside it — but the headroom is not: a
module that begins eight lines under a hard ceiling will breach it on almost
any change, and suppressing the check was the option this ADR already rejected.

The seam is the one the code already drew. `cuprum/_command_internals.py`
decides _what_ a run is: it prepares the observation, resolves the stdio plan,
and builds the execution bundle. `cuprum/_command_finalization.py` owns
everything that happens once the child exists and on every exit path afterwards
— driving the subprocess, invoking the after-hooks, and settling the sink
bracket, the terminal event, and the observer tasks. The finalization half was
already a closed cluster: `_execute_with_hooks` had exactly one caller, and
none of the helpers it moved with was reachable from the preparation half.

The ordering inside those helpers is the reason they stay together rather than
being scattered. Closing the sink before draining means a drain failure cannot
replace a timeout annotation, and draining through `_shielded_cleanup` means a
caller who cancels repeatedly is still waited on rather than leaving observer
tasks stranded. Moving that cluster as a unit keeps the two invariants legible
in one file.

The overrun this time is measurable from the commits, unlike the working-tree
figure the 2026-10-02 addendum had to reconstruct. Across every commit on the
branch that holds the file its peak is 424 lines; the commit that first crossed
the ceiling is `10d33429`, at 407. The split removes 158 lines from
`_command_internals.py` and adds 18, which is the net −140 that brings it to
284 — 116 lines clear of the ceiling, rather than the eight it started with.

The split changed no public surface. The two names that leave are re-exported
from `cuprum/_command_internals.py`, so `cuprum.sh`, `cuprum.sh.safe_cmd`, and
the tests that import them are unchanged, and object identity holds: the
re-exported `_execute_with_hooks` and `_finalize_prepared_command_failure` are
the finalization module's own objects, not copies. The maturin wheel snapshot
gained exactly one line for the new module, which is the same
one-line-per-module shape every other split here records.

## Addendum (2026-10-11): split the source-error builders out of `_subprocess_stdin_stream`

The 2026-09-27 addendum above records streaming stdin splitting out of
`cuprum/_subprocess_stdin`, and the 2026-10-02 addendum records the chunk write
then splitting out of `cuprum/_subprocess_stdin_stream.py`. The module crossed
the ceiling once more on the streaming-stdin branch, and this addendum records
that third split. It supersedes the 2026-09-27 addendum's claim that the
`_stdin_source_error` / `_source_error` pair lives in
`cuprum/_subprocess_stdin_stream.py`; that text stays as the record of what was
true when it was written.

The overrun is measurable from the commits. Across every commit on the branch
that holds the file its peak is 419 lines, reached at `5b8cba42`; its parent
held 380. Classifying a producer that cannot start added 39 net lines and put
the module 19 over the ceiling, and `make lint` failed at its `pylint-classic`
leaf with `C0302` on the branch head. The headroom was already thin: a module
that begins twenty lines under a hard ceiling breaches it on almost any change,
which is the same reasoning the 2026-10-11 command-finalization addendum above
records.

The seam is the one the code already drew. `cuprum/_subprocess_stdin_stream.py`
owns _which_ failures are the producer's — the pull loop, the early-close
predicate, and the handler that tells the three outcomes apart.
`cuprum/_subprocess_stdin_errors.py` owns what the caller is told about them:
the `_stdin_source_error` / `_source_error` pair that builds the public
`StdinSourceError`. Neither function in the moved pair reads the sink, the
process, or the observation, and nothing there calls back into the pull loop,
so the edge is a leaf rather than a cycle.

The lazy import is the one constraint that travels with the code.
`cuprum.sh.execution` defines `StdinSourceError`, but `cuprum.sh` pulls in the
whole `cuprum` surface and interior modules are loaded while that surface is
still being built. The pair reaches the type through the shared `_sh_module()`
shim for the same reason it did before the move, so the exception a caller
catches is the class `cuprum` exports rather than a copy.

The split changed no public surface. `_subprocess_stdin_stream.py` imports
`_stdin_source_error` from the new module, and the two names were not in its
`__all__`, so no importer outside the module ever saw them. The maturin wheel
snapshot gained exactly one line for the new module, which is the same
one-line-per-module shape every other split here records.

## Addendum (2026-10-14): the deprecated output spellings leave `cuprum/sh/output.py`

`cuprum/sh/output.py` reached 379 lines of the 400-line ceiling the 2026-09-25
addendum's split was written to respect, and the per-stream capture policies of
issue #443 add fields, resolution, and a property to `RunOutputOptions` itself.
The module could not absorb that growth and could not shed it either: the two
compatibility surfaces it also held — the deprecated `IOOptions` alias and the
flat `capture=`/`echo=` keyword shim behind `Pipeline.run` — describe how to
_reach_ the options vocabulary rather than what the vocabulary means, and both
are scheduled to disappear.

Those two surfaces left `cuprum/sh/output.py`, but not for one home. `main` had
already split the flat keyword shim into `cuprum/sh/pipeline_output.py` while
this branch was in flight, so the merged tree keeps that module as the shim's
owner and its existing re-export through `cuprum.sh.pipeline`;
`cuprum/sh/_deprecated.py` takes only `IOOptions`:

- `IOOptions` — the deprecated alias, which resolves every field exactly as its
  base does and then warns, now in `cuprum/sh/_deprecated.py`.
- `_DeprecatedOutputFlags` and `_resolve_pipeline_output` — the flat
  `capture=`/`echo=` keyword arguments of `Pipeline.run`/`run_sync`, translated
  into a `RunOutputOptions`, in `cuprum/sh/pipeline_output.py`.

The options module keeps only `RunOutputOptions` and its private helpers, and
sheds both surfaces at once so the capture work has room.

No public API changes: `cuprum/sh/__init__.py` re-exports every moved name with
the same object identity, so `cuprum.sh.IOOptions` and
`cuprum.sh._resolve_pipeline_output` resolve as before and `cuprum/__init__.py`
is untouched. `cuprum/sh/pipeline.py` imports the flags from
`cuprum.sh.pipeline_output` and `IOOptions` comes from `cuprum.sh._deprecated`,
which are the only in-package callers. No module in this tree needs a
module-size suppression.

## Addendum (2026-10-14): the convenience-sink machinery leaves `cuprum/sh/output.py`

Clearing the deprecated spellings (the addendum above) took
`cuprum/sh/output.py` to 318 lines, but that was measured before the per-stream
capture policies of issue #443 were restored to it. The two new fields, their
resolution, and the `resolved_capture` property take the module to 431 lines —
past the 400-line ceiling a second time, and by the same mechanism: the ceiling
applies to production targets, and this is one.

`cuprum/sh/_convenience_sink.py` now owns the machinery behind the `group` and
`annotate_failure` convenience flags: `_validate_convenience_flags`, which
rejects a non-`bool` for either, and `synthesize_sink`, which materializes them
as a `GitHubActionsSink`. The two were formerly a module function and a private
method of `RunOutputOptions`; the method's body only ever read or replaced its
own object's fields, so making it a module-level function over an options
argument is a rename and nothing more. Together they are 58 lines, and moving
them leaves `cuprum/sh/output.py` at 370 lines.

The cut is a responsibility boundary, not a line count: these two describe what
the convenience flags _produce_ — a presentation adapter configured for one
half of the framing — whereas the options module describes what an options
object _is_. The workflow-command syntax, the stop-commands lease, and the
`GITHUB_ACTIONS` environment gate all stay inside the adapter and its
synthesizer, and the options class calls into them from `__post_init__`.

There is no cycle. `cuprum/sh/_convenience_sink.py` imports `GitHubActionsSink`
at run time and `RunOutputOptions` only under `TYPE_CHECKING`, so the
annotation on its parameter does not make the options module's import of it
mutual at run time. The `_synthesized_sink` identity field stays declared on
`RunOutputOptions`, because it is an init field that `dataclasses.replace`
forwards and is what distinguishes a sink this run synthesized from one a
caller supplied.

No public API changes: neither moved name was ever exported — one is private
and the other was a private method — so `cuprum/sh/__init__.py` and
`cuprum/__init__.py` are untouched by this addendum. `cuprum/sh/output.py` is
the only importer, and `make pylint-classic` reports 10.00/10 on both passes
with no module-size suppression anywhere in the tree.

## Addendum (2026-10-18): four splits forced by per-stream capture

The per-stream capture policies of issue #443 added a policy object, a decoded
configuration, an accumulator factory, and a second resolution rule to modules
that were already near the ceiling. Four more splits came out of that, each cut
along a boundary that predates the feature rather than purely by line count.

`cuprum/_stream_config.py` now owns `_StreamConfig`, the value a drain is
handed for one of a child's two streams. It moved out of `cuprum/_streams.py`
because that module also holds the consumption predicates and the echo and pipe
plumbing, and the config is the one part of it whose construction _derives_ an
accumulator — which is what makes a cancelled drain's spill file reachable from
the run's reconciliation point. `cuprum._streams` re-exports the type, so the
handful of modules that import it from there are unchanged; the new module is a
leaf and nothing imports it except through that re-export.

`cuprum/_subprocess_stream_post_exit.py` now owns the three awaits between a
streamed child's exit and its completed gather: settling an owned process
group, waiting through the deadline path, and waiting for the stdin writer.
These came out of `cuprum/_subprocess_stream_run.py`, which keeps the run loop,
and every await is written to hand a failure to the coordinator rather than
repair it locally, so the module reads as one policy. The run module re-imports
all three names under `as` aliases of themselves. That re-import is
load-bearing: the callers stayed behind in the run module, so it is that
module's globals they resolve, and the teardown-boundary tests patch the names
where the production path still reads them. Moving the definitions without
keeping those bindings would have left the tests patching a name nothing calls.

`cuprum/sh/_capture_options.py` now owns `_resolve_capture`, the rule that
turns one stream's declared value — `None`, a `bool`, or a policy — into a
policy or `None`. It left `cuprum/sh/output.py` for the same ceiling that drove
the two addenda above, and the cut is also the resolution rule in one place:
stdout and stderr resolve identically because they call the same function, and
the validation raises before a spawn rather than at a drain.
`cuprum/sh/output.py` re-exports it, so the options class calls it through the
name it always had.

`cuprum/_line_stream/spawn.py` now owns the part of a `SafeCmd.lines()` run
that exists before the run is handed back: the spawn, the stdin writer, the
stream consumers, the chained line hooks, and the unwind of a run that failed
before it could be returned. It split from
`cuprum/_line_stream/coordinator.py`, which keeps the settlement and deadline
logic that follows the hand-back; the coordinator's own docstring already
described itself in terms of what happens _after_ that point.
`cuprum/_line_stream/__init__.py` exports `_start_line_stream_run` from the new
module, so the package's own facade is the only seam callers see.

No public API changes in any of the four: every moved name is private, and the
three facades that re-export — `cuprum._streams`, `cuprum.sh.output`, and
`cuprum._line_stream` — preserve the import path each importer already used.
`cuprum/_subprocess_stream_run.py` additionally preserves the _binding_ in that
module, for the patch seam described above. `max-module-lines = 400` still has
no suppression anywhere in the tree.

### The resolved views move to a mixin

A fifth split followed from restoring a public import path rather than from the
capture feature itself. `cuprum.sh.output.IOOptions` was exported from
`cuprum/sh/output.py` on `main` and the 2026-10-14 split moved the class to
`cuprum/sh/_deprecated.py` without leaving the name behind. Restoring it there
is an import cycle — `_deprecated` imports `RunOutputOptions` from
`output.py` — so the module resolves it lazily through a module-level
`__getattr__`, and that adapter plus its docstring took `output.py` from 400
lines to 439, back over the ceiling.

`cuprum/sh/_resolved_options.py` now owns the four resolved-value queries —
`resolved_broken_pipe_policy`, `resolved_capture`, `captures_any_stream`, and
`resolved_echo` — as `_ResolvedOptionsMixin`, which `RunOutputOptions`
inherits. The cut is a genuine one: these are the _query_ half of the options,
what the caller's spelling resolved to, while the fields they read stay
declared on the dataclass. It is a deliberate reversal of the ground on which
the 2026-10-14 convenience-sink addendum declined to move `resolved_capture`
("the plan requires it mirroring `resolved_echo`"): all four now move together,
so they still mirror each other, and the alternatives had narrowed to a
`too-many-lines` suppression or leaving the public import path broken.

The mixin declares `__slots__ = ()` for the reason `_BytesEntryPoints` and
`_OwnedStart` do: an un-slotted mixin donates a `__dict__` to the frozen,
slotted dataclass inheriting it, which would silently defeat the slots.
`cuprum/sh/output.py` keeps no re-export of the properties, because there were
never separate names to re-export — the class's own attribute lookup finds them
through the base, so every `options.resolved_capture` call site is unchanged
and the class docstring's examples still hold.

## Addendum (2026-10-18): the spill-I/O leaf and the asynchronous capture seam

CodeRabbit's round-8 review of the per-stream capture work made the filesystem
calls of a file-backed capture blocking work that ran on the run's event loop:
create, write, flush, and byte-mode read-back all executed inline, so a slow or
full disk held up the child's other stream, the deadline, and cancellation for
as long as the write took. Moving them onto a worker is not a rewrite of one
call site. `asyncio.to_thread` starts a separate worker per call, and the
capture's contract is _order_: the buffered prefix must land before the chunks
after it, and the flush before the read-back. Serializing one accumulator's
calls needs state, and that state has to live somewhere.

`cuprum/_capture_spill_io.py` is that somewhere. It holds `_create_spill` — the
one blocking call that has to exist as a named function, because it is the seam
the failure tests patch — plus `_SpillIo`, the object that runs each blocking
call on a worker under a per-accumulator lock. Every other call is a bound
method supplied at the call site: the accumulator awaits
`self._io.run(file.write, chunk)` and gets the result or the `OSError`. Naming
each call as a one-line forwarding function was the first shape, and the tree's
own `R9104` rule rejects that as a trivial attribute wrapper; supplying the
bound method at the call site is the rule's documented remedy, and it also
makes the traceback name the method that actually raised. The split is cut
along a real boundary rather than by line count: `cuprum/_capture_file.py`
keeps the _policy_ — when to spill, how a failure is absorbed, what the record
says — and the new module owns _how a blocking call is made without blocking
the run_. The accumulator's own logic never sees a thread, an executor, or a
lock.

The direction of the import graph is what makes the split safe. The new module
imports exactly one thing from the tree, `_shielded_cleanup` from
`cuprum/_process_lifecycle.py`, and `_capture_file.py` imports the leaf. No
cycle: `_process_lifecycle` does not know about capture, and the leaf composes
the lifecycle module's existing cancellation answer rather than adding a second
one. `tempfile` also moved here, because the create call is the leaf's
business; the failure tests patch
`cuprum._capture_spill_io.tempfile.NamedTemporaryFile` and so reach the real
create through the seam they already exercised.

The seam it feeds is `CaptureSink`, in `cuprum/_capture_types.py`, whose three
methods are now coroutines. That is a deliberate protocol change and not a
convenience: retention is the child's output, a file-backed policy cannot
retain a chunk without a write, and a synchronous `absorb` would put that write
on the loop by construction. Making the whole seam async — not just the
file-backed implementation — keeps one protocol for the drain rather than a
policy test between two, at the cost of a coroutine step per chunk for the two
heap-backed accumulators, which answer without suspending. The drain awaits
`absorb` in `cuprum/_streams.py` and `finalize` in
`cuprum/_stream_drain_finish.py`, so both the single-command and the pipeline
paths reach a spill through the same await.

`SpillHandle.close` in the same module changed with it: its unlink already ran
on a worker, and its descriptor close now does too, because closing is the same
blocking call that took the rest of the spill off the loop. `keep_file=True`
remains the sole exception — the descriptor is closed and the file is left for
the caller.

No public API change: the leaf is private, `CaptureSink` is a private protocol
implemented by private accumulators, and the four `cuprum/_capture_*.py`
modules that existed before keep their names and responsibilities.
`max-module-lines = 400` still has no suppression: the largest capture module is
`cuprum/_capture_file.py` at 361 lines.
