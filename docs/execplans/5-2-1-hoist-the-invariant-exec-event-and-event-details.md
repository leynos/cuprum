# Hoist invariant execution-event fields (5.2.1)

Status: BLOCKED — EP-M2's design cannot reach V5's 10% target. See the
2026-09-27 feasibility discovery below; a revised design needs approval before
any runtime change. EP-M1 is complete.

This ExecPlan is a living execution plan. Keep Constraints, Tolerances, Risks,
Progress, Surprises & discoveries, Decision log, Outcomes & retrospective,
Conformance basis, and Verification plan current throughout implementation. The
planning-only pull request has been rebased onto `991dee64` and now carries
implementation.

## Purpose / big picture

Line observers currently cause two frozen dataclasses to be constructed for
almost every output line. Roadmap item 5.2.1 moves stable execution metadata
out of that loop, reducing pure-Python observation overhead before a Rust
consume dispatcher is assessed. Consumers must receive the same event payloads
and lifecycle semantics, including a fresh timestamp and event per line.

Success requires all three results: metadata is resolved once per observed
stream, payload and hook contracts remain unchanged, and a committed profiler
artefact shows construction accounts for no more than 10% of the callback
consume samples. The historical approximately 39% is context, not a current
control. No performance result has been established by this plan.

## Constraints

- Obtain explicit user approval of this plan before changing runtime code,
  tests, or profiling helpers. Publishing this draft does not grant approval.
- Keep the work pure Python. Preserve the 65536-byte read size selected by
  5.1.1. Do not add a Rust consume dispatcher or a dependency.
- Preserve `ExecEvent` as the same public frozen, slotted dataclass, with its
  constructor, field order, defaults, equality, representation, and field
  introspection unchanged. Every emitted event remains a distinct object.
- Preserve the existing mapping references and shallow mapping semantics.
  Do not deep-copy metadata or silently change when overlays are resolved.
- Resolve PID only after spawning, once for each observed stream. `plan`
  still has no PID. Timestamp and line remain per-event values.
- Keep `_emit_event` and `_emit_exec_event` as the dispatch path. Preserve
  result-based awaitable detection, hook ordering, failure propagation, and
  ownership of already-scheduled tasks. Task 5.2.2 is separate work.
- Keep lifecycle emission and sanitized `emit_fail_fast` separate from the
  line factory. Do not introduce phases or change ADR-008's pump channel.
- Add focused parity coverage here; do not claim completion of the broader
  combinatorial suite in 5.2.3. Do not mark either later item done.
- Follow `AGENTS.md`, the documentation style guide, and `.rules/python-*.md`.
  Keep code files below 400 lines. Never run gates or profiles concurrently.
  Use the shared Cargo cache; use `/tmp` only for logs and scratch material.

## Tolerances (exception triggers)

A missed performance target is a design exception, not permission to weaken
acceptance. Try the ordinary-constructor factory and at most two local
refinements within the same design. If the final representative measurement
still exceeds 10%, set this plan to BLOCKED, record the measurements and
options, and seek approval for a revised design. Do not declare the feature
complete merely because field hoisting or correctness tests pass.

The two refinements are limited to argument-passing and local-binding variants
of ordinary `ExecEvent` construction. Neither may change public signatures or
event defaults.

Stop for approval if progress requires changing the public event
representation, constructor bypass, dynamic subclasses, event reuse, hook
classification, native code, dependencies, or metadata semantics. Stop if more
than four production Python modules need changes; tests and measurement helpers
are separate from that bound. Revisit the plan if the initial code survey
discovers another production callback path requiring modification. There is no
time limit. Disk exhaustion requires stopping and reporting it, not deleting
another agent's files or processes.

## Risks

High impact, medium likelihood: caching metadata may not reach 10% because a
fresh `ExecEvent` still initializes 23 slots. Mitigate with the early measured
feasibility gate; do not promise an unmeasured result.

High impact, medium likelihood: caching too early can bind the wrong PID or
execution context; caching an event can corrupt retained asynchronous payloads.
Bind after spawn, keep one factory per stream, and test concurrent executions
and delayed observers.

High impact, medium likelihood: generated constructor frames can be ambiguous,
profilers can miss Python frames, and percentages can improve by inflating the
denominator. Preserve full folded stacks, classify caller paths, retain
unprofiled timing controls, and reject unresolved attribution.

Medium impact, medium likelihood: a new post-spawn setup exception can bypass
cleanup. Keep factory creation inside existing process/task ownership and add
failure injection at that boundary.

## Progress

- [x] (2026-09-19) Read repository instructions, requested skills, roadmap,
  baseline, design, ADRs, and relevant Python rules; inspect live sources.
- [x] (2026-09-19) Create the requested branch from `861fe2f0`; confirm it
  matches the fetched `origin/main` at planning time.
- [x] (2026-09-19) Use two Wyvern investigators and the six Logisphere expert
  perspectives to identify semantics, measurement, and feasibility constraints.
- [x] (2026-09-19) Resolve dataclass and profiler questions using Firecrawl
  against primary documentation; incorporate review recommendations.
- [x] (2026-09-19) Pass formatting, Markdown/spelling, and Mermaid gates;
  publish [draft PR #433](https://github.com/leynos/cuprum/pull/433) with the
  requested upstream tracking and Lody session reference.
- [x] (2026-09-26) Obtain explicit user approval before starting EP-M1;
  granted, with instruction to implement the plan in full.
- [x] (2026-09-26) EP-M1b: re-baseline the plan against the current tree
  before writing any characterization test (see 2026-09-26 discoveries).
- [~] EP-M1: establish current control and contract characterization.
  - [x] V1 red test written and recorded through *both* existing production
    factories; `strict=True` xfail keeps the committed suite green, and a
    `0`-line case carries no marker because it passes either way. The pipeline
    factory's cases pin *routing* rather than restating the per-line cost: a
    mutation that gives the pipeline its own inline closure fails 2 of them.
  - [x] V2 field-for-field parity characterization against generic `emit`.
  - [x] V5 classifier implemented, unit-tested (33 tests), split to satisfy
    C0302, and verified against a real py-spy capture of both production
    factories.
  - [x] Control profile captured; D = 30822 ≥ 10000; share 34.7284%;
    `construction-share.json` reproduced byte-identically on re-run.
  - [x] `classifier-rules.json` committed with caller-scoped rules.
  - [x] V2 Hypothesis property module (`test_line_event_emission_properties.py`,
    21 tests) written against the generic `emit` oracle at a pinned clock. The
    plan's two required mutations are confirmed detected; see the 2026-09-27
    discovery below.
  - [x] V3 scheduling/failure-contract cases. Five hook-contract cases plus
    the pipeline reaping case, each mutation-checked (see the two V3 entries
    below). The plan's command-path "existing owner reaps the child" cases are
    **contradicted** by measurement and are out of scope, recorded as such
    rather than weakened to pass. See the 2026-09-27 V3 discoveries below.
  - [x] V4 seven pytest-bdd scenarios in `structured_events.feature`, the
    pipeline and concurrent cases among them, plus a Syrupy snapshot of the
    normalized lifecycle triple. Two of the plan's V4 assertions were wrong
    against the real tree and are corrected in the scenarios; see the
    2026-09-27 V4 discovery below.
  - [x] V4's step module split three ways to satisfy the 400-line
    `max-module-lines` cap, which `python-lint` enforces on `tests/`; the step
    module is loaded as a `pytest_plugins` entry because pytest-bdd 8 scopes a
    step to its defining module. See the 2026-09-27 "the 400-line cap applies
    to `tests/`" discovery below.
  - [ ] Three matched control/candidate pairs and ≥5 unprofiled paired rounds.
    Gated on EP-M2 by construction: the *candidate* is the post-hoist
    implementation, so there is nothing to pair until the hoist exists.
- [ ] EP-M2: **BLOCKED before implementation.** Pre-implementation feasibility
  measurement of the committed control capture shows the design's floor is
  N = 6474 → 21.0045% of D against a budget of N ≤ 3082 for 10%. Every sampled
  per-line `ExecEvent.__init__` resolves to the `emit` caller in
  `_pipeline_types.py`, which a hoist in `_line_callbacks.py` cannot move, so
  only the `_EventDetails` rule is addressable and removing all of it leaves
  21.00%. No runtime edit was made; the milestone stops here for approval of a
  revised design. See the 2026-09-27 feasibility discovery below for the
  mechanism, the measured cost driver, and four consolidated options.
- [ ] EP-M3: commit representative profiler evidence, documentation, and
  completion of roadmap item 5.2.1 after all acceptance conditions pass.
  Unreachable until EP-M2's design is revised and approved.

## Surprises & discoveries

### 2026-09-26: implementation-target re-baseline (EP-M1b)

A read-only survey of the rebased tree at `db1902db` (main `991dee64`) found
the plan's **Context and orientation** section stale in three ways. These
change *where* the hoist lands; they do not change what it must achieve.

1. **The shared factory already exists.** The plan's design goal — "a private
   callback factory shared by single-command and pipeline streams" — is now
   `cuprum/_line_callbacks.py::_compose_line_callbacks`, introduced since the
   planning baseline. `_subprocess_streams.py:88` and
   `_pipeline_stage_streams.py:110,139` are its only production callers; both
   delegate to it. The plan's named entry points (`_create_stream_callback`,
   `_create_stage_line_observer`) are therefore no longer where the per-line
   construction happens, and `_create_stage_line_observer` **does not exist
   anywhere in the tree** — it is referenced only by the plan itself.

2. **The per-line `_EventDetails` construction is now in that one seam.**
   `cuprum/_line_callbacks.py:58` is the *sole* per-output-line construction
   site repo-wide; every other `_EventDetails(` site is lifecycle (plan, start,
   exit, timeout, stdin, fail-fast). `_event_details` has no callers outside
   its own module.

3. **The argv cost is a plain `@property`, not the pipeline's.**
   `cuprum/sh/safe_cmd.py:74-83` builds `(str(self.program), *self.argv)` on
   every access with no memoization. Note `cuprum/sh.py` is now the package
   `cuprum/sh/`; the plan's ``cuprum/sh.py::SafeCmd.argv_with_program`` path is
   stale. The design document's own hot-path note already lists this at ~3%
   (`docs/tee-hotpath-profiling-baseline-2026-06-12.md:151`).

**Consequence for scope.** The hoist lands in one production module, not two.
This *narrows* the change: it is well inside the four-module exception bound,
so the "revisit the plan" clause at Constraints does not require fresh
approval. It also means both existing callback factories reach the optimization
by construction, so the plan's requirement that both paths use the factory is
satisfied structurally rather than by two parallel edits.

**Consequence for the red test (V1).** The plan asks that V1 exercise "both
existing production callback factories … not the proposed method", so that a
missing-method error is not mistaken for the performance bug. With one shared
seam, the honest red test drives the real production consumers —
`_create_stream_callback` and `_create_stage_capture_tasks` — through a real
observed stream and counts per-line `_EventDetails` constructions and argv
reads. Driving `_compose_line_callbacks` directly would measure the seam, not
the production path.

**Consequence for the event count.** The plan's risk section says a fresh
`ExecEvent` "still initializes 23 slots". The dataclass now has **27** fields:
main added `max_rss_bytes`, `user_cpu_seconds`, `system_cpu_seconds`, and
`resource_usage_mode` between the planning baseline and the rebase (verified by
diffing `cuprum/events.py` across `861fe2f0..HEAD`; the four are the only field
additions). The per-line construction cost is therefore ~17% higher than the
plan assumed. This makes the 10% gate *harder*, not easier, and it is recorded
because the plan's stated difficulty estimate is now materially optimistic.

**Verified still accurate.** The plan's claim that `_StageObservation.emit`
constructs the `ExecEvent` is correct (`cuprum/_pipeline_types.py:120-149`), and
`_emit_event`/`_emit_exec_event` remain the dispatch path
(`cuprum/_pipeline_types.py:200-207`, `cuprum/_observability.py:64`). Both
`SafeCmd.run()`/`lines()` style consumers reach the seam through
`_LineEmissionContext`, which carries `stream`, `pid`, `on_line`, and
`started_at`. `_StageObservation` is a `frozen=True, slots=True` dataclass with
fields `cmd`, `hooks`, `tags`, `cwd`, `env_overlay`, `pending_tasks`,
`wall_clock`, `exec_id`.

### 2026-09-27: EP-M1 characterization and the classifier's real-data convergence

EP-M1 is in progress. What follows is measured, not proposed.

**py-spy renders every generated constructor identically.** A control capture
of the real workload contains the frame `__init__ (<string>:N)` for
`_FinishedEchoLine`, `_EchoEncoding`, `_LineFinalization`, and both event
types, and *nothing in the frame text distinguishes them*. `dataclasses` builds
`__init__` with `exec`, so `co_filename` is the literal `<string>`. This is
stronger than the plan's "do not blanket-match `__init__`" warning: a
frame-text match is not merely risky, it is **impossible**. Every rule that
names a generated constructor must be resolved through its callers. The shipped
rules file records this and leaves `line` unset, because the generated line
number shifts whenever the class gains a field.

**Caller *proximity*, not declaration order, must pick the rule.** Two rules
both matching `__init__ (<string>)` — one for the per-line `_EventDetails`, one
for the full `ExecEvent` — both saw every construction sample on the real
capture, and declaration order alone attributed all 134 of them to the
`ExecEvent` rule. The fix is to prefer the rule whose nearest matching caller
sits closest to the frame. Re-classified, the real capture then split correctly
(89 `_EventDetails` + 45 `ExecEvent` in the probe capture; 6474 + 4230 in the
control). Resolution by order would have let a broadly-called rule silently
absorb a narrower one's frames.

**Drift detection converged over three designs, the first two rejected on real
data.** The plan requires that unresolved frames, a zero D, or missing expected
constructor callers make a run inconclusive. How to *detect* that was not
obvious:

1. *Frame-anchored* — a frame is unresolved when no rule claims it. Rejected:
   on the control capture this flagged 1412 samples of
   `__init__ (<string>:2/3/4/5)`, all of which trace to
   `finish_line (cuprum/_echo_truncation.py)`. Those are the echo-truncation
   limiter's own records, a different cost centre, not rule drift.
2. *Stack-anchored* — a stack is unresolved when a caller appears without its
   frame below it. Rejected: this flags ordinary sampling. A sample landing
   anywhere inside `emit_line` before it reaches the constructor has exactly
   that shape, and it is the common case.
3. *Capture-wide, per-rule* — a rule is drifted when its callers carry real
   weight inside the consume subtree but the rule matched nothing at all. This
   is the shipped semantics. A rule whose callers never appear is merely
   *absent* from this workload and is left alone, so one capture does not
   report every unrelated rule as broken. The control capture then reported
   `unresolved_frames: {}`.

**The classifier was verified against real production code, not only synthetic
stacks.** A scratch probe drove both production factories — `SafeCmd.run()`
with an observe hook and an `on_line` hook, then a two-stage `sh.Pipeline` with
the same — over a chatty child process, and was captured under py-spy. The real
frames match the rules exactly:
`emit_line → _event_details → __init__ (<string>)` and
`emit_line → emit (cuprum/_pipeline_types.py) → __init__ (<string>)`. Both
production callback factories construct `_EventDetails` per line, confirming
V1's premise against the tree rather than against the plan.

**Control baseline (untouched implementation).** One `py-spy record` capture of
the plan's worker invocation on the full wrap-76 fixture:

| Quantity                     | Value                                                                                                          |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `parent_samples`             | 32468                                                                                                          |
| `consume_samples` (D)        | 30822                                                                                                          |
| `construction_samples` (N)   | 10704                                                                                                          |
| `construction_share_percent` | 34.7284                                                                                                        |
| share of all parent samples  | 32.9678                                                                                                        |
| `matched_frames`             | `ExecEvent.__init__ via _StageObservation.emit`: 6474, `_EventDetails.__init__ for the per-line payload`: 4230 |
| `unresolved_frames`          | `{}`                                                                                                           |
| classifier `status` / exit   | `fail_above_limit` / 1                                                                                         |

D = 30822 clears the plan's 10000 floor by 3×. The control exits 1, which the
plan anticipates and requires be retained rather than treated as an error. The
run's recorded artefacts are `worker-result.json` (worker `status: ok`,
`wall_time_seconds` 316.24, 28256364 lines) and `construction-share.json`,
which a re-run of the committed classifier reproduces byte-identically.

**A supporting-module split was forced by the repo's own lint.** The first
draft of `benchmarks/summarize_line_event_profile.py` was 651 lines, tripping
`too-many-lines` (C0302) at the configured 400-line ceiling. `benchmarks/` *is*
walked by pylint, unlike `cuprum/unittests/`, where the two new test modules
sit at 459 and 596 lines without complaint. The parse/model half moved to
`benchmarks/_line_event_profile_model.py`; the gate (387 lines) keeps the
classification, reporting, and CLI, and re-exports the model's names so the
entry point is unchanged. The line-splitting is delegated to the existing
`benchmarks.summarize_folded._parse_folded_line`, as the plan asks.

**V2's property module is written and its required mutations are confirmed
detected.** `cuprum/unittests/test_line_event_emission_properties.py` (21
tests) asserts four invariants per generated case: payload identity against the
generic `emit` oracle, one clock read per delivered line in order, distinct
object identities, and unchanged earlier payloads. Three design points were
settled by failure, and each is worth keeping:

1. *The clock is pinned, not scripted.* An earlier draft shared a scripted
   iterator between the line path and the oracle, and the two silently
   desynchronized, reporting a `timestamp` divergence that was an artefact of
   the test. `_SetClock` instead holds one value the test sets before each
   path, so "the same clock value" is literal rather than index-arithmetic.
2. *`@example` kwargs must match `@given` exactly.* Hypothesis rejects
   per-argument `@example` rows beside a `@given` that names other arguments,
   so the module uses the repo's existing composite-`case` idiom
   (`@given(case=_emission_case())` + `@example(case=...)`), as in
   `test_stream_property_based.py`.
3. *Zero-line cases are excluded from the generators and named explicitly
   instead.* With no lines, every property is true without anything having
   happened; `min_lines=1` is the anti-vacuity default, and the zero-line
   boundary is a named case in `test_line_event_emission` where it asserts
   something concrete.

**The plan's two required mutations were applied and both are detected.** In
the final module shape, reusing one event object across lines fails 7 tests and
fixing the timestamp to a constant fails 2. The production files were restored
afterwards, confirmed by an empty `git diff --stat HEAD`.

### 2026-09-27: V4 — the plan's pipeline model of execution identity was wrong

V4 is written: seven scenarios in `tests/features/structured_events.feature`
with bindings in `tests/behaviour/test_structured_events.py` (9 tests, one
Syrupy snapshot). Two of the plan's own V4 assertions did not survive contact
with a real pipeline, and both were corrected against observed output rather
than reasoned about.

**A pipeline emits one plan/start/exit triple *per stage*, not per run.** The
plan's scenario text reads "plan has no process identifier and exit retains the
execution token" — singular, one pair for the run. A scratch probe printing
every event for a two-stage pipeline shows two `plan` events, two `start`
events, and two `exit` events, each `start` with its own `exec_id`. The step is
therefore named `then_every_stage_has_its_own_plan_and_exit_token` and asserts,
per stage, that `plan` carries no PID while its `exit` carries the token its own
`start` reported.

**A line event carries its *stage's* PID and execution token.** The natural
inference from the above — that a line event correlates to the run — is also
false. For every retained line event the binding asserts
`event.exec_id == starts[event.pid]`, where `starts` maps each observed `start`
PID to its own token. One run-wide pair would satisfy this only by accident,
and would be wrong the moment the pipeline spawns a second stage.

Both bindings were wrong in the same direction on first write, and both were
corrected only after dumping the real event stream. This is the EP-M1b lesson
repeating: the plan is a hypothesis about the tree, not a description of it.

**V4's per-stream ordering assertion must not be widened into a global one.**
An intermediate snapshot asserted a single interleaved stdout/stderr sequence
and failed, because stdout `beta` and stderr `gamma` were observed in the
opposite order to the child's write order. The two readers are independent
tasks; there is no cross-stream ordering guarantee anywhere in the design, and
the plan's V4 text says as much ("without inventing global stdout/stderr
ordering"). The snapshot was narrowed to the normalized lifecycle triple, and
per-stream order is asserted separately. Retaining the wrong assertion would
have made the suite flake, or worse, pinned a guarantee the library does not
offer.

### 2026-09-27: what `make lint` actually runs — two corrections to the plan

The plan's Validation section treats `make lint` as one gate. It is a make
target with prerequisites, and reading the Makefile changes which sub-checks
this task may claim. Both facts below were read off `Makefile:359-384`, not
inferred.

**`make lint` does not run the spelling check.** The target is
`lint: python-lint rust-lint github-actions-lint`. Spelling is reached only as
`rust-lint: lint-clippy lint-whitaker spelling`, and GNU make is free to build
the three lint prerequisites in any order. A run that stops at a spelling
failure may therefore have run `rust-lint` entirely, `github-actions-lint`
entirely, or neither. The failure ordering in a log is not a run order.

**This matters because of an abort-at-first-failure trap that already bit this
task.** The `lint` recipe is one `$(MAKE)` invocation whose prerequisites are
attempted in turn; the first failing prerequisite stops the run and the rest
are never attempted. The recorded `/tmp/521-lint.out` shows exactly that: it
reached `typos-config-builder gate`, failed on five real spelling errors, and
ended at `make: *** [Makefile:405: spelling] Error 2` with
`EXIT=2 DURATION=124s`. The two `15 KB` of log before that line is `rust-lint`
having run to completion, so the trivial spelling failure *masked a full Rust
rebuild*. Everything that sorts after the abort is **unobserved**, not passing.
The plan must report three states per gate — passed, failed, unobserved — and a
green spelling line in the log proves nothing about `github-actions-lint`.

The practical consequence: fix spelling first (`make spelling` alone is
seconds), because a single misspelling otherwise costs a `cargo doc` plus
`clippy` cycle in wall-clock time and hides whichever gate had not yet run.

### 2026-09-27: V3 disproves the plan's post-spawn ownership claim

V3's last requirement reads: "Inject factory preparation failure after spawn
and prove the existing owner reaps the child. Inject this in both command and
pipeline paths." The **pipeline** half is true. The **command** half is not,
and the gap is pre-existing on `main` — this branch has no production diff, and
`cuprum/` is byte-identical to `origin/main`.

A read-only probe replaced the two production factories with a raising
stand-in, recorded the pid from the spawn call itself (so a path that never
reaches `start` still yields a subject), and classified the child from
`/proc/<pid>/stat` after the failure had propagated:

| path       | factory that raised           | child after the failure          |
| ---------- | ----------------------------- | -------------------------------- |
| `run()`    | `_compose_line_callbacks`     | **leaked**, state `S` (sleeping) |
| `lines()`  | `_spawn_stream_consumers`     | **leaked**, state `S` (sleeping) |
| `pipeline` | `_create_stage_capture_tasks` | reaped                           |

The pipeline is safe because `_spawn_pipeline_processes` owns the stages it has
already created and tears them down on its own failure path. Neither command
path has an equivalent for the stream-factory region.

**Why, structurally.** `_build_unstarted_run` (`_line_stream/spawn.py:97`)
documents the invariant it means to establish:

> Nothing here suspends or fails. `_build_stream_config` only reads the
> execution, `_spawn_stdin_writer` and `_spawn_stream_consumers` are plain
> `create_task` calls, and `_LineStreamRun` is a frozen dataclass, so no
> exception can escape and leave a child running with half-built ownership.

The premise is false. `_spawn_stream_consumers` calls
`_create_stream_callback` — which calls the composition factory — *before* it
calls `create_task`, so a failure there escapes `_build_unstarted_run` into
`_start_line_stream_run` (`coordinator.py:115-141`), where `process` is a local
and `run` has not been assigned. The `except BaseException:` that calls
`_abandon_unstarted_run` guards only the block *after* `run` exists, so nothing
reclaims the child. The `run()` path is the same shape at
`_run_subprocess_with_streams` (`_subprocess_stream_run.py:157-171`): the spawn
context and the consumer pair are built on the line *after* the
`_RunTaskOwnership` constructor starts evaluating, still inside the
`tasks = ...` assignment, so a raising composition escapes
`_await_direct_completion` with no terminator behind it.

**This matters more after EP-M2, not less.** The plan's premise is that the
failure "occurs before dispatch" — the emission path is fine. But EP-M2 makes
the factory build the emitter by *binding argv, cwd, env, project, and the
execution id*, which is strictly more work at exactly the point where a failure
is unrecoverable. The hoist therefore widens a pre-existing leak window rather
than narrowing it, and V3 cannot honestly be marked satisfied for the command
paths without deciding what to do about that.

**Scope decision, per the plan's own bounds.** Three production modules touch
the seam — `_line_callbacks.py`, `_subprocess_streams.py`, and
`_pipeline_stage_streams.py` — and EP-M2 edits two of them. A fix is a third,
separate edit site: it changes *where ownership of a spawned child begins* on
both command paths, which is a behavioural change rather than a performance
one, and it lands in `_line_stream/spawn.py` and `_subprocess_stream_run.py`,
neither of which EP-M2 otherwise touches. The plan's Tolerances section
reserves that for its own approval, so the fix is **out of scope for EP-M2**
and is recorded here as a finding with its own follow-up, rather than being
folded silently into a performance change.

V3's actionable remainder, which is in scope: make the *emission path* fail
before dispatch (clock failure) and add no tasks, assert a later hook's failure
leaves the earlier scheduled prefix in `pending_tasks`, and pin the pipeline
path's reaping — which is real and demonstrable. The two command-path "proves
the existing owner reaps" cases are recorded as **contradicted**, not skipped,
so a later reader cannot mistake the gap for coverage.

### 2026-09-27: V3 completed — the pipeline reaping case, and why it needed its own

The hook-contract half of V3 landed first (five cases in
`test_line_event_emission.py`), and the reaping half went to
`test_pipeline_process_lifecycle.py`, which already owned that seam and carried
the one existing sibling case. The split is not cosmetic: the sibling asserts
the same cleanup for the *same* reason but reaches it by a different route.

The existing case fails the *spawn* (`create_subprocess_exec` raises
`FileNotFoundError`) on stage two. The new case fails the *preparation* — the
stage spawns successfully, then `_create_stage_capture_tasks` raises — which is
the route a failure in the hoist's own code would take, and the route
`_spawn_pipeline_stages` reaches only after appending the process. Both run
through the same `except BaseException` in `_spawn_pipeline_processes`, so
before mutation-checking it was not obvious the new case added anything.

It does. Narrowing that guard to `except OSError` kills *only* the new case; the
`FileNotFoundError` sibling still passes, because its exception is an
`OSError`. That mutation is the evidence that the new case covers a region no
existing test reached, and it is the reason the case is worth its place rather
than being a restatement. The other three mutations — dropping
`_terminate_all_shielded`, leaving the capture tasks uncancelled, and starting
teardown without awaiting it — kill it too, and the second hangs it to the 30s
`pytest-timeout` rather than failing a comparison.

The anti-vacuity assertion is `len(spawned) == len(stages)`: a run that never
got as far as the second stage's factory would satisfy every other assertion
about the first stage alone and read as a pass.

### 2026-09-27: the 400-line cap applies to `tests/`, and pytest-bdd 8 scopes steps per module

Two corrections found while clearing `python-lint`.

**The ceiling applies here.** `pyproject.toml` sets `max-module-lines = 400`,
and `PYLINT_TARGETS ?= benchmarks conftest.py cuprum scripts tests` with
`recursive = true` means `tests/behaviour/` is linted — unlike
`cuprum/unittests`, which the walk never enters. V4 left
`tests/behaviour/test_structured_events.py` at 795 lines, so `python-lint`
failed on C0302 while the earlier gates had passed: `interrogate` and
`ruff check` are both green on an over-long file. The file was split three
ways, following the `test_*_behaviour.py` + `_*_support.py` convention already
in the directory:

| module                          | lines | contents                                                      |
| ------------------------------- | ----- | ------------------------------------------------------------- |
| `test_structured_events.py`     | 348   | scenario declarations, `behaviour_state`, the `Then` steps    |
| `_structured_events_steps.py`   | 277   | the `Given`/`When` decorators                                 |
| `_structured_events_support.py` | 288   | state keys, the two protocols, the run helpers, normalization |

**Where the assertions had to go.** Ruff's `S` rules ban bare `assert` outside
`test_*.py`, and every `_*_support.py` in the directory has zero asserts. So
the assertion-carrying helpers could not move to the support module:
`retained_events` and `normalize_event` have no asserts, but the "fail loudly
when empty" guard had to become a raised `AssertionError` to survive the move.
The `Then` steps stayed in the `test_*.py` module for the same reason.

**pytest-bdd 8 scopes a step to the module that defines it.** `given`/`when`/
`then` do not register into a global registry: each writes a *pytest fixture*
into the **calling module's** `f_locals` (`get_caller_module_locals` in
`pytest_bdd.utils`, consumed by `StepFunctionContext`), and resolution goes
through `request.getfixturevalue`. A plain `from ... import given_x` therefore
registers nothing — all seven scenarios failed with
`StepDefinitionNotFoundError`. The existing precedent
(`test_telemetry_adapters.py`) works around this by re-registering each step
with its literal text,
`then("the span records output as events")(_tracing_steps.assert_span_events)`;
that duplicates the step text, and duplicating it also changes the Gherkin
snapshot counts and the `.feature` coverage. The split instead loads the step
module as a plugin:

```python
pytest_plugins = ("tests.behaviour._structured_events_steps",)
```

so pytest collects the step module's fixtures and the steps resolve with no
restated text. Verified: 9 passed, 1 snapshot passed — identical to the
pre-split module — and the full `tests/behaviour` suite is 164 passed, 16
skipped, 5 snapshots passed.

### 2026-09-27: `ambrleaks` reads a doubled backslash in snapshot text as a UNC path

Clearing `python-lint` past pylint exposed two checks the earlier C0302 abort
had left unobserved. One was a real defect in the V4 snapshot:

```text
ambrleaks: 3 finding(s)
tests/behaviour/__snapshots__/test_structured_events.ambr:8: [snapshot-windows-path] ...
```

The rule is `\b[A-Za-z]:\\[^\s"']+|\\\\[\w.$-]+\\[^\s"']+` — an absolute
Windows or UNC path. The offender was the probe's own `-c` argument, which the
snapshot normalizes only its first element of, leaving the script text in place:

```text
"import sys; sys.stdout.write('beta\\nalpha\\n');sys.stderr.write('gamma\\n')"
```

The scanner masks the matched text before reporting, so the value had to be
recovered by re-running the rule by hand. `\\nalpha\\n` matches the UNC
alternative: `\\` + `nalpha` + `\` + `n`. It is a false positive in intent —
the text is Python escape syntax, not a path — but the committed snapshot is
what `ambrleaks` gates, so the probe now writes newlines with `chr(10)`,
matching the spelling `given_observed_pipeline` already uses. Regenerating the
snapshot cleared all three findings, and the probe still delivers `beta`,
`alpha`, and `gamma` in order.

The other previously unobserved check, df12-python-lints, found one real
`C9102` (an assert without a failure message) on the anti-vacuity witness in
`test_the_probe_run_preserves_per_stream_line_order`; it now carries one. Both
findings are recorded because neither is visible from the gate that aborted:
`ruff check`, `interrogate`, and a 10.00/10 pylint rating were all green while
both were outstanding.

### 2026-09-27: the `transition_privacy` trybuild timeout is environmental

The EP-M1 gate run reported `cuprum-streams`'s `transition_privacy` trybuild
suite as a timeout at the 600 s allowance:

```text
TIMEOUT [ 600.034s] cuprum-streams::compile_tests transition_privacy
```

The suite is *build*-bound: trybuild compiles a scratch crate, so an allowance
kill says nothing about the assertions. Four independent lines of evidence
place the cause outside this branch.

1. **The Rust diff is empty.** `git diff --stat origin/main HEAD -- rust/`
   produces no output, and the whole-branch diff against `origin/main` is three
   documentation files. There is no Rust change for the suite to regress on.
2. **Its sibling passed in the same run**, from the same override and
   allowance: `cuprum-rust::compile_tests compile_time_ui` PASSed at
   `102.761s`. A shared-tier problem would have shown up in both.
3. **The two suites are not comparable by case count.** `cuprum-streams` has
   two `compile_fail` cases and no pass cases; the killed scratch directory
   held only `Cargo.lock`, `Cargo.toml`, and `main.rs`, that is, it was stopped
   while still compiling a dependency rather than while running a case.
4. **The host was oversubscribed.** Load average was 11.18/13.05/13.56 on a
   6-core box, with other agents' `rustc` processes observed at 94–107% CPU in
   the `netsuke` and `axinite` worktrees.

`gh run list --branch main` reports both CI and Coverage as **success on
`991dee64`**, which is this branch's own base commit, so the same suite passes
on the same tree in CI. `docs/coverage-timeout-tiers.md` already documents this
exact failure class, describing a gate that killed trybuild "while it was still
compiling a dependency, that is, **while it was healthy**", and notes a 277 s
versus 124.884 s spread that "is `sccache` and machine load, not the test".

The timeout occurred in a `scrutineer` run, not in a run of this branch's own
gates, and is recorded here rather than in the code because there is nothing in
the code to change. The residual risk is that a `make test` run under the same
load reports it again; the disposition is to re-read the tier's log rather than
rebuild anything.

### 2026-09-27: EP-M2 is BLOCKED — the hoist as designed cannot reach 10%

This is the Tolerances stop condition, reached *before* any runtime edit. It
was measured from the committed control capture, not estimated.

**The mechanism.** The classifier's nearest-caller resolution is the whole
argument, so it is worth stating exactly. Each matched generated constructor
gets its rule from the *closest* matching caller frame. In the control capture
both rules resolve like this:

```text
 6474  ExecEvent.__init__ via _StageObservation.emit
         nearest caller pattern: emit (cuprum/_pipeline_types.py)
 4230  _EventDetails.__init__ for the per-line payload
         nearest caller pattern: _event_details (cuprum/_line_callbacks.py)
```

`ExecEvent.__init__` is reached through `_StageObservation.emit`, which lives in
`cuprum/_pipeline_types.py`. The rules file lists `emit_line` and
`_LineEventEmitter` as *callers*, and proximity picks the nearest — but `emit`
sits closer to the constructor than either, and it is not something a hoist in
`_line_callbacks.py` can move. Hoisting therefore leaves **every one of the
6474 `ExecEvent.__init__` samples in the numerator**. Only the `_EventDetails`
rule is actually addressable by this design.

**The arithmetic.** D = 30822, and the 10% threshold is
`100·N/D ≤ 10` ⇒ `N ≤ 3082`. Removing the entire `_EventDetails` rule leaves

```text
 N = 6474   share = 21.0045%   (one ExecEvent.__init__ per delivered line)
```

against a budget of 3082. That is 2.1x over, with 100% of executable
construction removed and none of the retained kind. No local refinement within
this design can close it, because the plan explicitly *retains* the ordinary
per-line `ExecEvent` constructor and forbids the alternatives that would remove
it.

**Why the refinements are both already ruled out.** The Tolerances section
allows "argument-passing and local-binding variants of ordinary `ExecEvent`
construction". Both were priced:

- *Argument passing.* `ExecEvent(**kw)` measured 2735 ns against 2431 ns for
  positional — 11% better, nowhere near the 2.1x needed.
- *Local binding.* The 16 optional fields are what cost, and they are passed
  explicitly by `emit`; hoisting the values cannot avoid re-passing them.

**The real cost driver — measured, and not what the plan assumed.** The Risks
section predicted failure "because a fresh `ExecEvent` still initializes 23
slots". Slot initialization is not the driver. Isolating the variables:

| variant (all `frozen=True, slots=True` unless noted) | ns | vs 2-field |
| --- | --- | --- |
| 2 fields | 239 | 1.00x |
| 2 required + 15 defaulted | 1238 | 5.20x |
| 0 fields | 68 | 0.28x |
| `frozen=False`, 2 fields | 89 | 0.38x |
| handwritten `__slots__` class | 90 | 0.38x |

The cost tracks the **count of defaulted fields**, at roughly 70 ns each, and
`frozen=True` multiplies the floor by ~2.7x on top. `ExecEvent` has 16 defaulted
fields of 27, and `_EventDetails` has 15 of 16. The retained constructor is
expensive because of its 16 defaults, not because it is frozen or slotted.

A field-count sweep confirms the shape. Positional construction, single
process:

| fields | ns | of 27-field |
| --- | --- | --- |
| 11 | 1435 | 46.2% |
| 16 | 2046 | 65.9% |
| 27 | 3106 | 100% |

So consolidating `ExecEvent`'s 16 optionals into one sub-record would remove
about 54% of constructor cost — large, but still not enough on its own, since
`6474 · 0.46 ≈ 2978` only skirts the 3082 budget and would be measured against
an unchanged D while adding the sub-record's own construction.

**What this means for the milestone.** R3 cannot be discharged by the planned
hoist, and EP-M2 must not be implemented as written. Per the plan's own
instruction, this is recorded as BLOCKED with measurements and options rather
than worked around by weakening acceptance — 21.00% is not 10%. Consolidated
options for the required design revision:

1. **Consolidate the defaulted fields.** Collapse `ExecEvent`'s 16 optionals
   into one sub-record so a per-line construction brings fewer defaults.
   Largest measured lever (~54% of ctor cost), but changes the public event
   representation, which the Tolerances section marks as a stop-for-approval
   change. Fails R2's payload-parity reading unless parity is redefined to the
   flattened view.
2. **Re-scope the measurement.** Split the gate's input so the *lifecycle*
   events and the per-line events are reported separately, and hold only the
   per-line share to 10%. This is arguably what the roadmap meant — the
   numerator was never meant to include lifecycle construction — but it is a
   change to acceptance, so it needs approval, not assertion.
3. **Change the callback's shape.** Drop the per-line event emission in favour
   of one event per run carrying a line sequence. This removes the per-line
   construction outright, at the cost of the observation contract V2 and V4
   currently pin, so it is a design change well beyond 5.2.1's scope.
4. **Accept the floor and close 5.2.1 as partial.** Record 21.00% as the
   measured floor, mark R3 unmet, and leave the roadmap item unchecked so the
   remaining work is visible rather than absorbed.

Options 1 and 2 are the only ones that keep the current observation contract;
option 1 is measured, option 2 is a re-scoping argument that needs the
roadmap's intent checked against 5.2.2 and 5.2.3 before it is claimed.

**Independent corroboration that the samples are real constructor time.** The
capture's own arithmetic and the microbenchmarks agree without being fitted to
each other. One repeat delivers 28,256,364 lines in 316.24 s; the consume
subtree holds 94.9% of parent samples, so roughly 10.6 µs of parent time per
line, of which the two constructors take 34.7% — about 3.7 µs. The standalone
cost of the same two constructors is 3.25 µs + 2.25 µs ≈ 5.5 µs. Same order,
same two constructors, derived independently. Separately, every one of the 6474
`ExecEvent.__init__` samples carries `emit_line` on its stack, so the per-line
population is exactly the whole match total — there is no lifecycle residue to
exclude.

**Evidence reproduction.** Parsing and classification both ran through the
gate's own code (`_line_event_profile_model.parse_capture` /
`load_rules`, `summarize_line_event_profile.classify_capture`), not a
re-implementation, and reproduced the committed artefact exactly —
`parent_samples 32468`, `consume_samples 30822`, `construction_samples 10704`,
`share 34.7284%`, matching `dist/profiles/5-2-1-event-details/control-1/` field
for field. The microbenchmarks are `timeit` in a single process on this host
and are comparative only; they are not capture-derived and are not offered as
gate evidence.

### Earlier discoveries

The roadmap's source line numbers are historical. Use the symbols and paths
below rather than those numbers. The read-size plateau has already landed;
5.2.1 must compare against the current 64 KiB baseline.

Table 4 combines `ExecEvent` and `_EventDetails` construction without providing
an exact machine-readable classifier or denominator. Current measurements must
publish both whole-parent and consume-subtree shares and state the chosen gate
explicitly. Historical `perf` folded output omitted Python frames; that is not
evidence of zero Python construction cost.

The current worker accepts `--repeat-count`; reproduction examples in the
historical baseline use `--repeat`. New instructions use the current spelling.

The context-pack Model Context Protocol (MCP) service failed both creation and
listing because an existing pack exceeded its 524288-byte limit. Investigators
exchanged source paths and findings instead. No shared service files were
modified. CodeGraph indexing succeeded, but dynamic callback relationships
require checking the explicit callback factory bodies as well.

## Decision log

- 2026-09-19: Propose a shared private closure factory on `_StageObservation`,
  used by both current line-callback factories. It binds stable metadata once
  and uses the ordinary `ExecEvent` constructor per line. This removes the
  transient `_EventDetails` allocation without adding another state container.
- 2026-09-19: Keep generic `emit` for lifecycle events and as the independent
  test oracle for line payloads. This is its continuing production role, not a
  temporary compatibility layer. No aliases or legacy private wrappers are
  needed solely to stage the change.
- 2026-09-19: Treat reaching 10% as uncertain. Pandalump and Wafflecat's review
  rejected assuming that eliminating one constructor proves the target.
  Replacing generated initialization with differently named work must not
  manufacture a passing profile.
- 2026-09-19: Telefono and Doggylump require fresh retained events, per-event
  clock reads, unchanged task ownership, and explicit setup-failure coverage.
  Buzzy Bee and Dinolump require complete attribution inputs and current
  controls, not just a truncated profiler ranking.
- 2026-09-19: No new ADR is presently required: this is private allocation
  tuning within the existing observation architecture. Record its scope and
  lifetime in the design document. A representation or dispatch change would
  need separate approval and an architectural decision record if substantive.
- 2026-09-26: Retarget the hoist to `_compose_line_callbacks` in
  `cuprum/_line_callbacks.py`, the shared seam that now owns per-line
  observation for both paths. Add a private `_LineEventEmitter` there — a
  frozen slotted dataclass holding the per-stream stable metadata and the bound
  `_StageObservation`, with a `__call__(line)` that reads the clock once and
  constructs one fresh ordinary `ExecEvent` per line — and have composition
  return an instance of it instead of the current closure. This keeps the
  plan's stated design ("the ordinary `ExecEvent` constructor per line", one
  metadata bind per stream, no template event, no `replace`, no
  `object.__new__`, no slot mutation) while removing the per-line
  `_EventDetails` allocation and the per-line `argv_with_program` rebuild.
- 2026-09-26: The plan's proposed `_StageObservation.make_line_emitter` method
  is **not** adopted. Two reasons: `_StageObservation` is frozen with
  `slots=True` in a module already at 299 of the 400-line ceiling, and adding a
  line-emission method there would couple the stage-observation type to
  `LineStreamName` and to the composition seam for no benefit. A private
  emitter owned by `_line_callbacks.py` keeps the same binding semantics with a
  smaller blast radius. The `Literal["stdout", "stderr"]` phase type in the
  signature is preserved exactly as proposed. This is a target-site adjustment
  within the same approved design, not a representation or dispatch change.
- 2026-09-26: Do **not** turn `SafeCmd.argv_with_program` into a
  `cached_property`. Caching it on the command object would freeze the tuple
  for the object's lifetime, so a caller mutating the argv input list after
  constructing the command would keep getting the pre-mutation tuple — a
  semantic change to a public property, which the tolerances forbid without
  approval. The hoist solves the same cost by reading the property once at
  per-stream preparation instead of once per line.
- 2026-09-27: Resolve overlapping construction rules by **caller proximity**
  (nearest matching caller wins, ties on rule order), not by declaration order.
  Measured: declaration order alone attributed all 134 construction samples of
  a real probe capture to the broader `ExecEvent` rule, leaving the
  `_EventDetails` rule empty. Proximity is what the stack actually encodes —
  the nearest caller is the one invoking the constructor — so it survives a
  rules file being reordered.
- 2026-09-27: Drift is judged **capture-wide and per rule**, not per frame or
  per stack. A rule is drifted when its callers carry real weight inside the
  consume subtree but it matched nothing. The two rejected alternatives and
  their concrete false positives on the control capture are recorded in
  Surprises & discoveries; they are rejected on evidence, not on taste.
- 2026-09-27: Criteria are counts, never keywords. The V1 red test asserts
  zero `_EventDetails` constructions and zero `argv_with_program` reads while
  delivering lines, and the V5 classifier asserts weighted sample fractions.
  Neither greps source text, so neither can be satisfied by a rename.
- 2026-09-27: Split the classifier at the parse/classify seam to satisfy the
  repo's own 400-line pylint ceiling, rather than asking for a suppression.
  Model and parsers move to `benchmarks/_line_event_profile_model.py`; the gate
  re-exports them so its entry point is unchanged, and a test pins the
  re-export surface to the model's own objects so the two cannot drift.

## Outcomes & retrospective

Planning has identified a narrow implementation and an honest stop condition.
Draft PR #433 publishes the reviewed plan and proposed design note. The initial
plan commit is `ee8026b2`; `make fmt`, `make check-fmt`, `make markdownlint`
(including spelling), `make nixie`, and `git diff --check` passed. The branch
tracks its matching `origin` branch. Implementation approval remains pending.
No runtime changes, benchmark runs, or implementation acceptance claims belong
to this draft. Record actual results and gate evidence at each milestone.
Before COMPLETE, reconcile discoveries with the design, guides, ADRs, and
roadmap; retain rejected options and the reason for each rejection.

## Context and orientation

An observe hook receives structured `ExecEvent` values. A stream callback is
called after decoding each complete stdout or stderr line, or its final
unterminated fragment. A stage observation holds the command, resolved context,
hooks, clock, execution identifier, and list of pending observer tasks.

`cuprum/events.py::ExecEvent` defines the public payload.
`cuprum/_pipeline_types.py::_EventDetails` currently transports optional
per-event fields into `_StageObservation.emit`, which constructs `ExecEvent`.
`cuprum/sh.py::SafeCmd.argv_with_program` creates the full argument tuple on
access. That tuple need not be reconstructed for every output line.

The two production callback entry points are
`cuprum/_subprocess_streams.py::_create_stream_callback` and
`cuprum/_pipeline_stage_streams.py::_create_stage_line_observer`. Both
currently construct `_EventDetails(pid=..., line=...)` before generic emission.
Their callers create them after spawn, when PID is known. Keep their return
contract: no observe hooks means no line callback, preserving the cheaper
consume path.

`_StageObservation._emit_event` invokes
`cuprum/_observability.py::_emit_exec_event` and retains tasks even when a
later hook raises. The latter checks the returned value for awaitability; a
normal synchronous function can return an awaitable. This behaviour must
survive. `emit_fail_fast` deliberately strips argv, cwd, env, and tags and must
not use the ordinary metadata cache.

Existing coverage includes `cuprum/unittests/test_cqrs_helpers.py`,
`cuprum/unittests/test_observe.py`,
`cuprum/unittests/test_cqrs_hook_behaviour.py`,
`cuprum/unittests/test_stream_line_boundaries.py`, and
`tests/behaviour/test_structured_events.py` with
`tests/features/structured_events.feature`. Reuse their fixtures and keep new
focused modules small. `benchmarks/tee_profile_worker.py` runs the real parent
consume workload; `benchmarks/summarize_folded.py` ranks stacks but its top-30
report alone cannot establish this gate.

## Conformance basis

The source revision for this plan is
`861fe2f053645311482141f155baeaa70dca0299`. No separate Terms of Reference
exists for this task. Governing documents at that revision are
[roadmap 5.2.1][roadmap], [Cuprum design §8.1.3][design-events],
[ADR-002][adr-002], [ADR-008][adr-008], [Table 4 of the tee baseline][baseline]
, and the [read-size evidence](../tee-hotpath-read-size-sweep-2026-08-29.md).

Use these selective trace links throughout implementation:

- R1, roadmap field hoisting: EP-M2, evidenced by V1's metadata access counts
  and absence of per-line `_EventDetails` construction.
- R2, unchanged observable payloads and hooks: EP-M1 and EP-M2, evidenced by
  V2–V4, public dataclass parity, and real-process behavioural scenarios.
- R3, construction share at most 10%: EP-M2 feasibility and EP-M3 acceptance,
  evidenced by V5's committed samples and reproducible classification.
- R4, Python-first tuning and unchanged pump observation: every milestone,
  evidenced by the scoped diff, unchanged dispatcher, and sequential gates.

Read [documentation contents][contents], [repository layout][layout],
[documentation style][style], [developers' guide][developers], and
[users' guide observation contract](../users-guide.md) before editing. For
measurement helper changes also read
[scripting standards](../scripting-standards.md).

Load `execplans`, `codegraph-mcp`, `python-router`, and `rust-router`. The
useful Python follow-on skills are `python-data-shapes`, `python-testing`,
`hypothesis`, and `python-quality-tools`; load only those needed for the active
milestone. Use `leta` for Language Server Protocol navigation and CodeGraph for
relationships; check dynamic callback connections against source. Use
`firecrawl-mcp` for external facts, `logisphere-experts` for design exceptions,
`commit-message` for commits, and `pr-creation` for publication. Rust Router is
an explicit boundary check here: no Rust tests, Kani model, or Verus proof is
introduced unless a separately approved Rust change creates such obligations.

## Proposed implementation and interfaces

**Revised 2026-09-26 (see Surprises & discoveries).** Add one private
`_LineEventEmitter` to `cuprum/_line_callbacks.py`, and have
`_compose_line_callbacks` return it in place of the current closure. Its
conceptual interface is unchanged from the original proposal — a
`Literal["stdout", "stderr"]` phase and a post-spawn `int | None` PID, returning
`Callable[[str], None] | None` — but it is a frozen slotted dataclass rather
than a `_StageObservation` method, for the reasons recorded in the decision log.

**Revised 2026-09-27 (EP-M1, see Surprises & discoveries).** This is the only
production edit EP-M2 makes. Both existing callback factories reach it
structurally, because both already call `_compose_line_callbacks`; no second
edit site exists. The classifier is split across
`benchmarks/_line_event_profile_model.py` (shapes and parsers) and
`benchmarks/summarize_line_event_profile.py` (classification, reporting, CLI),
which keeps each under the repo's 400-line pylint ceiling.

The original proposal, retained for traceability: add one private
`_StageObservation.make_line_emitter` method, taking a
`Literal["stdout", "stderr"]` phase and a post-spawn `int | None` PID, returning
`Callable[[str], None] | None`. The name is proposed, not an existing API.
Preserve the current callback factories' optional PID contract, including test
doubles; do not assert or coerce it to an integer. First return `None` if the
hook tuple is empty. Only then bind programme, full argv, cwd, env, PID, phase,
tags, project, execution ID, the clock callable, and the existing bound
`_emit_event` dispatcher into the closure.

On each call, invoke the bound clock exactly once and construct a fresh
`ExecEvent` from the captured values, current line, and timestamp. Omit
optional fields only where their existing defaults equal the generic line
event's values. Do not use a template event, `dataclasses.replace`, copying,
`object.__new__`, or private slot mutation. Those approaches retain field
initialization cost or add semantic risk and are outside this design.

Make both existing callback entry points delegate to that factory, preserving
their logging, reader selection, output ordering, and cleanup boundaries. The
factory allocates metadata once per stream, not once per command builder or
process-wide. Keep generic lifecycle `emit` and fail-fast emission intact.
Before adding the method, confirm no equivalent factory exists using CodeGraph
and record the search result. Document its scope as private line observation
shared by single-command and pipeline execution.

## Verification plan

The obligations below are implementation invariants, not claims of formal
proof. No new contractual business algorithm or non-trivial mathematical lemma
is introduced. The relation to establish is that substituting captured stable
fields leaves each event value equal to the old constructor at the same clock
value. Named tests plus generated comparisons exercise that relation; real
process tests cover the integration boundary that an abstract proof would omit.

### V1: preparation is constant in the number of lines

In new `cuprum/unittests/test_line_event_emission.py`, instrument argv access
and `_EventDetails` construction for a real observation. Parameterize 0, 1, and
100 lines, both phases, and no hooks. The registered-callback case reads argv
once at factory preparation and never during the line loop; PID and metadata
are bound there. No-hooks preparation returns `None`, does not read metadata,
and never reads the clock. Assert zero `_EventDetails` constructions for line
delivery. The old callback path must fail the multiple-line count assertion
before implementation. This is the red test; payload characterization alone is
expected to pass before and after. Exercise both existing production callback
factories for the red test, not the proposed method: a missing-method error is
not evidence of the performance bug.

### V2: every fresh event preserves the complete payload

Use a deterministic clock and compare every field returned by
`dataclasses.fields(ExecEvent)` against generic `emit`, using matching stage
state and execution IDs. Compare fields directly: `dataclasses.asdict` is not
the oracle because recursive copying of read-only mappings may fail. Include
both integer and absent PIDs. Check exact type, frozen assignment failure,
field order, constructor defaults, equality, and representative `repr` output
against ordinary events.

In new `cuprum/unittests/test_line_event_emission_properties.py`, generate
Unicode lines, including empty and repeated lines, both phases, finite
non-monotonic timestamps, and short sequences of 0–30 events. Generate distinct
stage metadata and interleave two stream emitters. Use Hypothesis's normal
settings with explicit boundary examples and no broad rejection filters. Assert
identical fields, one clock call per delivered event, distinct object
identities, and unchanged earlier payloads after later calls. Do not require
wall-clock timestamps to increase. A seeded event-reuse or fixed-timestamp
mutation must fail; restore it before gating. These tests sample the domain and
are not exhaustive proofs of the interpreter.

### V3: hooks retain their scheduling and failure contract

**Amended 2026-09-27 after measurement (see Surprises & discoveries).** The
post-spawn injection below is split by path, because the two command paths do
not share the property the plan assumed they did.

Add focused cases alongside V1, reusing existing asynchronous fixtures. Test
synchronous hooks, async hooks, synchronous callables returning awaitables, and
multiple ordered hooks. Retain events across an asynchronous yield and check
earlier lines and timestamps after later events have been emitted. A later hook
raising or cancelling must leave the previously scheduled tasks in the
observation's pending list; they must settle during existing cleanup. A clock
failure must occur before dispatch and add no tasks. The tests must fail if the
new path bypasses `_emit_event` or discards the scheduled prefix. Keep
unrelated exception-policy changes out of scope.

For the post-spawn injection, split as follows:

- **Pipeline path — in scope.** Inject a factory-preparation failure after the
  stages exist and assert every stage child is reaped and earlier stages are
  cleaned up. Measurement confirms the pipeline owner does cleanup on this
  path, so the case pins existing behaviour that EP-M2 must not break.
- **Command paths (`run()` and `lines()`) — out of scope, recorded as
  contradicted.** Measurement shows both currently leak the child when the
  composition factory raises during consumer preparation: the raising call sits
  before the first `create_task`, outside the region either path guards. The
  plan's requirement cannot be satisfied without an ownership change spanning
  at least `_line_stream/spawn.py` and `_subprocess_stream_run.py`, which the
  plan's own scope bounds reserve for separate approval. **Do not report these
  as passing, and do not weaken the assertion to make them pass** — record the
  gap, and let EP-M2's review decide whether to schedule the fix.

### V4: real streams preserve the externally visible sequence

Extend `tests/features/structured_events.feature` and its existing pytest-bdd
binding module with this specification, adapting existing step fixtures:

```gherkin
Feature: Stable execution events while observing output lines
  Scenario: Retained events preserve stream metadata and line order
    Given an observed command writes repeated and empty lines to both streams
    And the observer retains events until asynchronous callbacks settle
    When the command runs with captured and echoed output
    Then each stream has exactly the expected ordered line sequence
    And every line event retains its original line and timestamp
    And all line events carry the spawned process and resolved metadata
    And plan has no process identifier and exit retains the execution token
```

Add a pipeline scenario using final stdout and each stage's stderr, and a
concurrent scenario reusing one `SafeCmd` under distinct contexts. Assert
per-stream ordering and execution-ID isolation without inventing global
stdout/stderr ordering. Cover final unterminated fragments, CRLF boundaries,
empty output, and a non-zero child exit using existing fixtures where possible.
Use Syrupy for a small normalized lifecycle payload snapshot, paired with
explicit sequence, count, PID, and correlation assertions. Normalize actual
PID, path, execution ID, time, and duration only; never normalize line content,
phase, defaults, or stage ownership. Keep existing timeout, cancellation,
stdin, and sanitized fail-fast regressions in the full run. Characterization
scenarios should already pass before the optimization.

### V5: measured construction share is at most 10%

Use parent-only py-spy raw stacks as the primary Python attribution evidence,
with the same flags for control and candidate. Do not replace this with
cProfile timing: deterministic profiler overhead affects Python call costs.
Choose no native, idle, GIL-only, or subprocess flags, matching the historical
parent-only corroboration command. Record the installed profiler version.

Define the gating denominator D as the sum of sample weights of stacks
containing `_consume_stream_with_lines`. Define numerator N as the weight of
those stacks containing `ExecEvent` or `_EventDetails` initialization or
semantically equivalent allocation, copying, or default-field initialization.
Count each stack once even if it contains several matching frames. Report
`100 * N / D` and the same numerator over all parent samples. The consume
subtree is the explicit conservative gate; Table 4 did not formally specify its
denominator, so do not claim exact numerical comparability with 39%. All-parent
samples mean weighted stack records in the complete capture, excluding profiler
metadata and other non-stack records.

Identify generated constructor frames from their callers and the source
revision, not a blanket match on `__init__`. Commit the classifier rules and
matched frame names. Initialization moved into a helper stays in N. Unresolved
frames, a zero D, or missing expected constructor callers make a run
inconclusive. Do not count missing symbols as a zero-cost result. Require
initialization frames within identified caller paths; matching all of `emit`
would incorrectly attribute clock and dispatch work to construction.

Add a focused `benchmarks/summarize_line_event_profile.py`, reusing the
existing folded-stack parser in `benchmarks/summarize_folded.py` where
suitable. Test it under `cuprum/unittests/test_line_event_profile.py`: exact
weighted fractions, nested constructor frames counted once, replacement helper
frames, empty/malformed data, and unresolved generated frames. An
all-constructor synthetic profile must fail the gate; a 9-of-100 input must
pass and 11-of-100 must fail. Actual control frames must match non-empty
categories. Keep the helper under the scripting standards and avoid adding
runtime dependencies.

The proposed command accepts one folded-stack path, `--rules` for an explicit
JSON classification file, and `--output` for its JSON result. Emit weighted
`parent_samples`, `consume_samples`, `construction_samples`, both percentages,
`matched_frames`, `unresolved_frames`, and `status`. Exit 0 for a valid share
at most 10%, 1 for a valid share above 10%, and 2 for malformed, insufficient,
or unresolved input. A control run may intentionally exit 1; retain its result.
Regression timing is assessed separately from this single-capture command.

Collect three matched control/candidate profile pairs on the full wrap-76
fixture, each with one worker repeat. Require every valid candidate run to be
at most 10%, publish sample counts and dispersion, and require D of at least
10000 in every capture and a candidate share range of at most two percentage
points. These are proposed measurement tolerances. If samples are insufficient,
increase the whole-run repeat count equally for both variants and collect a new
complete set, retaining the discarded set. Persistently unstable results are
inconclusive and require documenting the interference before retrying. Also
collect at least five unprofiled paired rounds for the callback workload and
the echo/tee no-callback controls. Alternate control/candidate order per round
and retain every result. The candidate must not slow median `worker-result.json`
`wall_time_seconds` by more than 5% in each scenario separately; never pool
callback, echo, and tee timings. Report absolute time as well as the share to
expose denominator inflation. These are proposed regression tolerances, not a
new claim of a required 20% improvement for 5.2.1.

Use the same interpreter, lockfile, read size, fixture, machine, backend, and
warm-up policy. Run `--backend python` to isolate Python tuning. Perform one
unprofiled warm-up per variant before collection; direct worker runs do not
implicitly inherit the driver's warm-up. Capture command exit status and worker
status, exit code, read size, and expected line count. Small fixtures are smoke
tests only. No full gate or competing profile may overlap collection.

### Trusted assumptions and residual limits

The clock callable returns the event time; tests can verify call placement but
not operating-system clock accuracy. Ordinary frozen dataclass construction and
field introspection follow the Python standard library contract. Frozen fields
do not imply deeply frozen nested tag values. Existing asyncio task and process
ownership remains authoritative; integration tests exercise it rather than
attempting to prove asyncio. py-spy sampling is approximate and affected by
scheduling; repeated runs and raw evidence expose this limitation.

Primary references checked with Firecrawl on 2026-09-19 are
[Python dataclasses][dataclasses], [Python profiling][profiling], and
[py-spy documentation](https://github.com/benfred/py-spy). Frozen
initialization uses `object.__setattr__`, and `dataclasses.replace` calls
initialization again; neither a cached template nor renamed construction
establishes a speedup.

## Milestones and plateaus

### EP-M1: current control and contract characterization

After approval, record the implementation starting SHA and environment. Run
existing focused tests before changes. Add passing characterization tests for
V2–V4 and the tested profile classifier for V5. Capture the untouched control
profiles. Add V1's failing test and record its expected failure before runtime
edits; do not commit a failing suite. A coherent first commit may contain only
passing characterization and measurement tooling, after all code gates. R2 and
R3 gain evidence here; R1 remains open. Recovery is to retain control artefacts
and revert only the task's own uncommitted work.

### EP-M2: bounded optimization and feasibility

Implement the shared factory and update both production callers together. Make
V1 green, then run V2–V4 and inspect adjacent hook ownership. Remove all
experimental alternatives, temporary expected-failure markers, and probes. Run
the representative profile gate before claiming R3. If it misses, record
BLOCKED and present the measured limitation for design revision. Correctness
alone does not discharge R3. A successful plateau contains one production
factory, unchanged dispatch, passing gates, and reproducible profile evidence;
commit it as one atomic functional change. Recovery is an ordinary reviewed
revert, not a force reset of unrelated work.

### EP-M3: durable evidence and closeout

Commit full compact folded inputs, worker results, and classifier output under
`docs/profiling/5-2-1-line-event-emission/`. Keep large binary profiles and
fixtures in ignored `dist/`, never `/tmp`. Add
`docs/tee-hotpath-line-event-emission-5-2-1.md` with exact reproduction
commands, source SHAs, fixture checksums, runtime/profiler versions, flags,
per-run N and D, percentages, line counts, and raw unprofiled timing results.
Redact only machine-specific path prefixes, preserving frame names and line
identifiers.

Update `docs/cuprum-design.md` §8.1.3 with the implemented private factory's
scope, lifetime, and measured decision. Update `docs/developers-guide.md` with
the profiler classification/reproduction convention and cache invariant. Update
`docs/users-guide.md` with the measured performance guidance and the preserved
observation contract; do not invent new options or promise a universal speedup.
Add the evidence report to `docs/contents.md`.

Only after R1–R4 and V1–V5 pass, mark 5.2.1 done (`[x]`) in `docs/roadmap.md`
and link the evidence. Leave 5.2.2 and 5.2.3 unchecked. Update this plan's
living sections and status, run final gates, and commit closeout. Every
milestone checks the scoped diff against R1–R4 and records deviations; an
unapproved architecture deviation blocks continuation. No compatibility layer
is needed.

## Concrete steps

Run commands from the repository root. First inspect status, branch,
instructions, and the implementation starting revision; avoid overwriting
another agent's work. `make build` prepares the Python environment. Prefer root
Makefile targets and record output with `set -o pipefail` and `tee`.

For the focused baseline and later red/green checks, use:

```bash
set -o pipefail
make test-python PYTEST_TARGETS='cuprum/unittests/test_cqrs_helpers.py cuprum/unittests/test_observe.py' \
  2>&1 | tee /tmp/521-baseline-tests.out
FOCUSED_TESTS='cuprum/unittests/test_line_event_emission*.py cuprum/unittests/test_line_event_profile.py'
make test-python PYTEST_TARGETS="$FOCUSED_TESTS" \
  2>&1 | tee /tmp/521-focused-tests.out
make test-python PYTEST_TARGETS='tests/behaviour/test_structured_events.py' \
  2>&1 | tee /tmp/521-behaviour-tests.out
```

Run only paths that have been created at the relevant milestone. Expected red:
the multi-line argv/preparation assertion fails against the old callback path.
Expected green: all selected tests pass without unexpected skips or strict
expected-failure markers. Record exact test identifiers and counts when run.

Generate the historical fixture outside any measurement window:

```bash
uv run python benchmarks/deterministic_b64_fixture.py --seed 12345 \
  --raw-bytes 1610612736 --wrap 76 \
  --output dist/fixtures/seed12345-wrap76.b64 \
  --manifest dist/fixtures/seed12345-wrap76.json
```

The full fixture has 2175740011 bytes and 28256364 output lines per repeat. Its
SHA-256 is `51394f18e57972a681a2eb97c7c477d02d1d15b175247f518a60c331319774cc`.
A control capture, repeated with unique run labels, is:

```bash
PROFILE_PYTHON=$(uv run python -c 'import sys; print(sys.executable)')
PROFILE_DIR=dist/profiles/5-2-1-event-details/control-1
mkdir -p "$PROFILE_DIR"
py-spy record --format raw --rate 100 --output "$PROFILE_DIR/stacks.folded" \
  -- "$PROFILE_PYTHON" -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 \
  --mode echo --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 1 --read-size 65536 \
  --output "$PROFILE_DIR/worker-result.json"
uv run python benchmarks/summarize_folded.py "$PROFILE_DIR/stacks.folded" \
  --output "$PROFILE_DIR/ranking.json"
```

Use the identical worker invocation without `py-spy record` for warm-up and
unprofiled timings. Use candidate labels after optimization. Keep control and
candidate checkouts outside `/tmp` if alternating variants; use
`git-donkey-worktrees` when creating those checkouts, record each SHA, and use
the same environment without overlapping runs. Resolve the interpreter in each
checkout and verify its version and imported `cuprum` path. Do not create
separate Cargo caches. For tee/no-callback controls, use the unwrapped fixture
from `benchmarks/README.md`, remove `--line-callbacks`, and select `--mode tee`
or `--mode echo` respectively. Record the final classifier command in this
section if its approved interface changes; the generic ranking is not the gate.
EP-M1 implements this proposed classifier command:

```bash
uv run python benchmarks/summarize_line_event_profile.py "$PROFILE_DIR/stacks.folded" \
  --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output "$PROFILE_DIR/construction-share.json"
```

After the final edit of each code commit, delegate these gates to `scrutineer`
in sequence, with distinct `/tmp/521-*.out` logs:

```bash
set -o pipefail
make fmt 2>&1 | tee /tmp/521-fmt.out
make check-fmt 2>&1 | tee /tmp/521-check-fmt.out
make lint 2>&1 | tee /tmp/521-lint.out
make typecheck 2>&1 | tee /tmp/521-typecheck.out
make test 2>&1 | tee /tmp/521-test.out
make markdownlint 2>&1 | tee /tmp/521-markdownlint.out
make nixie 2>&1 | tee /tmp/521-nixie.out
```

Inspect formatter changes before continuing and restore only unrelated churn.
`make lint` includes spelling and blocking Skylos checks. Do not suppress real
dead code. The root gates include existing Rust checks without introducing Rust
implementation work. Read failure logs before fixing and rerunning affected
gates. Documentation-only plan commits require `make fmt`, `make check-fmt`,
`make markdownlint`, and `make nixie`, run sequentially; no new runtime test is
needed to validate prose.

## Validation and acceptance

A reviewer can reproduce the property and behavioural tests, see retained
stdout/stderr events with the same full payload, and observe that argv and
other stable metadata are not resolved in the line loop. Both production
callback paths must use the factory. No-hook streams must retain their existing
callback-free path, and lifecycle/failure sanitization must remain unchanged.

The committed report must allow recalculation of N/D from complete inputs. All
three candidate captures must be at most 10%, controls must use the same
classification, and unprofiled runs must meet the regression tolerance. Missing
Python symbols, skipped callback work, changed line counts, or a moved
constructor cost do not pass. Record exact gate commands, exit statuses, and
source revisions; a queued hosted check is not a passing result.

Report each gate as **passed**, **failed**, or **unobserved**, and never
collapse unobserved into passed. `make lint` aborts at its first failing
prerequisite, so sub-checks that sort after the failure did not run at all (see
the 2026-09-27 discovery above); a gate that stopped early proves nothing about
what follows it. Run `make spelling` alone before `make lint` for exactly this
reason.

## Idempotence and recovery

Use new artefact directories for every measurement; never overwrite the
control. Fixture generation can be retried after checking disk space and
verifying its manifest. Restore only task-owned mutations used for negative
controls and rerun the affected tests. Do not commit failing tests or temporary
prototypes. Revert a completed atomic commit if necessary and update this plan
with the reason. If approval is absent or a tolerance is exceeded, stop at the
last coherent plateau and preserve evidence for review.

## Revision note

2026-09-19: Drafted from live source and revised with the Wyvern findings and
six expert perspectives. Added an explicit feasibility gate, preserved hook
failure ownership, specified weighted sample attribution, and separated this
work from 5.2.2 and 5.2.3. Implementation remains subject to user approval. The
final document review preserved optional PID typing, made the red test exercise
existing factories, covered cleanup of earlier pipeline stages, and made
classifier commands and measurement tolerances explicit.

2026-09-19: Record publication and documentation-gate evidence after opening
draft PR #433. All implementation milestones remain pending explicit approval.

[roadmap]: ../roadmap.md#52-make-per-line-event-emission-cheap-for-line-callback-workloads
[design-events]: ../cuprum-design.md#813-structured-execution-events-observe-hooks
[adr-002]: ../adr-002-additional-rust-components.md
[adr-008]: ../adr-008-rust-pump-observation-channel.md
[baseline]: ../tee-hotpath-profiling-baseline-2026-06-12.md
[contents]: ../contents.md
[layout]: ../repository-layout.md
[style]: ../documentation-style-guide.md
[developers]: ../developers-guide.md
[dataclasses]: https://docs.python.org/3.13/library/dataclasses.html
[profiling]: https://docs.python.org/3/library/profile.html
