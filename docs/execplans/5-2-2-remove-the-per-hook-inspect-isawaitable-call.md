# Remove the per-hook `inspect.isawaitable` call from per-line event dispatch

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`,
`Decision log`, `Outcomes & retrospective`, `Conformance basis`, and
`Verification plan` must be kept up to date as work proceeds.

Status: DRAFT

Roadmap item: 5.2.2 in `docs/roadmap.md` (phase 5, "Reclaim the pure-Python
consume hot path"; step 5.2, "Make per-line event emission cheap for
line-callback workloads").

Branch: `5-2-2-remove-the-per-hook-inspect-isawaitable-call`.

## Purpose / big picture

Cuprum lets a caller register *observe hooks*. An observe hook is a callable
that receives an `ExecEvent` (a frozen record describing one moment in a
command's life, such as `plan`, `start`, `stdout`, or `exit`). When a caller
also asks for per-line output events, Cuprum emits one `ExecEvent` per output
line, so the dispatcher that hands each event to each hook runs once per line
per hook. The tee profiling baseline
(`docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, Table 4) found that
the dispatcher spends a measurable share of that per-line time asking
`inspect.isawaitable(result)` of every hook's return value, even though almost
every hook is an ordinary function that returns `None`. In the post-5.2.1
profile the call accounts for 589 py-spy samples under the per-line path
(`docs/tee-hotpath-line-event-emission-5-2-1.md`, around line 333).

After this change, Cuprum decides once, when an execution binds its hooks,
whether each hook is *detectably asynchronous*. The per-line dispatcher then
uses that decision instead of re-asking `inspect.isawaitable` for every result:
a synchronous hook that returns `None` is finished with a single identity
comparison, and a hook declared `async def` has its coroutine scheduled
directly. A plain function that happens to return an awaitable (for example a
lambda that returns a coroutine) keeps working exactly as before, through a
rare fallback that is off the hot path for every hook that honours its own
declared shape.

A user observes three things:

1. Event payloads, hook ordering, failure semantics, and async-hook scheduling
   are unchanged; the existing behavioural and snapshot suites pass
   unmodified.
2. A committed py-spy capture of the line-callback scenario shows
   `inspect.isawaitable` contributing exactly 0 sampled frames under the
   per-line hot path, while a matched control capture of the pre-change tree
   shows it contributing hundreds.
3. The line-callback scenario is no slower, and is expected to be modestly
   faster; the measured change is recorded rather than promised.

## Constraints

These are hard invariants. Violating one requires escalation, not a
workaround.

- C1. Observable event semantics must not change. Every `ExecEvent` emitted
  for every phase keeps the same field values, the same per-hook delivery
  order, and the same per-line freshness (one new event and one clock read per
  delivered line, as established by 5.2.1).
- C2. Async-hook scheduling must be identical for every hook whose result is
  an awaitable: the same `asyncio.create_task` call shape, the same task name
  `cuprum.observe.<phase>`, the same `_await_awaitable` wrapper and its
  `observe_hook_task_started`, `observe_hook_task_failed`, and
  `observe_hook_task_finished` DEBUG/ERROR records, the same
  `observe_hook_task_scheduled` DEBUG record with the same `extra` keys, and
  the same position in the returned task list.
- C3. Failure semantics must not change. A hook raising `CancelledError` is
  wrapped in `_ExecEventEmissionError` with no log; any other
  `BaseException` is logged as `observe_hook_failed` with the same `extra`
  keys and wrapped; both carry exactly the tasks scheduled by earlier hooks
  in the same emission (the *scheduled prefix*).
- C4. The public observe-hook contract in `docs/users-guide.md` ("Awaitable
  hook results are scheduled as `asyncio.Task` instances and awaited before
  the run completes") remains true for every callable shape that satisfies
  the `ExecHook` type alias in `cuprum/events.py`, including plain callables
  that return an awaitable. The plan must not narrow that contract.
- C5. Public surfaces stay byte-for-byte stable: `cuprum.observe`,
  `HookRegistration`, `CuprumContext.observe_hooks`,
  `ScopeConfig.observe_hooks`, the `ExecHook` alias, and the identity of the
  hook objects stored in those tuples. Detach-by-identity
  (`CuprumContext.without_observe_hook`) and the `_PipelineWaitReporter`
  `isinstance` dispatch in `_StageObservation.report_pipeline_wait` must keep
  seeing the caller's own hook objects.
- C6. No Rust, native-extension, dependency, or packaging-metadata change.
  Phase 5 is deliberately Rust-free (roadmap phase 5 "Idea").
- C7. The per-line path must keep routing through
  `_StageObservation._emit_event`, which owns `pending_tasks` (see the
  `_line_event_emitter` docstring in `cuprum/_line_callbacks.py`).
- C8. Production modules stay at or under 400 lines (AGENTS.md; enforced for
  production code by pylint). `cuprum/_observability.py` is 259 lines today.
- C9. Every quality gate in `AGENTS.md` passes before each commit: `make
  check-fmt`, `make typecheck`, `make lint`, `make test`, and for Markdown
  edits `make markdownlint` and `make nixie`.
- C10. The 5.2.1 construction-share gate is not an acceptance criterion for
  this item. Removing work outside `ExecEvent.__init__` is forecast to raise
  that share from 29.9414% towards 30.9686% *by succeeding*
  (`docs/developers-guide.md`, "Construction-share classification (roadmap
  5.2.1)"). A re-run that crosses 30% after this change is the documented
  inversion, not a regression, and must be reported as such.

## Tolerances (exception triggers)

- Scope: stop and escalate if production changes touch more than four modules
  under `cuprum/` (planned: `cuprum/_observability.py`,
  `cuprum/_pipeline_types.py`, `cuprum/_idle_heartbeat.py`, and the new
  `cuprum/_callable_kinds.py`), or exceed 150 net production lines.
- Interface: stop if any public signature, public attribute, or the contents
  of `CuprumContext.observe_hooks` / `ScopeConfig.observe_hooks` would need to
  change.
- Contract: stop if any existing test in `cuprum/unittests/` or
  `tests/behaviour/` must have its *assertions* changed (as opposed to its
  call into the private `_emit_exec_event` signature) to pass, or if any
  committed syrupy snapshot changes.
- Measurement: stop if, after two complete collection attempts, a candidate
  capture still shows a non-zero `inspect.isawaitable` count under the
  per-line anchor, or if any capture's anchor weight is below 10,000 samples.
- Performance: stop if any unprofiled paired scenario's candidate median is
  more than 5% slower than its control median.
- Iterations: stop if a red test cannot be made green within three attempts
  without breaching another tolerance.
- Dependencies: stop if any new runtime or development dependency appears to
  be required.
- Ambiguity: stop if the expert review or implementation evidence shows that a
  hook shape the users' guide supports today would be scheduled differently.

## Risks

- R1. Registration-time classification is unsound if used as a gate.
  Severity: high. Likelihood: certain if implemented literally.
  `inspect.iscoroutinefunction` cannot see a plain callable that *returns* an
  awaitable. The local probe recorded in `Surprises & discoveries` shows that
  on CPython 3.12.13, 3.13.13, 3.14.4, and 3.15.0b2 a lambda returning a
  coroutine, a plain function returning one, and a `functools.wraps` sync
  wrapper around an `async def` all classify as not-a-coroutine-function yet
  return awaitables. Mitigation: the classification selects a fast path; it
  never suppresses scheduling. Non-`None` results from hooks classified
  synchronous still reach `inspect.isawaitable` (Decision D1).
- R2. `inspect.markcoroutinefunction` lets a plain function declare itself a
  coroutine function. If such a hook lies and returns a non-awaitable,
  skipping detection on the async path would turn today's silent no-op into a
  `TypeError` raised when the run awaits its tasks. Severity: low.
  Likelihood: low. Mitigation: Decision D2 keeps the guard for the residual
  case so behaviour is identical; the property suite includes a lying-marker
  class.
- R3. The profiler cannot prove a zero at low sample counts. Severity:
  medium. Likelihood: low. Mitigation: the control capture must show a
  non-zero count of the same frame under the same anchor (expected in the
  hundreds), every capture's anchor weight must be at least 10,000, and three
  matched pairs are collected.
- R4. Adding `cuprum/_callable_kinds.py` changes the wheel file manifest and
  breaks `cuprum/unittests/__snapshots__/test_maturin_build.ambr`. Severity:
  low. Likelihood: certain. Mitigation: update that snapshot in the same
  commit that adds the module, and confirm the diff names only the new file.
- R5. Changing the private `_emit_exec_event` signature breaks tests that call
  it with raw hook tuples (`cuprum/unittests/test_cqrs_helpers.py` around
  lines 120, 138, 169; `cuprum/unittests/test_cqrs_hook_behaviour.py` around
  lines 248 and 256). Severity: low. Likelihood: certain. Mitigation: update
  those calls in the same commit to pass `_classify_observe_hooks(...)`; do not
  add a compatibility overload (the function is private).
- R6. CodeScene and pylint complexity limits on `_emit_exec_event`. One extra
  branch previously tipped CodeScene's mean cyclomatic complexity over its
  threshold (5.2.1 plan, around lines 2716-2750). Severity: medium.
  Likelihood: medium. Mitigation: extract scheduling into a named helper
  `_schedule_hook_result`, keep the loop body flat, and run `cs delta
  origin/main` before pushing.
- R7. Variant mix-up during profiling (a candidate run importing the control
  tree, or vice versa). Severity: high. Likelihood: low. Mitigation: each
  capture records `cuprum.__file__` and a variant probe (the presence or
  absence of `cuprum._observability._classify_observe_hooks`) in
  `variant.txt`, as 5.2.1 did.

## Progress

- [x] (2026-10-01) Branch `5-2-2-remove-the-per-hook-inspect-isawaitable-call`
  created from `main` at `71aaf3eb`; the remote branch did not exist yet.
- [x] (2026-10-01) Reconnaissance: hook registration and dispatch paths,
  tests, documentation contract, and 5.2.1 profiling method surveyed.
- [x] (2026-10-01) Classification probe run on CPython 3.12-3.15.
- [x] (2026-10-01) Draft plan written.
- [ ] Expert design review completed and incorporated.
- [ ] Plan approved by the user.
- [ ] EP-M1: shared async-callable classifier extracted.
- [ ] EP-M2: red tests committed.
- [ ] EP-M3: classified dispatch implemented; red tests green.
- [ ] EP-M4: frame-census tool implemented and tested.
- [ ] EP-M5: profiler artefacts collected and evidence document written.
- [ ] EP-M6: documentation, changelog, and roadmap updated; item marked done.

## Surprises & discoveries

- Observation: static classification cannot identify every hook that returns
  an awaitable.
  Evidence: `/tmp/probe-5-2-2-classify.py`, output in
  `/tmp/probe-classify-cuprum-5-2-2.out`, reproduced in `Artefacts and notes`.
  On all four interpreters, `async def`, `functools.partial` of an `async
  def` or bound async method, a bound async method, and
  `inspect.markcoroutinefunction` are recognised by
  `inspect.iscoroutinefunction`; an instance with `async def __call__` is
  recognised only by the `type(obj).__call__` rule that
  `cuprum/_idle_heartbeat.py:_is_async_callback` already applies; a lambda or
  plain function returning a coroutine and a `functools.wraps` sync wrapper of
  an `async def` are recognised by neither.
  Impact: classification can only select a fast path; Decision D1.
- Observation: `inspect.isawaitable(None)` costs roughly 230-480 ns per call
  on these interpreters against roughly 11-14 ns for `result is None`.
  Evidence: same probe (`timeit`, 2,000,000 iterations each).
  Impact: the fast path for synchronous hooks should be an identity test, not
  a cheaper detector.
- Observation: the roadmap's citation `cuprum/_observability.py:35` is stale;
  the call is at line 132 in `_emit_exec_event` (defined at line 88).
  Impact: correct the citation when the roadmap entry is ticked.
- Observation: the 5.2.1 collection script was never committed; only the
  capture and classifier commands survive in
  `docs/tee-hotpath-line-event-emission-5-2-1.md` and
  `docs/profiling/5-2-1-line-event-emission/README.md`.
  Impact: this plan restates every command it needs and commits its own
  README with them.
- Observation: `benchmarks/summarize_folded.py` ranks the top `--limit` frames
  and therefore cannot prove that a frame is absent; the 5.2.1 classifier
  computes a construction share with a hard-wired 30% limit and cannot be
  repurposed as a presence census without changing its meaning.
  Impact: EP-M4 adds a small frame-census command that reuses the 5.2.1
  capture model (`benchmarks/_line_event_profile_model.py`).

## Decision log

- Decision D1 (proposed; subject to expert review and user approval):
  interpret "classify each hook as sync or async once at registration" as
  selecting a per-hook *dispatch path* once, never as deciding whether a
  result may be scheduled.
  Hooks classified *async* (detectably asynchronous by the shared rule) have
  their result scheduled. Hooks classified *sync* finish on `result is None`
  with no awaitable detection; a non-`None` result from a sync-classified hook
  falls back to the unchanged `inspect.isawaitable` test, so a lambda that
  returns a coroutine is still scheduled.
  Rationale: the literal reading (never inspect a sync-classified hook's
  result) silently drops the coroutines returned by lambdas, plain wrappers,
  and `functools.wraps` decorators, violating C2 and C4 and leaving "coroutine
  was never awaited" warnings. The success criterion is phrased over
  *known-sync* hooks, and a hook returning `None` is the only shape whose
  synchrony is known; the fallback runs only when a hook has just
  demonstrated that it is not the shape it was classified as.
  Alternative considered: no classification, only `if result is not None and
  inspect.isawaitable(result)`. It meets the frame criterion for synchronous
  hooks but keeps detection on the async path and departs from the roadmap's
  stated mechanism. It remains the fallback if the expert review rejects D1.
  Date/Author: 2026-10-01, planning agent.
- Decision D2 (proposed): on the async path, schedule a non-`None` result
  directly when it is a coroutine object, testing with
  `type(result) is types.CoroutineType`, and fall back to `inspect.isawaitable`
  only for any other non-`None` result; skip a `None` result exactly as today.
  Rationale: a genuine `async def` call always returns a native coroutine, so
  the exact-type test discharges every honest async hook in one C-level
  comparison, while the fallback keeps today's behaviour for awaitable objects
  that are not native coroutines (futures, objects with `__await__`) and for
  a lying `markcoroutinefunction` marker (R2). This keeps C2 exact rather than
  "identical except for pathological hooks".
  Date/Author: 2026-10-01, planning agent.
- Decision D3 (proposed): classify where an execution binds its hooks, in
  `_ExecutionHooks.__post_init__` (`cuprum/_pipeline_types.py`), and store the
  result in a derived field `observe_dispatch`, rather than in
  `CuprumContext`, `ScopeConfig`, or `observe()`.
  Rationale: hooks reach an execution through four construction paths
  (`observe()` → `with_observe_hook`, `ScopeConfig`, direct `CuprumContext(...)`
  construction, and `narrow` merging) but leave the context through exactly
  one seam, `_collect_hooks` (`cuprum/_pipeline_internals.py:87`), which
  builds `_ExecutionHooks` once per command or pipeline stage. Classifying
  there covers every path, costs well under a microsecond per hook per stage,
  is never per event, and leaves C5 untouched because the context keeps the
  caller's own hook objects. Classifying inside the public context would add a
  derived field to a public frozen dataclass for no behavioural gain.
  This is a recorded deviation from the roadmap's literal word "registration";
  the roadmap entry is to be reworded on completion to "once per execution,
  when hooks are bound".
  Date/Author: 2026-10-01, planning agent.
- Decision D4 (proposed): extract the existing `_is_async_callback` rule from
  `cuprum/_idle_heartbeat.py` into a new dependency-free module
  `cuprum/_callable_kinds.py` as `_is_async_callable`, and use it from both the
  idle heartbeat validator and the observe-hook classifier.
  Rationale: AGENTS.md requires sweeping for an existing helper before adding
  one. The sweep found exactly one (`_is_async_callback`, the only
  `inspect.iscoroutinefunction` user in `cuprum/`). Importing it from
  `_idle_heartbeat` would couple observability to heartbeat code; a leaf
  module avoids that coupling and any import cycle. Scope and reuse policy:
  `_callable_kinds` answers only "is this callable detectably asynchronous?";
  it must not grow per-result detection.
  Date/Author: 2026-10-01, planning agent.
- Decision D5 (proposed): no ADR. The change preserves every public contract
  and is recorded in `docs/cuprum-design.md` §8.1.3 instead. If the expert
  review or the user prefers the literal (contract-narrowing) reading of the
  roadmap, that *would* be a substantive decision and would require ADR 019.
  Date/Author: 2026-10-01, planning agent.
- Decision D6 (proposed): no Rust, Verus, Kani, or `proptest` work. The
  obligations concern CPython callable semantics and asyncio scheduling, which
  cannot be modelled in a Rust extension without moving the hot path across
  the foreign function interface, contrary to C6. Hypothesis differential
  testing against a frozen oracle is the proportionate instrument
  (`Verification plan`).
  Date/Author: 2026-10-01, planning agent.

## Outcomes & retrospective

Not yet started. Record the measured frame counts, the unprofiled timing
pairs, and any deviation here at each milestone.

## Context and orientation

Cuprum is a Python library for running allow-listed subprocesses with typed
commands, structured events, and optional Rust acceleration of stream pumping.
Everything this plan touches is pure Python under `cuprum/`, `benchmarks/`,
`tests/`, and `docs/`.

Terms used below:

- *Observe hook*: a callable of type `ExecHook = Callable[[ExecEvent],
  Awaitable[None] | None]` (`cuprum/events.py:296`) registered with
  `cuprum.observe(hook)` (`cuprum/context/registration.py:340`) or supplied in
  `ScopeConfig.observe_hooks` (`cuprum/context/_scope.py:91`). Hooks are
  stored as a plain tuple on the frozen dataclass
  `CuprumContext.observe_hooks` (`cuprum/context/core.py:74`), appended by
  `with_observe_hook` (around line 282) and removed by identity in
  `without_observe_hook` (around line 310).
- *Execution hooks*: `_ExecutionHooks` (`cuprum/_pipeline_types.py:66`), a
  frozen, slotted private dataclass holding the before, after, and observe
  hook tuples for one command or pipeline stage. It is built by
  `_collect_hooks(ctx)` (`cuprum/_pipeline_internals.py:87`), the only place
  hooks leave the context, called from `cuprum/_command_internals.py:291`,
  `cuprum/sh/safe_cmd.py:191`, and per stage in
  `cuprum/_pipeline_internals.py:106`. Tests also construct it directly with
  raw tuples.
- *Stage observation*: `_StageObservation` (`cuprum/_pipeline_types.py`) owns
  one stage's hooks and its `pending_tasks` list. Its `_emit_event` method
  (around line 220) is the only production caller of `_emit_exec_event`.
- *Dispatcher*: `_emit_exec_event(hooks, event)`
  (`cuprum/_observability.py:88`). For each hook in order it calls the hook,
  wraps failures in `_ExecEventEmissionError` together with the tasks
  scheduled so far, and, if `inspect.isawaitable(result)` (line 132), schedules
  the result as `asyncio.create_task(_await_awaitable(result, event.phase),
  name=f"cuprum.observe.{event.phase}")` and logs
  `observe_hook_task_scheduled`.
- *Per-line hot path*: `_LineEventEmitter.emit_line`
  (`cuprum/_line_callbacks.py`, around line 139), built by `_line_event_emitter`
  once per observed stream in 5.2.1, calls the bound
  `_StageObservation._emit_event` once per delivered line. In a py-spy capture
  the chain renders as `emit_line (cuprum/_line_callbacks.py:…)` →
  `_emit_event (cuprum/_pipeline_types.py:…)` →
  `_emit_exec_event (cuprum/_observability.py:…)` → `isawaitable
  (inspect.py:…)`.
- *Detectably asynchronous*: `inspect.iscoroutinefunction(hook)` is true, or
  `inspect.iscoroutinefunction(type(hook).__call__)` is true. This is the rule
  `cuprum/_idle_heartbeat.py:356` (`_is_async_callback`) already uses to reject
  async `on_idle` callbacks.
- *py-spy raw capture* (`stacks.folded`): one line per distinct stack, frames
  separated by `;`, each frame rendered `function (path:line)`, followed by a
  space and an integer sample weight.
- *Anchor*: a frame whose presence in a stack marks that stack as belonging to
  the per-line hot path. This plan's anchor is `emit_line` in
  `cuprum/_line_callbacks.py`.

Related prior work: item 5.2.1 hoisted the invariant event fields; its plan
(`docs/execplans/5-2-1-hoist-the-invariant-exec-event-and-event-details.md`),
evidence (`docs/tee-hotpath-line-event-emission-5-2-1.md`), and raw captures
(`docs/profiling/5-2-1-line-event-emission/`) define the profiling method this
plan reuses. Item 5.2.3 will later add a combinatorial event-parity suite over
the hook-type and stream-mode matrix; this plan's differential property is
scoped to the dispatcher and must not pre-empt that suite.

### Documentation and skills signposts

Read before starting:

- `AGENTS.md` (quality gates, file-size cap, commit rules).
- `docs/roadmap.md` §5 and §5.2 (the item and its neighbours).
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, Table 4.
- `docs/tee-hotpath-line-event-emission-5-2-1.md` (method, inversion warning).
- `docs/profiling/5-2-1-line-event-emission/README.md` (artefact layout).
- `docs/cuprum-design.md` §7.1 and §8.1.3 (event model, async observers).
- `docs/users-guide.md`, the observe-hook section and "When an observe hook
  raises".
- `docs/developers-guide.md`: "Choosing a test shape per observe hook",
  "Profiling harness overview", "Construction-share classification (roadmap
  5.2.1)", and "Line observation".
- `docs/adr-002-additional-rust-components.md` (line-callback workloads are
  optimized in Python, not Rust) and
  `docs/adr-008-rust-pump-observation-channel.md` (the separate synchronous
  pump-hook channel; do not conflate it with observe hooks).
- `docs/documentation-style-guide.md` and `docs/scripting-standards.md`.
- `.rules/python-00.md`, `.rules/python-typing.md`,
  `.rules/python-exception-design-raising-handling-and-logging.md`.

Skills to load: `execplans` (this plan), `python-router` then
`python-types-and-apis` and `python-testing`, `hypothesis` for the
differential property, `python-quality-tools` for py-spy work,
`codegraph-mcp` for caller and impact queries, `en-gb-oxendict` for prose,
`commit-message` and `pr-creation` for delivery, and `firecrawl-mcp` for any
external documentation lookup. `rust-router` was consulted and is not needed
beyond Decision D6.

## Conformance basis

Upstream artefacts, at `main` revision `71aaf3eb`:

- `docs/roadmap.md` item 5.2.2 (identifier ROAD-5.2.2), with three success
  clauses: ROAD-5.2.2-S1 "known-sync hooks dispatch without per-line awaitable
  detection"; ROAD-5.2.2-S2 "async hooks retain identical scheduling
  behaviour"; ROAD-5.2.2-S3 "a committed profiler artefact shows
  `inspect.isawaitable` contributes 0 sampled frames in the per-line hot
  path". Mechanism clause ROAD-5.2.2-M "classifying each hook as sync or async
  once at registration".
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, Table 4 (BASE-T4,
  "avoid `inspect.isawaitable` for known-sync hooks").
- `docs/cuprum-design.md` §8.1.3 "Async observers" (DES-8.1.3-ASYNC).
- `docs/users-guide.md` observe-hook contract (UG-OBS-AWAIT, "Awaitable hook
  results are scheduled…").
- `docs/adr-002-additional-rust-components.md` (ADR-002: line-callback cost is
  a Python concern).
- No Terms of Reference document exists for this phase.

Trace links:

```plaintext
BASE-T4 -> ROAD-5.2.2-S1 -> D1/D2 -> EP-M3 -> test_observe_hook_dispatch::test_sync_none_results_never_reach_isawaitable
ROAD-5.2.2-M -> D3/D4 -> EP-M1, EP-M3 -> test_callable_kinds::test_classification_table, test_observe_hook_dispatch::test_hooks_are_classified_once_per_execution
ROAD-5.2.2-S2 + UG-OBS-AWAIT + DES-8.1.3-ASYNC -> D1/D2 -> EP-M3 -> test_observe_hook_dispatch_properties::test_dispatch_matches_reference_oracle, tests/features/observe_hook_dispatch.feature
ROAD-5.2.2-S3 -> EP-M4, EP-M5 -> docs/profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/frame-census.json
```

Deviation recorded for approval: D3 classifies when an execution binds its
hooks rather than inside `observe()`; see the Decision log.

## Verification plan

The change introduces one new invariant (dispatch equivalence), one new
contract (classification is computed once per execution), and one measurable
absence (no `isawaitable` frame under the per-line anchor). No lemma requires a
formal proof; Decision D6 records why.

Axioms relied upon, not verified here:

- A1. CPython evaluates `async def` calls (including bound methods and
  `functools.partial` wrappers recognised by `inspect.iscoroutinefunction`) to
  objects of exact type `types.CoroutineType`. Exercised by the
  classification table on every interpreter in the CI matrix (3.12-3.15).
- A2. `inspect.iscoroutinefunction` and `inspect.isawaitable` behave as
  documented in the standard library for each supported interpreter; the
  probe in `Artefacts and notes` records the observed behaviour.
- A3. py-spy 0.4.2 in raw mode attributes a sample to every Python frame on
  the sampled stack, so a function that runs on the per-line path at a
  measurable rate appears in some sample; the control capture is the
  empirical check that it does.
- A4. `asyncio.create_task` scheduling order equals call order within one
  emission (FIFO ready queue).

Obligations:

- V1. Dispatch equivalence.
  Obligation: for every finite sequence of hooks drawn from the hook-shape
  classes below and every phase, `_emit_exec_event(_classify_observe_hooks(
  hooks), event)` is observationally equivalent to the pre-change dispatcher:
  same hook call order, same returned task list length and task names, same
  results when the tasks are awaited, same exception type and identity on
  failure, same scheduled-prefix length in `_ExecEventEmissionError`, and the
  same sequence of log records (logger, level, message, and `extra` keys).
  Method: Hypothesis differential (oracle) property test.
  Rationale: the domain is sequences with ordering-dependent failure; a
  property over generated sequences covers prefix and interleaving cases that
  finite tables miss. The oracle is the current dispatcher body copied
  verbatim into the test support module as `_reference_emit_exec_event`, so
  the comparison is against real pre-change behaviour rather than a restated
  specification.
  Domain: lists of length 0 to 8 drawn from twelve classes: sync returning
  `None`; sync returning a non-awaitable non-`None` value; plain function
  returning a coroutine; lambda returning a coroutine; `functools.wraps`
  sync wrapper of an `async def`; `async def`; `functools.partial` of an
  `async def`; bound async method; instance with `async def __call__`;
  instance with sync `__call__`; honest `markcoroutinefunction`; lying
  `markcoroutinefunction` returning `None`; lying `markcoroutinefunction`
  returning a non-awaitable non-`None` value; hook returning an
  `asyncio.Future`; hook raising `Exception`; hook raising a non-`Exception`
  `BaseException`; hook raising `CancelledError`. (The list is open to the
  expert review.) Phases sampled from `plan`, `start`, `stdout`, `exit`.
  Artefact: `cuprum/unittests/test_observe_hook_dispatch_properties.py` with
  helpers in `cuprum/unittests/_observe_hook_dispatch_support.py`.
  Evidence: `uv run pytest
  cuprum/unittests/test_observe_hook_dispatch_properties.py -v` passes; before
  EP-M3 the test cannot import `_classify_observe_hooks` and is marked
  `xfail(strict=True)` for the red stage.
  Non-vacuity: each class is tagged with `hypothesis.event(...)` and the run
  is executed with `--hypothesis-show-statistics` once, with the output
  recorded in `Artefacts and notes`; an `@example` pins one sequence per class
  and one sequence where a failing hook follows two scheduling hooks. A seeded
  fault test, `test_oracle_rejects_literal_classification`, runs the same
  comparison against a deliberately wrong dispatcher that never inspects
  sync-classified results and must observe a mismatch on the lambda class; a
  second seeded fault that schedules every non-`None` result of an
  async-classified hook without detection must be rejected by the
  lying-marker class that returns a non-awaitable value.
- V2. Classification table.
  Obligation: `_is_async_callable` returns true exactly for the detectably
  asynchronous shapes listed in `Context and orientation`, and the observe
  classifier maps each hook to the dispatch path D1 prescribes.
  Method: parameterized pytest (finite partition with named rows).
  Artefact: `cuprum/unittests/test_callable_kinds.py`.
  Evidence: passes on every interpreter in the CI matrix.
  Non-vacuity: rows exist for both outcomes; the sync-wrapper rows assert
  `False`, recording the known limitation as specification rather than a
  surprise.
- V3. Known-sync results never reach awaitable detection (ROAD-5.2.2-S1).
  Obligation: for hooks classified sync that return `None`, and for `async
  def` hooks, emitting any number of events performs zero calls to
  `inspect.isawaitable`.
  Method: named pytest examples with a counting spy installed by
  `monkeypatch.setattr(inspect, "isawaitable", spy)`.
  Artefact: `cuprum/unittests/test_observe_hook_dispatch.py`.
  Evidence: red before EP-M3 (the spy counts one call per hook per event);
  green after.
  Non-vacuity: a companion test asserts the spy *is* called exactly once per
  event for a lambda returning a coroutine, proving the spy is wired into the
  real call site.
- V4. Classification happens once per execution.
  Obligation: emitting `n` line events through one `_StageObservation`
  classifies each hook exactly once, independent of `n`.
  Method: Hypothesis property over `n` in 0 to 200 with a spy on
  `cuprum._callable_kinds._is_async_callable`.
  Artefact: `cuprum/unittests/test_observe_hook_dispatch.py`.
  Non-vacuity: `n = 0` and `n ≥ 1` are both generated (`hypothesis.event`);
  a seeded fault that classifies inside the loop must fail.
- V5. End-to-end behaviour through a real subprocess.
  Obligation: a real command with line callbacks delivers every line to a sync
  hook, an `async def` hook, and a lambda returning a coroutine, and every
  scheduled task completes before `run_sync()` returns.
  Method: pytest-bdd scenarios.
  Artefact: `tests/features/observe_hook_dispatch.feature`,
  `tests/behaviour/test_observe_hook_dispatch.py`.
  Non-vacuity: each scenario asserts an exact count of received line events
  equal to the number of lines the command prints.
- V6. Absence in the profile (ROAD-5.2.2-S3).
  Obligation: under the per-line anchor, `isawaitable` in `inspect.py`
  receives zero sampled weight in every candidate capture.
  Method: three matched py-spy control/candidate capture pairs analysed by
  the frame-census command (EP-M4).
  Artefact: `docs/profiling/5-2-2-observe-hook-dispatch/`.
  Evidence: candidate `frame-census.json` reports `target_weight == 0` and the
  command exits 0; control reports `target_weight > 0` and exits 1.
  Non-vacuity: anchor weight at least 10,000 in every capture; control target
  weight non-zero in every pair; `variant.txt` proves which tree each capture
  imported.
- V7. Frame-census correctness.
  Obligation: the census command's anchor and target weights equal a
  brute-force recount over the parsed stacks.
  Method: Hypothesis property over generated folded stacks plus named examples
  for malformed input and the exit-code table.
  Artefact: `cuprum/unittests/test_hot_path_frame_census.py`.
  Non-vacuity: generated stacks include, in the same capture, stacks with the
  target outside the anchor (must not count), inside it (must count), and
  repeated within one stack (counted once).

Methods deliberately not used: syrupy snapshots (no new output format is
introduced; the existing `tests/behaviour/__snapshots__/test_structured_events.ambr`
must stay unchanged, which is itself evidence for C1); CrossHair (the
classifier is introspection over live callables, which symbolic execution
does not model usefully); Rust `proptest`, Kani, and Verus (Decision D6).

## Plan of work

Stage A (no code): expert review and approval of this plan.

Stage B (red): add V2-V5 tests and the V1 property, marked
`@pytest.mark.xfail(strict=True, reason="5.2.2 classified dispatch not yet
implemented")` where they depend on the new symbols; run them and record the
expected failures.

Stage C (green): implement EP-M1 and EP-M3, remove the markers, then EP-M4.

Stage D: collect profiles (EP-M5), write the evidence and documentation
(EP-M6), and run every gate.

### Production code shape

In `cuprum/_callable_kinds.py` (new, under 40 lines):

```python
def _is_async_callable(candidate: object) -> bool:
    """Return whether *candidate* is detectably asynchronous."""
    if inspect.iscoroutinefunction(candidate):
        return True
    return inspect.iscoroutinefunction(type(candidate).__call__)
```

`cuprum/_idle_heartbeat.py` imports it in place of `_is_async_callback`, which
is deleted.

In `cuprum/_observability.py`:

```python
class _ObserveHookSlot(typ.NamedTuple):
    """One observe hook paired with its once-computed dispatch path."""

    hook: ExecHook
    is_async: bool


def _classify_observe_hooks(
    hooks: tuple[ExecHook, ...],
) -> tuple[_ObserveHookSlot, ...]:
    """Pair each hook with whether it is detectably asynchronous."""
    return tuple(_ObserveHookSlot(hook, _is_async_callable(hook)) for hook in hooks)
```

`_emit_exec_event(dispatch: tuple[_ObserveHookSlot, ...], event)` keeps its
`try`/`except` block unchanged, then:

```python
        if result is None:
            continue
        if is_async and type(result) is types.CoroutineType:
            scheduled.append(_schedule_hook_result(result, event, len(scheduled)))
        elif inspect.isawaitable(result):
            scheduled.append(_schedule_hook_result(result, event, len(scheduled)))
```

where `_schedule_hook_result` holds today's `create_task` call and the
`observe_hook_task_scheduled` record verbatim. The two scheduling branches may
be merged into one condition if CodeScene prefers; the order of the tests is
the specification. Note that `result is None` skipping is equivalent to
today's behaviour because `inspect.isawaitable(None)` is false.

In `cuprum/_pipeline_types.py`, `_ExecutionHooks` gains

```python
    observe_dispatch: tuple[_ObserveHookSlot, ...] = dc.field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Classify observe hooks once for this execution."""
        object.__setattr__(
            self, "observe_dispatch", _classify_observe_hooks(self.observe_hooks)
        )
```

and `_StageObservation._emit_event` passes `self.hooks.observe_dispatch`.
`report_pipeline_wait` keeps iterating `self.hooks.observe_hooks`. No import
cycle is introduced: `cuprum/_pipeline_types.py:17` already imports
`_emit_exec_event` and `_ExecEventEmissionError` from `cuprum._observability`
at module level, and `_observability` does not import `_pipeline_types`.

## Milestones and plateaus

- EP-M1. Shared classifier extracted. `cuprum/_callable_kinds.py` exists,
  `_idle_heartbeat` uses it, `test_callable_kinds.py` (V2) passes, the wheel
  manifest snapshot is updated, and every gate passes. Requirements:
  ROAD-5.2.2-M (partial). Conformance check: no behaviour change; idle
  heartbeat tests unchanged and green. Recovery: revert the single commit.
  Remaining: dispatch. Compatibility decision: none (private helper renamed
  and moved together with its only caller).
- EP-M2. Red tests. V1, V3, V4, V5, and the observe rows of V2 committed with
  strict `xfail` markers; `make test` passes because the markers hold.
  Recovery: revert. Remaining: implementation.
- EP-M3. Classified dispatch. `_ObserveHookSlot`, `_classify_observe_hooks`,
  `_schedule_hook_result`, the new `_emit_exec_event` loop, and
  `_ExecutionHooks.observe_dispatch` land together with the updated direct
  callers in `test_cqrs_helpers.py` and `test_cqrs_hook_behaviour.py`; markers
  removed; all gates pass; `cs delta origin/main` reports no new finding.
  Requirements: ROAD-5.2.2-S1, -S2, -M. Conformance check: C1-C5 hold by V1,
  V3, V5 and the unchanged snapshot. Recovery: revert the commit; EP-M2's
  markers return the tree to green. Compatibility decision: none;
  `_emit_exec_event` is private and every caller is updated in the same
  commit.
- EP-M4. Frame census. `benchmarks/summarize_hot_path_frames.py` (command
  line) and its rules file format, reusing `parse_capture`, `Frame`, and
  `FramePattern` from `benchmarks/_line_event_profile_model.py`; V7 passes.
  The command takes `<stacks.folded> --rules <rules.json> --output <out.json>`;
  rules name `anchor_frames` and `target_frames` as `{function, location}`
  patterns plus `min_anchor_weight`; output records `total_weight`,
  `anchor_weight`, `target_weight`, `target_weight_outside_anchor`, and
  `status`; exit 0 when the target weight is zero and the anchor is
  sufficient, 1 when the target weight is non-zero, 2 when input is malformed
  or the anchor is insufficient. Requirements: ROAD-5.2.2-S3 (instrument).
  Recovery: revert.
- EP-M5. Profiler evidence. Three control/candidate pairs collected, analysed,
  and committed; evidence document written. Requirements: ROAD-5.2.2-S3.
  Conformance check: candidate zero, control non-zero, anchor sufficient,
  variants proven. Recovery: re-collect (idempotent; captures are overwritten
  per pair directory).
- EP-M6. Documentation and roadmap. Users' guide, design document,
  developers' guide, changelog, contents index, and roadmap updated; roadmap
  item 5.2.2 ticked with its citation corrected and D3's wording recorded.
  Recovery: revert.

## Concrete steps

Run everything from the worktree root. Use the `tee` logging convention from
`AGENTS.md`, for example:

```bash
make test 2>&1 | tee /tmp/test-cuprum-5-2-2-remove-the-per-hook-inspect-isawaitable-call.out
```

Red stage (EP-M2):

```bash
uv run pytest cuprum/unittests/test_observe_hook_dispatch.py \
  cuprum/unittests/test_observe_hook_dispatch_properties.py \
  cuprum/unittests/test_callable_kinds.py \
  tests/behaviour/test_observe_hook_dispatch.py -v
```

Expected: every new test reports `XFAIL` (or, for the BDD scenario that
already passes on today's dispatcher, `PASSED`, which is correct: V5 is a
regression guard, not a red test).

Green stage (EP-M3): rerun the same command; expect `PASSED` throughout and no
`XPASS(strict)`.

Profiling (EP-M5). Prepare a control worktree at the commit before EP-M3 and
the candidate at EP-M3's head:

```bash
git worktree add ../cuprum-5-2-2-control <EP-M2 head SHA>
```

Build the fixture exactly as 5.2.1 did (see
`docs/profiling/5-2-1-line-event-emission/README.md`), producing
`dist/fixtures/seed12345-wrap76.b64`. For each round `r` in 1, 2, 3, alternate
which variant runs first, and capture from inside the variant's worktree so
`python -m` imports that tree's `cuprum`:

```bash
py-spy record --format raw --rate 100 \
  --output <capture>/stacks.folded -- \
  <python> -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 --mode echo \
  --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 1 --read-size 65536 --output <capture>/worker-result.json
python -m benchmarks.summarize_hot_path_frames <capture>/stacks.folded \
  --rules docs/profiling/5-2-2-observe-hook-dispatch/frame-census-rules.json \
  --output <capture>/frame-census.json
```

Record `py-spy` and census exit codes, the revision, and the variant probe:

```bash
python -c "import cuprum, cuprum._observability as o; print(cuprum.__file__, hasattr(o, '_classify_observe_hooks'))"
```

A non-zero py-spy exit with `No child process (os error 10)` after `Errors: 0`
is benign (5.2.1 evidence, around lines 167-172). Then run five unprofiled
paired rounds of the `cb` scenario (`echo-devnull-cb-s1`) as in 5.2.1 and
record medians.

## Validation and acceptance

Acceptance is met when all of the following hold:

- `make check-fmt`, `make typecheck`, `make lint`, `make test`, `make
  markdownlint`, and `make nixie` pass at the final head.
- `cuprum/unittests/test_observe_hook_dispatch.py::test_sync_none_results_never_reach_isawaitable`
  failed before EP-M3 (as a strict `xfail`) and passes after.
- The V1 differential property passes with every class reported in its
  statistics, and both seeded-fault tests pass by observing a mismatch.
- `tests/behaviour/__snapshots__/test_structured_events.ambr` is unchanged
  (`git diff --exit-code origin/main -- tests/behaviour/__snapshots__/`).
- Each `docs/profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-candidate/frame-census.json`
  reports `"target_weight": 0` with census exit 0, and each control reports a
  non-zero target weight with census exit 1; every anchor weight is at least
  10,000.
- No unprofiled scenario's candidate median is more than 5% slower than its
  control.

Quality criteria: tests and gates above; V1-V7 discharged; no new dependency;
no change to public API.

## Idempotence and recovery

Every milestone is one or a few commits that can be reverted independently in
reverse order. Profiling is re-runnable; each pair directory is overwritten on
re-collection, and nothing under `dist/` is committed. Remove the control
worktree with `git worktree remove ../cuprum-5-2-2-control` when EP-M5 is
complete.

## Artefacts and notes

Classification probe (2026-10-01), abridged; identical results on 3.12.13,
3.13.13, 3.14.4, and 3.15.0b2:

```plaintext
async def                              iscorofn=True  heartbeat_rule=True  returns_awaitable=True
sync def                               iscorofn=False heartbeat_rule=False returns_awaitable=False
sync def returning coroutine           iscorofn=False heartbeat_rule=False returns_awaitable=True
lambda returning coroutine             iscorofn=False heartbeat_rule=False returns_awaitable=True
partial(async def)                     iscorofn=True  heartbeat_rule=True  returns_awaitable=True
partial(bound async method)            iscorofn=True  heartbeat_rule=True  returns_awaitable=True
bound async method                     iscorofn=True  heartbeat_rule=True  returns_awaitable=True
instance async __call__                iscorofn=False heartbeat_rule=True  returns_awaitable=True
instance sync __call__                 iscorofn=False heartbeat_rule=False returns_awaitable=False
markcoroutinefunction                  iscorofn=True  heartbeat_rule=True  returns_awaitable=True
functools.wraps sync wrapper of async  iscorofn=False heartbeat_rule=False returns_awaitable=True
3.12.13: isawaitable(None) 481.1 ns ; 'is None' 13.6 ns
3.14.4:  isawaitable(None) 233.8 ns ; 'is None' 11.0 ns
```

Control-capture baseline: in
`docs/profiling/5-2-1-line-event-emission/r2-candidate/stacks.folded`,
`isawaitable (inspect.py:…)` leaves under `emit_line` total 589 samples
(5.2.1 evidence, around line 333).

## Interfaces and dependencies

No new dependency. New private symbols, all module-private and unexported:

- `cuprum._callable_kinds._is_async_callable(candidate: object) -> bool`.
- `cuprum._observability._ObserveHookSlot` (`NamedTuple` of `hook: ExecHook`,
  `is_async: bool`).
- `cuprum._observability._classify_observe_hooks(hooks: tuple[ExecHook, ...])
  -> tuple[_ObserveHookSlot, ...]`.
- `cuprum._observability._schedule_hook_result(result, event, count) ->
  asyncio.Task[None]`.
- `cuprum._observability._emit_exec_event(dispatch: tuple[_ObserveHookSlot,
  ...], event: ExecEvent) -> list[asyncio.Task[None]]` (signature change from
  `hooks: tuple[ExecHook, ...]`).
- `cuprum._pipeline_types._ExecutionHooks.observe_dispatch` (derived,
  `init=False`).
- `benchmarks.summarize_hot_path_frames` (command line and `summarize(...)`
  function).

## Revision note

2026-10-01: initial draft from reconnaissance and the classification probe;
awaiting expert review.
