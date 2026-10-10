# Remove the per-hook `inspect.isawaitable` call from per-line event dispatch

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT (awaiting approval, including the choice recorded in Decision D1)

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
this dispatcher spends a measurable share of the per-line time asking
`inspect.isawaitable(result)` of every hook's return value, although almost
every hook is an ordinary function that returns `None`. In the post-5.2.1
captures the call and the `abc.__instancecheck__` work beneath it weigh 787 to
1,065 py-spy samples per capture under the per-line emitter, about 4.6% of the
consume samples, or roughly 0.29 µs per line.

After this change, the dispatcher stops paying that cost for the shapes that
dominate real use. A hook result of `None` — the only result a synchronous hook
ever returns — is dismissed by one identity comparison, and a native coroutine
— the only result an `async def` hook ever returns — is recognized by one
exact-type comparison. Only a result that is neither (an `asyncio.Future`, an
object with `__await__`, a generator-based coroutine, or a non-awaitable value
returned against the type) reaches the unchanged `inspect.isawaitable` test.
Every hook shape that works today keeps working identically, including a plain
function or lambda that returns a coroutine.

A user observes three things:

1. Event payloads, hook ordering, failure semantics, and async-hook
   scheduling are unchanged; the existing behavioural and snapshot suites pass
   unmodified.
2. A committed py-spy capture of the line-callback scenario shows
   `inspect.isawaitable` contributing exactly 0 sampled frames under the
   per-line emitter, while a matched control capture of the pre-change tree
   shows it contributing hundreds.
3. The line-callback scenario is no slower and is expected to be a few per
   cent faster; the measured change is reported with a confidence interval
   rather than promised.

## Constraints

These are hard invariants. Violating one requires escalation, not a workaround.

- C1. Observable event semantics must not change. Every `ExecEvent` emitted
  for every phase keeps the same field values, the same per-hook delivery
  order, and the same per-line freshness (one new event and one clock read per
  delivered line, as established by 5.2.1).
- C2. Async-hook scheduling must be identical for every hook result that is
  awaitable today: the same `asyncio.create_task` call shape, the same task name
  `cuprum.observe.<phase>`, the same `_await_awaitable` wrapper and its
  `observe_hook_task_started`, `observe_hook_task_failed`, and
  `observe_hook_task_finished` records, the same `observe_hook_task_scheduled`
  DEBUG record with the same message arguments and `extra` *values* (including
  `cuprum_scheduled_task_count`, which counts the task just appended), and the
  same position in the returned task list.
- C3. Failure semantics must not change. A hook raising `CancelledError` is
  wrapped in `_ExecEventEmissionError` with no log; any other `BaseException`
  is logged as `observe_hook_failed` with the same `extra` values and wrapped;
  both carry exactly the tasks scheduled by earlier hooks in the same emission
  (the *scheduled prefix*). An exception raised by the awaitable check itself
  (possible today, see `Surprises & discoveries`) keeps escaping unwrapped from
  outside the `try` block; fixing that is out of scope.
- C4. The public observe-hook contract in `docs/users-guide.md` ("Awaitable
  hook results are scheduled as `asyncio.Task` instances and awaited before the
  run completes") remains true for every callable that satisfies the `ExecHook`
  alias in `cuprum/events.py:296`, including plain callables that return an
  awaitable. This plan must not narrow that contract.
- C5. Public surfaces stay stable: `cuprum.observe`, `HookRegistration`,
  `CuprumContext.observe_hooks`, `ScopeConfig.observe_hooks`, the `ExecHook`
  alias, and the identity of the hook objects stored in those tuples
  (detach-by-identity in `CuprumContext.without_observe_hook` and the
  `_PipelineWaitReporter` `isinstance` dispatch in
  `_StageObservation.report_pipeline_wait` depend on it).
- C6. No Rust, native-extension, dependency, or packaging-metadata change.
  Phase 5 is deliberately Rust-free (roadmap phase 5 "Idea").
- C7. The per-line path keeps routing through
  `_StageObservation._emit_event`, which owns `pending_tasks` (see the
  `_line_event_emitter` docstring in `cuprum/_line_callbacks.py`).
- C8. Production and benchmark modules stay at or under 400 lines.
  `cuprum/_observability.py` is 259 lines;
  `benchmarks/_line_event_profile_model.py` is 397 lines and must not grow.
  Test modules that are already over the cap
  (`cuprum/unittests/test_cqrs_helpers.py` at 449 and
  `cuprum/unittests/test_cqrs_hook_behaviour.py` at 444) must not grow.
- C9. Every quality gate in `AGENTS.md` passes before each commit:
  `make check-fmt`, `make typecheck`, `make lint`, `make test`, and for
  Markdown edits `make markdownlint` and `make nixie`.
- C10. The 5.2.1 construction-share gate is not an acceptance criterion for
  this item. Removing work outside `ExecEvent.__init__` raises that share *by
  succeeding* (`docs/developers-guide.md`, "Construction-share classification
  (roadmap 5.2.1)"). The re-run in EP-M5 replaces the 5.2.1 forecast with a
  measurement; a value above 30% is the documented inversion, not a regression.

## Tolerances (exception triggers)

- Scope (Option A): stop and escalate if production changes touch any module
  other than `cuprum/_observability.py`, or exceed 30 net production lines.
  (Option B, if chosen: four modules and 120 net lines.)
- Interface: stop if any public signature, public attribute, or the contents
  of `CuprumContext.observe_hooks` / `ScopeConfig.observe_hooks` would need to
  change.
- Contract: stop if any existing test's *assertions* must change to pass, or
  if any committed syrupy snapshot changes.
- Measurement: stop if, after two complete collection attempts, any candidate
  capture shows a non-zero `isawaitable` weight under the anchor, or any
  capture's anchor weight is below the derived floor of 14,000 samples (see V6).
- Performance: stop if the upper bound of the 95% confidence interval for the
  `cb` scenario's candidate/control wall-time ratio exceeds 1.05.
- Iterations: stop if a red test cannot be made green within three attempts
  without breaching another tolerance.
- Dependencies: stop if any new runtime or development dependency appears to
  be required.
- Ambiguity: stop if implementation evidence shows that any hook shape the
  users' guide supports today would be scheduled differently.

## Risks

- R1. The roadmap's mechanism clause ("by classifying each hook as sync or
  async once at registration") cannot be implemented both literally and
  soundly. Severity: high. Likelihood: certain. A classifier that *decides*
  scheduling drops the coroutines returned by lambdas, plain wrappers, and
  `functools.wraps` decorators (probe in `Artefacts and notes`); a classifier
  that merely *hints* changes no behaviour and no cost that a result test
  cannot change more cheaply (expert review, `Decision log`). Mitigation:
  Decision D1 puts the choice to the approver, with Option A recommended.
- R2. A future maintainer "optimizes" the result test into a hook test,
  silently dropping coroutines returned by decorated hooks. Severity: high.
  Likelihood: low. Mitigation: the named invariant test
  `test_sync_callable_returning_coroutine_is_still_scheduled` and the
  behavioural scenario outline both pin the shape, with docstrings that state
  the rule; `docs/cuprum-design.md` §8.1.3 records it.
- R3. The profile proves zero by matching nothing (the target frame renders
  differently, for example with a full `inspect.py` path, or the anchor moves).
  Severity: high. Likelihood: low. Mitigation: the census reports both a
  location-qualified and a name-only target weight and refuses a result where
  they differ; the control capture must show a non-zero target under the same
  anchor; one pinned interpreter runs every capture and `sys.version` is
  recorded.
- R4. Variant mix-up during profiling. Severity: high. Likelihood: low.
  Mitigation: each capture records `cuprum.__file__` and `git rev-parse HEAD`
  of the directory that holds the imported package in `variant.txt`.
- R5. The unprofiled timing comparison is noisy (5.2.1 `cb` runs ranged
  167.5-210.9 s, about ±12%) against an expected gain of about 4.5%. Severity:
  medium. Likelihood: high. Mitigation: ABBA ordering, a discarded warm-up, a
  paired geometric-mean ratio with a bootstrap 95% interval, and an in-process
  microbenchmark as the precise per-event figure.
- R6. CodeScene or pylint complexity on `_emit_exec_event`. Severity: medium.
  Likelihood: low under Option A (one extra early `continue`). Mitigation: keep
  the scheduling block's shape, and run `cs delta origin/main` before pushing.

## Progress

- [x] (2026-10-01) Branch `5-2-2-remove-the-per-hook-inspect-isawaitable-call`
  created from `main` at `71aaf3eb`; the remote branch did not exist yet.
- [x] (2026-10-01) Reconnaissance: hook registration and dispatch paths,
  tests, documentation contract, and 5.2.1 profiling method surveyed.
- [x] (2026-10-01) Classification probe run on CPython 3.12-3.15.
- [x] (2026-10-01) First draft written (classification as a fast-path
  selector).
- [x] (2026-10-01) Expert design review (three panels) completed; plan
  revised to recommend Option A and correct the measurement plan.
- [ ] Plan approved, with Option A or Option B chosen.
- [ ] EP-M1: red tests committed.
- [ ] EP-M2: result-guarded dispatch implemented; red tests green.
- [ ] EP-M3: frame-census command implemented and tested.
- [ ] EP-M4: dispatch microbenchmark added.
- [ ] EP-M5: profiler and timing evidence collected; evidence document
  written.
- [ ] EP-M6: documentation, changelog, and roadmap updated; item marked done.

## Surprises & discoveries

- Observation: static classification cannot identify every hook that returns
  an awaitable. Evidence: probe output in `Artefacts and notes`, identical on
  CPython 3.12.13, 3.13.13, 3.14.4, and 3.15.0b2. A lambda or plain function
  returning a coroutine and a `functools.wraps` sync wrapper of an `async def`
  are invisible to both `inspect.iscoroutinefunction(hook)` and the
  `type(hook).__call__` rule in `cuprum/_idle_heartbeat.py:356`. The expert
  review added that `unittest.mock.create_autospec(async_fn)` classifies as
  async on 3.13 and 3.14 but not on 3.12. Impact: classification can at best
  select a fast path; Decision D1.
- Observation: a sound classification removes no work that the result does
  not already reveal. `type(result) is types.CoroutineType` is the first test
  `inspect.isawaitable` itself makes, `types.CoroutineType` cannot be subclassed
  (`TypeError: type 'coroutine' is not an acceptable base type`), and `None`
  is never awaitable. Evidence: structure-and-contracts review probe, 14 result
  shapes crossed with both classifications, all agreeing with today's
  `inspect.isawaitable`. Impact: Option A.
- Observation: measured per-event dispatch cost, one hook, median of 15
  pinned rounds (ns): today 422 (3.12) / 353 (3.14) for a sync hook returning
  `None` and 265 / 232 for an `async def` hook; Option A 123 / 99.6 and 241 /
  205; the draft's `NamedTuple`-slot classifier 173 / 136 and 286 / 246 (slower
  than today on the async path, because unpacking a tuple subclass misses
  CPython's exact-tuple fast path); plain-tuple slots 129 / 103 and 247 / 217.
  Evidence: alternatives-and-cost review microbenchmarks. Impact: Option A is
  the fastest variant measured; Option B, if chosen, must use plain tuples.
- Observation: `inspect.isawaitable` can raise (a result whose `__class__`
  property raises propagates it). Today the check sits outside the `try` block
  at `cuprum/_observability.py:132`, so such an exception escapes unwrapped and
  the scheduled prefix never reaches `pending_tasks`. Impact: preserved
  deliberately (C3) and pinned by a V1 class; record as a candidate follow-up
  issue rather than changing behaviour here.
- Observation: in Option B, `inspect.iscoroutinefunction(hook)` raises for a
  proxy hook whose `__getattr__` raises, although calling the hook works.
  Classifying in `_collect_hooks` would fail every command in the scope before
  the `plan` event, unlogged and unwrapped. Impact: Option B must catch and
  fall back to the guarded path.
- Observation: the 5.2.1 figure of 589 samples counts `isawaitable` leaf
  frames only; counting every stack that contains the frame gives 787-1,065 per
  capture (828 in r2-candidate; the remainder sits in `abc.__instancecheck__`).
  The forecast inversion is therefore larger than the 30.9686% recorded in
  5.2.1: 5,317 / (17,758 − 828) ≈ 31.41%. Impact: the evidence document must
  not compare census output with 589, and EP-M5 replaces the forecast with a
  measurement.
- Observation: the per-line anchor `emit_line (cuprum/_line_callbacks.py)`
  weighs only 9,142, 9,318, and 9,173 samples in the 5.2.1 candidate captures
  (this plan's control). The "D ≥ 10,000" figure in 5.2.1 is the whole consume
  region (about 17,400), not this anchor. Impact: captures use
  `--repeat-count 2`, and the floor is derived (V6).
- Observation: every `isawaitable` sample in all six 5.2.1 captures sits under
  `emit_line (cuprum/_line_callbacks.py)` via `_emit_exec_event`, with none
  outside. A second `emit_line` exists in `cuprum/_stream_line_consumer.py:39`,
  and the `inspect.py` line number varies (366-371) by interpreter. Impact: the
  anchor pattern includes the location; the target pattern omits the line
  number.
- Observation: the roadmap's citation `cuprum/_observability.py:35` is stale;
  the call is at line 132 in `_emit_exec_event` (line 88). Impact: correct it
  when the roadmap entry is ticked.
- Observation: the 5.2.1 collection script was never committed, and
  `benchmarks/summarize_folded.py` cannot prove absence (it keys on full frame
  text including line numbers, has no anchor filter, and truncates to
  `--limit`). Impact: EP-M3 adds a small census command and this plan restates
  every capture command.
- Observation: `observe_hook_task_scheduled` builds its `extra` dictionary and
  calls `str(event.program)` for every scheduled line even when DEBUG logging
  is disabled; on the async path that likely costs more than the call this item
  removes. Impact: out of scope; record as a candidate roadmap follow-up in
  EP-M6.

## Decision log

- Decision D1 (requires approver choice): recommend **Option A**, a
  result-guarded dispatcher with no per-hook classification, over **Option B**,
  a per-execution classification used only as a speed hint. Option A replaces
  the `inspect.isawaitable(result)` test in `_emit_exec_event` with: skip a
  `None` result; schedule a result whose type is exactly `types.CoroutineType`;
  otherwise schedule if `inspect.isawaitable(result)`. Rationale: all three
  review panels found independently that a sound classification cannot remove
  any work the result does not already reveal, while adding a new failure point
  (a classifier that raises on proxy hooks), a new module, a private signature
  change, edits to two over-cap test files, and a field that invites a future,
  unsound "optimization" (R2). Option A satisfies success clauses S1
  ("known-sync hooks dispatch without per-line awaitable detection" — a
  synchronous hook's result is `None` and never reaches detection), S2
  (scheduling is identical for every result shape), and S3, and it is the
  fastest variant measured. It departs from mechanism clause M: the roadmap
  entry is to be reworded on completion to "by dismissing `None` results and
  recognizing native coroutines by exact type before awaitable detection".
  Option B, if the approver requires clause M literally, is specified under
  `Option B deltas`; it narrows no contract, so it still needs no ADR, but it
  must document `is_async` as a hint that never decides whether a result is
  scheduled. Date/Author: 2026-10-01, planning agent after expert review;
  awaiting the user.
- Decision D2: test native coroutines by exact type
  (`type(result) is types.CoroutineType`), not with `asyncio.iscoroutine`.
  Rationale: `asyncio.iscoroutine(None)` measured 610 / 516 ns (it caches only
  positive types) and accepts any registered `collections.abc.Coroutine`, which
  is broader than needed; the exact-type test costs about 17-21 ns, is the
  first branch inside `inspect.isawaitable`, and so can never disagree with it.
  Date/Author: 2026-10-01, planning agent.
- Decision D3: keep the awaitable check outside the `try` block and keep the
  scheduling block's logging inline, with the scheduled count read *after* the
  append. Rationale: C2 and C3. The first draft's
  `_schedule_hook_result(result, event, len(scheduled))` would have logged the
  count before the append, an off-by-one in `cuprum_scheduled_task_count`.
  Option A needs no helper. Date/Author: 2026-10-01, planning agent.
- Decision D4: write the dispatch property (V1) against a *specification*
  oracle, not a verbatim copy of the old dispatcher. Rationale: a frozen copy
  rots and duplicates item 5.2.3's parity suite. The specification is short and
  already used in `cuprum/unittests/test_cqrs_hook_behaviour.py:226-262`: up to
  the first failing hook, exactly the hooks whose result satisfies
  `inspect.isawaitable` are scheduled, in order. Item 5.2.3's plan should name
  V1 as the property it extends. Date/Author: 2026-10-01, planning agent.
- Decision D5: no ADR. Option A changes no contract and is recorded in
  `docs/cuprum-design.md` §8.1.3. Option B also narrows nothing. Only the
  literal, contract-narrowing reading (never inspect a sync-classified hook's
  result) would warrant ADR 019, and this plan rejects it under C4.
  Date/Author: 2026-10-01, planning agent.
- Decision D6: no Rust, Verus, Kani, or `proptest` work, and no CrossHair.
  Rationale: the obligations concern CPython result types and asyncio
  scheduling, which a Rust extension could only model by moving the hot path
  across the foreign function interface, contrary to C6; the dispatcher is
  impure (it schedules tasks and logs), which CrossHair does not model
  usefully. Hypothesis over a specification oracle is the proportionate
  instrument. Date/Author: 2026-10-01, planning agent.
- Decision D7: add a dedicated census command
  (`benchmarks/summarize_hot_path_frames.py`) with flags rather than a rules
  file, reusing `parse_capture` and `FramePattern` from
  `benchmarks/_line_event_profile_model.py` without modifying that module.
  Rationale: the 5.2.1 classifier computes a share with a hard-wired limit,
  `summarize_folded` cannot prove absence, and the model module is at 397
  lines. Scope and reuse policy: the command answers only "how much sampled
  weight does a target frame carry inside stacks containing an anchor frame?";
  it must not grow share or verdict logic. Date/Author: 2026-10-01, planning
  agent.
- Decision D8: do not profile an async-hook line-callback scenario at full
  scale. Rationale: finished observe tasks stay in `pending_tasks` until the
  run ends (`cuprum/_command_internals.py:251`), at about 813 bytes each; the
  28-million-line fixture would need roughly 23 GB. The exact-type fast path
  saves about 25 ns of a roughly 4,000 ns async path, so a profile would show
  "unchanged", which V1 and the microbenchmark already establish. Date/Author:
  2026-10-01, planning agent.

## Outcomes & retrospective

Not yet started. Record the measured census weights, the timing ratio and its
interval, the microbenchmark figures, and the re-measured construction share
here at each milestone.

## Context and orientation

Cuprum is a Python library for running allow-listed subprocesses with typed
commands, structured events, and optional Rust acceleration of stream pumping.
Everything this plan touches is pure Python under `cuprum/`, `benchmarks/`,
`tests/`, and `docs/`.

Terms used below:

- *Observe hook*: a callable of type
  `ExecHook = Callable[[ExecEvent], Awaitable[None] | None]`
  (`cuprum/events.py:296`), registered with `cuprum.observe(hook)`
  (`cuprum/context/registration.py:340`) or supplied in
  `ScopeConfig.observe_hooks` (`cuprum/context/_scope.py:91`), and stored as a
  plain tuple on `CuprumContext.observe_hooks` (`cuprum/context/core.py:74`).
- *Execution hooks*: `_ExecutionHooks` (`cuprum/_pipeline_types.py:66`), a
  frozen, slotted private dataclass of the before, after, and observe hook
  tuples for one command or pipeline stage, built by `_collect_hooks`
  (`cuprum/_pipeline_internals.py:87`).
- *Stage observation*: `_StageObservation` (`cuprum/_pipeline_types.py`) owns
  one stage's hooks and its `pending_tasks` list. Its `_emit_event` method
  (around line 220) is the only production caller of `_emit_exec_event`.
- *Dispatcher*: `_emit_exec_event(hooks, event)`
  (`cuprum/_observability.py:88`). For each hook in order it calls the hook
  inside a `try` block that wraps failures in `_ExecEventEmissionError`
  together with the tasks scheduled so far; then, outside the `try`, if
  `inspect.isawaitable(result)` (line 132), it appends
  `asyncio.create_task(_await_awaitable(result, event.phase),
  name=f"cuprum.observe.{event.phase}")`
  and logs `observe_hook_task_scheduled`.
- *Per-line hot path*: `_LineEventEmitter.emit_line`
  (`cuprum/_line_callbacks.py`, around line 139), built once per observed
  stream by `_line_event_emitter` in 5.2.1, calls the bound
  `_StageObservation._emit_event` once per delivered line.
- *py-spy raw capture* (`stacks.folded`): one line per distinct stack, frames
  separated by `;`, each frame rendered `function (path:line)`, followed by a
  space and an integer sample weight.
- *Anchor*: a frame whose presence marks a stack as belonging to the per-line
  hot path; here `emit_line` in `cuprum/_line_callbacks.py`.
- *Target*: the frame whose weight inside anchored stacks must be zero; here
  `isawaitable` in `inspect.py`. A stack's whole weight counts once if the
  target appears anywhere in it ("inclusive" weight).

Related prior work: item 5.2.1 hoisted the invariant event fields; its plan
(`docs/execplans/5-2-1-hoist-the-invariant-exec-event-and-event-details.md`),
evidence (`docs/tee-hotpath-line-event-emission-5-2-1.md`), and captures
(`docs/profiling/5-2-1-line-event-emission/`) define the profiling method
reused here. Item 5.2.3 will add a combinatorial event-parity suite over hook
type and stream mode; V1 is scoped to the dispatcher so that 5.2.3 can extend
it rather than duplicate it.

### Documentation and skills signposts

Read before starting:

- `AGENTS.md` (quality gates, file-size cap, commit rules).
- `docs/roadmap.md` §5 and §5.2.
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, Table 4.
- `docs/tee-hotpath-line-event-emission-5-2-1.md` (method and inversion).
- `docs/profiling/5-2-1-line-event-emission/README.md` (artefact layout).
- `docs/cuprum-design.md` §7.1 and §8.1.3 (event model, async observers).
- `docs/users-guide.md`, the observe-hook section (around lines 960-1062)
  and "When an observe hook raises" (around lines 1109-1129).
- `docs/developers-guide.md`: "Choosing a test shape per observe hook"
  (around line 1160), "Profiling harness overview" (around line 2961),
  "Construction-share classification (roadmap 5.2.1)" (around line 3380), and
  "Line observation" (around line 5729).
- `docs/adr-002-additional-rust-components.md` (line-callback cost is a
  Python concern) and `docs/adr-008-rust-pump-observation-channel.md` (the
  separate, synchronous pump-hook channel; do not conflate it with observe
  hooks).
- `docs/documentation-style-guide.md` and `docs/scripting-standards.md`.
- `.rules/python-00.md`, `.rules/python-typing.md`, and
  `.rules/python-exception-design-raising-handling-and-logging.md`.

Skills to load: `execplans` (this plan); `python-router`, then `python-testing`
and `hypothesis`; `python-quality-tools` for py-spy work; `codegraph-mcp` for
caller and impact queries; `en-gb-oxendict` for prose; `commit-message` and
`pr-creation` for delivery; `firecrawl-mcp` for any external documentation
lookup. `rust-router` was consulted and is not needed (Decision D6).

## Conformance basis

Upstream artefacts at `main` revision `71aaf3eb`:

- `docs/roadmap.md` item 5.2.2 (ROAD-5.2.2), with success clauses
  ROAD-5.2.2-S1 ("known-sync hooks dispatch without per-line awaitable
  detection"), ROAD-5.2.2-S2 ("async hooks retain identical scheduling
  behaviour"), and ROAD-5.2.2-S3 ("a committed profiler artefact shows
  `inspect.isawaitable` contributes 0 sampled frames in the per-line hot
  path"), and mechanism clause ROAD-5.2.2-M ("by classifying each hook as sync
  or async once at registration").
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, Table 4 (BASE-T4).
- `docs/cuprum-design.md` §8.1.3 "Async observers" (DES-8.1.3-ASYNC).
- `docs/users-guide.md` observe-hook contract (UG-OBS-AWAIT).
- `docs/adr-002-additional-rust-components.md` (ADR-002).
- No Terms of Reference document exists for this phase.

Trace links (Option A):

```plaintext
BASE-T4 -> ROAD-5.2.2-S1 -> D1(A)/D2 -> EP-M2
  -> test_observe_hook_dispatch::test_none_results_never_reach_isawaitable
  -> test_observe_hook_dispatch::test_native_coroutines_never_reach_isawaitable
ROAD-5.2.2-S2 + UG-OBS-AWAIT + DES-8.1.3-ASYNC -> D1(A)/D2/D3/D4 -> EP-M2
  -> test_observe_hook_dispatch_properties::test_dispatch_matches_specification
  -> test_observe_hook_dispatch::test_sync_callable_returning_coroutine_is_still_scheduled
  -> tests/features/observe_hook_dispatch.feature
ROAD-5.2.2-S3 -> D7 -> EP-M3, EP-M5
  -> docs/profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/frame-census.json
ROAD-5.2.2-M -> D1 -> deviation (Option A) or EP-M2 Option B deltas
```

Deviation recorded for approval: under Option A, clause ROAD-5.2.2-M is not
implemented as written; the outcome clauses S1-S3 are. See Decision D1.

## Verification plan

Option A introduces one invariant (dispatch equivalence over every result
shape) and one measurable absence (no `isawaitable` frame under the per-line
anchor). It introduces no lemma requiring a formal proof (Decision D6).

Axioms relied upon, not verified here:

- A1. A call to an `async def` function (including bound methods,
  `functools.partial` wrappers, `AsyncMock`, and `async def __call__`) returns
  an object of exact type `types.CoroutineType`, and that type cannot be
  subclassed. Exercised by V1's classes on every interpreter in the CI matrix
  (3.12-3.15).
- A2. `inspect.isawaitable(x)` returns true whenever
  `type(x) is types.CoroutineType`, and returns false for `None`, on every
  supported interpreter (its first branch is the coroutine-type test).
- A3. py-spy 0.4.2 in raw mode attributes each sample to every Python frame
  on the sampled stack. The control capture is the empirical check.
- A4. `asyncio.create_task` scheduling order equals call order within one
  emission.

Obligations:

- V1. Dispatch matches its specification.
  Obligation: for every finite sequence of hooks drawn from the classes below
  and every phase, `_emit_exec_event(hooks, event)` (a) calls hooks in order up
  to and including the first raising hook; (b) schedules, in order, exactly
  those earlier hooks whose result satisfies `inspect.isawaitable` (evaluated
  by the test on an identical, separately produced result); (c) names each task
  `cuprum.observe.<phase>`; (d) raises `_ExecEventEmissionError` carrying
  exactly that scheduled prefix and wrapping the original exception object, or
  returns the list when nothing raises; and (e) emits the same log records as
  today, compared on logger name, level, `getMessage()`, `extra` values, and
  `exc_info` type. Method: Hypothesis property with a specification oracle
  (Decision D4). Domain: sequences of length 0 to 8 drawn from: sync returning
  `None`; sync returning a non-awaitable non-`None` value; plain function
  returning a coroutine; lambda returning a coroutine; `functools.wraps` sync
  wrapper of an `async def`; `async def`; `functools.partial` of an
  `async def`; bound async method; instance with `async def __call__`; instance
  with sync `__call__`; `AsyncMock`; `create_autospec` of an `async def`;
  `markcoroutinefunction`-marked function returning `None`; hook returning an
  `asyncio.Future`; hook returning a `@types.coroutine` generator; hook
  returning an object with `__await__`; hook returning a `Mock` and a
  `MagicMock`; hook raising `Exception`; hook raising a non-`Exception`
  `BaseException`; hook raising `CancelledError`; and hook returning an object
  whose awaitable check raises (pinning C3's unwrapped escape). Phases are
  sampled from `plan`, `start`, `stdout`, and `exit`. Artefacts:
  `cuprum/unittests/test_observe_hook_dispatch_properties.py`, with hook
  factories in `cuprum/unittests/_observe_hook_dispatch_support.py`. Evidence:
  `uv run pytest cuprum/unittests/test_observe_hook_dispatch_properties.py -v`
  passes both before and after EP-M2 (it is a preservation property, not a red
  test), and fails against each seeded fault below. Non-vacuity: every class is
  tagged with `hypothesis.event(...)`; one run with
  `--hypothesis-show-statistics` is recorded in `Artefacts and notes` and must
  show every class; `@example`s pin one sequence per class and one where a
  raising hook follows two scheduling hooks. Two seeded-fault tests run the
  property body against deliberately wrong dispatchers and must observe a
  mismatch: `test_specification_rejects_every_non_none_scheduled` (rejected by
  the non-awaitable-value class) and
  `test_specification_rejects_native_coroutines_only` (rejected by the `Future`,
  `@types.coroutine`, and `__await__` classes). Seeded faults close any
  coroutine they drop, so no unraisable `RuntimeWarning` leaks into later tests.
- V2. Known-shape results never reach awaitable detection (ROAD-5.2.2-S1).
  Obligation: for hooks returning `None` and hooks returning native coroutines,
  emitting any number of events makes zero calls to `inspect.isawaitable` from
  `_emit_exec_event`. Method: named pytest examples with a counting spy
  installed by `monkeypatch.setattr(inspect, "isawaitable", spy)` that counts
  only calls whose immediate caller's code object is
  `cuprum._observability._emit_exec_event.__code__`
  (`sys._getframe(1).f_code`), so asyncio or pytest plugin calls cannot inflate
  or mask the count. Artefact:
  `cuprum/unittests/test_observe_hook_dispatch.py`, tests
  `test_none_results_never_reach_isawaitable` and
  `test_native_coroutines_never_reach_isawaitable`. Evidence: red before EP-M2
  under `xfail(strict=True)` (the spy counts one call per hook per event
  against the existing signature, so the module imports cleanly and the failure
  is an assertion, not a collection error); green after, with the marker
  removed. Non-vacuity: `test_awaitable_objects_still_reach_isawaitable`
  asserts the spy *is* called exactly once per event for a hook returning an
  `asyncio.Future`, proving the spy sees the real call site.
- V3. Shapes that classification cannot see are still scheduled (guards R2).
  Obligation: a plain function, a lambda, and a `functools.wraps` wrapper that
  return coroutines are scheduled exactly as an `async def` hook is. Method:
  named pytest example, parameterized over the three shapes, whose docstring
  states the rule. Artefact:
  `cuprum/unittests/test_observe_hook_dispatch.py::test_sync_callable_returning_coroutine_is_still_scheduled`.
  Non-vacuity: asserts the task count, task names, and that each coroutine ran
  to completion (a side-effect flag), with `gc.collect()` inside
  `warnings.catch_warnings(record=True)` showing no "never awaited" warning.
- V4. End-to-end behaviour through a real subprocess.
  Obligation: a real command with observed line events delivers every line to
  every supported hook shape, and every scheduled task completes before
  `run_sync()` returns. Method: pytest-bdd scenario outline (specification
  below). Artefacts: `tests/features/observe_hook_dispatch.feature` and
  `tests/behaviour/test_observe_hook_dispatch.py` (collected by the
  `test_[i-r]*.py` glob in `PYTEST_TARGETS`), reusing
  `tests/behaviour/_structured_events_support.py`. A new feature is used
  because extending `structured_events.feature` would push
  `tests/behaviour/test_structured_events.py` (353 lines) past the cap.
  Evidence: passes before and after EP-M2; it is a regression guard.
  Non-vacuity: each example asserts an exact count of received line events
  equal to the number of lines printed, in print order.
- V5. Absence in the profile (ROAD-5.2.2-S3).
  Obligation: under the anchor, the target carries zero inclusive weight in
  every candidate capture. Method: three matched py-spy control/candidate pairs
  analysed by the census command. Artefacts:
  `docs/profiling/5-2-2-observe-hook-dispatch/`. Evidence: each candidate's
  `frame-census.json` reports `target_weight` 0 and
  `target_weight_any_location` 0, and the census exits 0; each control reports
  both weights equal and non-zero (expected in the high hundreds per repeat)
  and exits 1. Non-vacuity and bound: the floor is derived, not assumed. If the
  target survived at a fraction f of anchored samples, the chance of observing
  zero in N samples is (1 − f)^N. Requiring that to be below 10⁻⁶ for f = 0.1%
  (one hundredth of the control's roughly 9-11% share) gives N ≥ 13,816, so the
  floor is 14,000. Single-repeat anchors measured 9,142-9,318, so captures run
  with `--repeat-count 2`. The control's non-zero target, the name-only
  cross-check, the pinned interpreter, and `variant.txt` guard against a zero
  produced by matching nothing.
- V6. Census correctness.
  Obligation: the census command's anchor, target, and name-only target weights
  equal a brute-force recount over the parsed stacks. Method: Hypothesis
  property over generated folded stacks, plus named examples for malformed
  input and each exit status. Artefact:
  `cuprum/unittests/test_hot_path_frame_census.py`. Non-vacuity: generated
  captures include, together, stacks with the target outside the anchor (must
  not count), inside it (must count), repeated within one stack (counted once),
  and rendered with a different location (counted only by the name-only weight).
- V7. Speed is not worse, and the per-event saving is quantified.
  Obligation: the `cb` scenario's candidate/control wall-time ratio has a 95%
  interval whose upper bound is at most 1.05, and the in-process microbenchmark
  shows the per-event saving. Method: ABBA-ordered unprofiled pairs with a
  discarded warm-up, a paired geometric-mean ratio with a bootstrap interval;
  and a pytest-benchmark microbenchmark of `_emit_exec_event`. Artefacts:
  `docs/profiling/5-2-2-observe-hook-dispatch/unprofiled/` and the new
  `observe-dispatch` group in `benchmarks/test_stream_microbenchmarks.py`.
  Non-vacuity: the microbenchmark runs both a `None`-returning hook and an
  `async def` hook; the timing report lists every paired ratio, not only the
  summary.

Methods deliberately not used: new syrupy snapshots (no output format is
introduced; `tests/behaviour/__snapshots__/test_structured_events.ambr` must
stay unchanged, which is itself evidence for C1); CrossHair, Rust `proptest`,
Kani, and Verus (Decision D6).

### Behavioural specification

`tests/features/observe_hook_dispatch.feature`:

```gherkin
Feature: Observe hook dispatch
  Every supported observe-hook shape receives every line event, and every
  awaitable it returns is awaited before the run returns.

  Scenario Outline: Every hook shape receives every line and settles before the run returns
    Given a safe command that prints 5 numbered lines to stdout
    And an observe hook shaped as <shape> that records stdout line events
    When the command runs synchronously with line events observed
    Then the hook records exactly 5 stdout line events in print order
    And no observe-hook task is pending when the run returns
    And no coroutine-never-awaited warning was emitted

    Examples:
      | shape                                  |
      | a synchronous function                 |
      | an async def function                  |
      | a lambda returning a coroutine         |
      | a functools.wraps wrapper of async def |
      | an instance with async __call__        |
```

Under Option B, add:

```gherkin
  Scenario: A hook whose attribute lookup raises still observes every line
    Given a safe command that prints 5 numbered lines to stdout
    And a synchronous observe hook whose attribute lookup raises
    When the command runs synchronously with line events observed
    Then the hook records exactly 5 stdout line events in print order
```

## Plan of work

Stage A (no code): approval of this plan and the Option A or B choice.

Stage B (red): add V2 under strict `xfail`, and add V1, V3, and V4, which pass
on today's dispatcher because they specify preserved behaviour. Record the red
run.

Stage C (green): change `_emit_exec_event`, remove the marker, and add the
census command and the microbenchmark.

Stage D: collect evidence (EP-M5), write documentation (EP-M6), and run every
gate.

### Production change (Option A)

In `cuprum/_observability.py`, inside `_emit_exec_event`, replace the single
line `if inspect.isawaitable(result):` (line 132) with an early skip and a
two-part test, leaving the scheduling block and its logging untouched:

```python
        if result is None:
            continue
        if type(result) is types.CoroutineType or inspect.isawaitable(result):
            scheduled.append(
                asyncio.create_task(
                    _await_awaitable(result, event.phase),
                    name=f"cuprum.observe.{event.phase}",
                )
            )
            # observe_hook_task_scheduled record unchanged; count read after
            # the append exactly as today.
```

Add a short comment explaining *why*: `None` is the only result a synchronous
hook returns and a native coroutine is the only result an `async def` hook
returns, so the general test runs only for the remaining shapes. `types` is
already imported. Update the docstring's first paragraph to say which results
are scheduled. No other production module changes.

### Option B deltas (only if the approver requires clause M literally)

- Classify in `_ExecutionHooks.__post_init__` (`cuprum/_pipeline_types.py`)
  into a derived `observe_dispatch` field of `(hook, is_async)` pairs, declared
  with `dc.field(init=False, repr=False, compare=False)` and set with
  `object.__setattr__`. Use plain tuples rather than a `NamedTuple` (measured
  faster). The probe confirmed this works on a frozen, slotted dataclass, and
  that `dc.replace`, `copy`, and `pickle` behave; nothing calls `dc.replace` on
  `_ExecutionHooks`. Classification runs once per command or pipeline stage.
- Extract `_is_async_callback` from `cuprum/_idle_heartbeat.py:356` into a
  new leaf module `cuprum/_callable_kinds.py` as `_is_async_callable`, with its
  own `__all__`; update
  `cuprum/unittests/__snapshots__/test_maturin_build.ambr` for the new file.
- The observe classifier catches `Exception` from `_is_async_callable` and
  falls back to `False`; the shared helper itself keeps raising for the
  heartbeat validator.
- `_emit_exec_event` takes `tuple[tuple[ExecHook, bool], ...]`; its loop uses
  the hint only to try the exact-type test first, and the result-guarded logic
  above still decides scheduling. Document in code and in §8.1.3 that the hint
  never decides whether a result is scheduled.
- Update the direct callers in `test_cqrs_helpers.py` and
  `test_cqrs_hook_behaviour.py` without growing either file (move the touched
  tests into a new module if needed).
- Add a version-aware classification table (`create_autospec` differs on
  3.12) and a spy on `cuprum._observability._is_async_callable` (the name as
  looked up) asserting exactly `len(hooks)` calls per stage.
- Add the raising-proxy behavioural scenario above.

## Milestones and plateaus

- EP-M1. Red tests. V2 committed under
  `xfail(strict=True, reason="5.2.2 result-guarded dispatch not yet implemented")`;
  V1, V3, and V4 committed passing. Gates pass. Requirements: ROAD-5.2.2-S1
  (specified), -S2 (guarded). Recovery: revert. Compatibility decision: none.
- EP-M2. Result-guarded dispatch. The Option A change lands; V2's marker is
  removed; all gates pass; `cs delta origin/main` reports nothing new.
  Requirements: ROAD-5.2.2-S1, -S2. Conformance check: C1-C5 hold by V1-V4 and
  the unchanged snapshot; no public surface touched. Recovery: revert; EP-M1's
  marker returns the tree to green. Compatibility decision: none.
- EP-M3. Census command. `benchmarks/summarize_hot_path_frames.py` with
  `<stacks.folded> --anchor FUNCTION@LOCATION --target FUNCTION@LOCATION
  --min-anchor-weight N --output OUT.json`,
  reusing `parse_capture`, `Frame`, and `FramePattern`. Output fields:
  `total_weight`, `anchor_weight`, `target_weight`,
  `target_weight_any_location`, `target_weight_outside_anchor`, `status`. Exit
  0 when both target weights are zero and the anchor meets the floor; 1 when
  the target weights agree and are non-zero; 2 for malformed input, an anchor
  below the floor, or disagreeing target weights. V6 passes. Documented under
  "Profiling harness overview" in `docs/developers-guide.md`. Requirements:
  ROAD-5.2.2-S3 (instrument). Recovery: revert.
- EP-M4. Microbenchmark. An `observe-dispatch` group in
  `benchmarks/test_stream_microbenchmarks.py` (122 lines today) timing
  `_emit_exec_event` with one `None`-returning hook and with one `async def`
  hook. Runs under `make benchmark-micro`. Recovery: revert.
- EP-M5. Evidence. Three capture pairs, census results, the re-run 5.2.1
  classifier on every capture, unprofiled ABBA pairs, and microbenchmark output
  for control and candidate are committed under
  `docs/profiling/5-2-2-observe-hook-dispatch/` with a README; the evidence
  document `docs/tee-hotpath-observe-hook-dispatch-5-2-2.md` is written.
  Conformance check: candidate zero, control non-zero and name-only equal,
  anchors at or above 14,000, variants and interpreter proven. Recovery:
  re-collect; each pair directory is overwritten.
- EP-M6. Documentation and roadmap (see `Concrete steps`). Recovery: revert.

## Concrete steps

Run everything from the worktree root. Log every gate with `tee`, for example:

```bash
make test 2>&1 | tee /tmp/test-cuprum-5-2-2-remove-the-per-hook-inspect-isawaitable-call.out
```

Red stage (EP-M1):

```bash
uv run pytest cuprum/unittests/test_observe_hook_dispatch.py \
  cuprum/unittests/test_observe_hook_dispatch_properties.py \
  tests/behaviour/test_observe_hook_dispatch.py -v
```

Expected: the two V2 tests report `XFAIL`; every other new test reports
`PASSED`.

Green stage (EP-M2): remove the marker and rerun; expect `PASSED` throughout.

Profiling (EP-M5). Pin one interpreter (CPython 3.14.4, as 5.2.1 used) for
every run. Create the control worktree at EP-M1's head and use EP-M2's head as
the candidate:

```bash
git worktree add ../cuprum-5-2-2-control <EP-M1 head SHA>
```

Build the fixture exactly as 5.2.1 did (see
`docs/profiling/5-2-1-line-event-emission/README.md`), producing
`dist/fixtures/seed12345-wrap76.b64`. For rounds 1 to 3, alternate which
variant runs first, and capture from inside the variant's worktree so that
`python -m` imports that tree's `cuprum`:

```bash
py-spy record --format raw --rate 100 \
  --output <capture>/stacks.folded -- \
  <python> -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 --mode echo \
  --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 2 --read-size 65536 --output <capture>/worker-result.json
python -m benchmarks.summarize_hot_path_frames <capture>/stacks.folded \
  --anchor emit_line@cuprum/_line_callbacks.py \
  --target isawaitable@inspect.py \
  --min-anchor-weight 14000 --output <capture>/frame-census.json
python -m benchmarks.summarize_line_event_profile <capture>/stacks.folded \
  --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output <capture>/construction-share.json
```

Record in `variant.txt`:

```bash
python - <<'EOF' > <capture>/variant.txt
import pathlib
import subprocess
import sys

import cuprum

package = pathlib.Path(cuprum.__file__).parent
print(sys.version)
print(package)
print(subprocess.check_output(["git", "-C", str(package), "rev-parse", "HEAD"], text=True).strip())
EOF
```

A non-zero py-spy exit with `No child process (os error 10)` after `Errors: 0`
is benign (5.2.1 evidence, around lines 167-172). Then run one discarded
warm-up per variant and six ABBA-ordered unprofiled pairs of the `cb` scenario
(`echo-devnull-cb-s1`), compute the paired geometric-mean ratio with a
10,000-resample bootstrap 95% interval, and run `make benchmark-micro` in both
trees.

Documentation (EP-M6):

- `docs/users-guide.md`: list the supported hook shapes explicitly ("any
  callable whose return value is awaitable, including `async def` functions,
  bound async methods, objects with `async def __call__`, and plain functions
  or lambdas that return a coroutine"), and add a performance note beside the
  5.2.1 note (around line 1046) with the measured figures.
- `docs/cuprum-design.md` §8.1.3: record the result-guarded dispatch, why the
  decision is made on the result and not on the hook, and the rule that no hook
  classification may decide whether a result is scheduled.
- `docs/developers-guide.md`: document the census command under "Profiling
  harness overview"; replace the forecast at around line 3413 and lines
  5842-5847 with the measured construction share, and correct the 589-sample
  figure's meaning (leaf-only).
- `docs/roadmap.md`: tick 5.2.2; correct the stale `:35` citation; reword the
  mechanism per Decision D1; replace the forecast at around line 311 with the
  measurement; add a candidate follow-up for the per-line
  `observe_hook_task_scheduled` record cost.
- `docs/contents.md`: index the evidence document and the profile README
  beside the 5.2.1 entries (around line 105), and update the 5.2.1 entry's
  forecast wording (around line 110).
- `benchmarks/_line_event_profile_classifier.py`: refresh the line citations
  in the comment above `_HOOK_DISPATCH_BOUNDARY` (around line 70) and confirm
  the boundary still matches `_emit_exec_event` by name and location.
- `CHANGELOG.md`: add a bullet after the 5.2.1 entry in `### Changed` under
  `## [0.2.0-beta1]`, linking the evidence and quoting measured figures.
- Run `make fmt` (it reflows Markdown with `mdtablefix`), then
  `make markdownlint` and `make nixie`.

## Validation and acceptance

Acceptance is met when all of the following hold:

- `make check-fmt`, `make typecheck`, `make lint`, `make test`,
  `make markdownlint`, and `make nixie` pass at the final head.
- `test_none_results_never_reach_isawaitable` and
  `test_native_coroutines_never_reach_isawaitable` were recorded as strict
  `xfail` before EP-M2 and pass after.
- The V1 property passes with every class in its recorded statistics, and
  both seeded-fault tests pass by observing a mismatch.
- `git diff --exit-code origin/main -- tests/behaviour/__snapshots__/ cuprum/unittests/__snapshots__/`
  reports no change (Option A).
- Every candidate census reports both target weights 0 and exits 0; every
  control census reports equal, non-zero target weights and exits 1; every
  anchor weight is at least 14,000.
- The `cb` ratio's 95% interval upper bound is at most 1.05.

Quality criteria: tests and gates above; V1-V7 discharged; no new dependency;
no change to public API.

## Idempotence and recovery

Every milestone is one or a few commits that can be reverted independently, in
reverse order. Profiling is re-runnable; each pair directory is overwritten on
re-collection, and nothing under `dist/` is committed. Remove the control
worktree with `git worktree remove ../cuprum-5-2-2-control` after EP-M5.

## Artefacts and notes

Classification probe (2026-10-01), abridged; identical on 3.12.13, 3.13.13,
3.14.4, and 3.15.0b2 (`iscorofn` is `inspect.iscoroutinefunction(hook)`;
`heartbeat_rule` adds the `type(hook).__call__` test):

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

Expert design review (2026-10-01), three panels: structure and contracts,
alternatives and cost, and failure modes and viability. All three independently
recommended Option A. Their corrections to the first draft are folded into
`Surprises & discoveries`, the Decision log (D1-D8), V1-V7, and the tolerances.
The verdict was "proceed with conditions": choose between Options A and B,
correct the anchor floor, and make the red stage collectable.

## Interfaces and dependencies

No new dependency, and no new or changed production symbol under Option A.
`_emit_exec_event` keeps its signature:

```python
def _emit_exec_event(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]: ...
```

New non-production artefacts:

- `benchmarks/summarize_hot_path_frames.py`: a command line and a
  `summarize(capture, *, anchor, target, min_anchor_weight) -> dict[str,
  object]`
  function.
- `cuprum/unittests/test_observe_hook_dispatch.py`,
  `cuprum/unittests/test_observe_hook_dispatch_properties.py`,
  `cuprum/unittests/_observe_hook_dispatch_support.py`,
  `cuprum/unittests/test_hot_path_frame_census.py`,
  `tests/features/observe_hook_dispatch.feature`, and
  `tests/behaviour/test_observe_hook_dispatch.py`.
- `docs/tee-hotpath-observe-hook-dispatch-5-2-2.md` and
  `docs/profiling/5-2-2-observe-hook-dispatch/`.

## Revision note

2026-10-01, first draft: classification once per execution as a fast-path
selector, with a new shared classifier module.

2026-10-01, revision 1 after the expert design review: Option A (a
result-guarded dispatcher with no classification) is recommended, and
classification moves to `Option B deltas` for the approver to choose. The
measurement plan is corrected: a derived anchor floor of 14,000 with
`--repeat-count 2`, inclusive rather than leaf weights, a name-only
cross-check, a pinned interpreter, `git rev-parse` variant proof, and a timing
interval in place of a raw 5% threshold. The off-by-one scheduled-count
logging, the reachable `isawaitable` exception, the over-cap test files, and
the red stage's collectability are also addressed. Remaining work is unchanged
in order but smaller in scope: one production module under Option A.
