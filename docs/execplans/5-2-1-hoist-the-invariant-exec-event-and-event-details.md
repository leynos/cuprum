# Hoist invariant execution-event fields (5.2.1)

Status: **COMPLETE — R3 met at the revised 30% target, all three milestones
done, every deterministic gate green, and hosted CI green at the current
head.** EP-M2 is implemented, and the completed hoist measures **29.91%**
(median of three matched pairs, candidate range 0.0423 points) against the
**30%** bar approved on 2026-09-27. That is up from 28%, which the same
captures missed by 1.90 points; the revision is the one this plan's Tolerances
section required, and it was granted on the measurement rather than on a
further projection (see "The 30% revision, and why the margin is thin" below).
All four acceptance requirements R1–R4 are met. EP-M1 is complete; EP-M3's
evidence artefact (`docs/tee-hotpath-line-event-emission-5-2-1.md`),
documentation closeout (design §8.1.3, both guides, contents index), roadmap
tick, and changelog entry are all committed. The pass is *not* a weakening of a
met criterion for its own sake: the same collection shows the candidate 30.96%
faster in median wall time, the three controls exceed the 30% bar by 4.04 to
5.35 points, and the margin over 30% is 0.0586 points against a 0.0423-point
spread — thin, recorded as thin, and forecast to be inverted by 5.2.2.

Two full local suite runs carry the deterministic evidence, both on frozen
trees with nothing skipped or bounded: `3315c5c3` (the implementation and
evidence, all eight gates including `test-act`) and `d98fb5c9` (the review
dispositions, seven gates — `test-act` not re-run, justified as docstring-only
in the delta). Every commit above `d98fb5c9` is a documentation edit behind its
own hash-pinned docs-scoped sweep, so neither full run is superseded; the chain
is verified as a bijection in the retrospective. Hosted CI is green at
`e4a53306` — 19 checks pass, 3 deliberate skips, zero failures. The draft
status of PR #433 and the `CodeRabbit` app's resulting no-op are recorded as
open items, not as completed review.

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
artefact shows construction accounts for no more than 30% of the callback
consume samples. The historical approximately 39% is context, not a current
control. The measured result — 29.91% against a 34.29% control, on three
matched pairs — is established and committed.

**The 39% baseline does not reproduce, and its denominator was never stated.**
Its only source is a one-line table row in
`docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5, whose scope is a
narrower July fixture; the document names no denominator for it. This branch's
own committed control on the wrap-76 fixture measures **34.73%** of the consume
subtree and **32.97%** of all parent samples
(`dist/profiles/5-2-1-event-details/control-1/construction-share.json`), and a
small-fixture smoke run reads **47.86%**. Both percentages a reader might
plausibly have meant sit below 39%, so "falls from the 39% baseline" does not
name a real quantity for this scenario. The plan's statement that 39% is
"context, not a current control" is therefore correct, and should be read more
strongly than it was written: the roadmap's *baseline* is as unusable as its
*threshold*. V5's denominator and fixture are this plan's own definitions, so
the 10% bar is well-formed; only the comparison to 39% is not. This is the
strongest argument yet for settling the design question as an explicit
threshold revision, since the criterion cannot be evaluated as literally
written in either direction.

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
still exceeds the threshold, set this plan to BLOCKED, record the measurements
and options, and seek approval for a revised design. Do not declare the feature
complete merely because field hoisting or correctness tests pass.

**This clause was invoked twice, and both invocations were honoured.** The
first raised the threshold from 10% to 28% before implementation, because the
10% figure was unreachable by this design without breaking V2/V4's observation
contract and the 39% baseline it was compared against has no denominator in its
source. The second, on 2026-09-27, raised it from 28% to **30%** after the
completed hoist measured 29.91% — a measurement, not a projection, which is why
the plan was set to BLOCKED in between rather than re-sited unilaterally. Both
revisions changed an unmeetable criterion explicitly and on the record; neither
weakened a criterion the implementation had already met.

Because both revisions were granted, neither refinement this section budgets
was **needed to reach the criterion**: the two design changes that would have
closed the 28% gap were rejected on other grounds and are recorded in the
discovery entry "The 30% revision, and why the margin is thin". This is a
weaker statement than "not tried", and the earlier wording overstated it:
argument passing *was* priced — `ExecEvent(**kw)` at 2735 ns against 2431 ns
positional — in the entry that rules both refinements out, which is why that
entry, not this paragraph, is the authority on what was measured. Local binding
was reasoned about rather than measured, the cost being the 16 optional fields
that must be passed explicitly either way.

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

High impact, medium likelihood: caching metadata may not reach the threshold
because a fresh `ExecEvent` still initializes 23 slots. Mitigate with the early
measured feasibility gate; do not promise an unmeasured result. This risk has
**materialized and been priced**: the retained per-line `ExecEvent`
construction is what makes the original 10% unreachable, and the threshold was
revised to 28% on 2026-09-27 for that reason, and to 30% once the hoist
measured 29.91% (see the two threshold-revision entries). The risk is now that
a *partial* hoist — one that leaves argv or the payload per-line — is mistaken
for a complete one; the one-sample-either-side-of-the-limit boundary in V5's
tests is the guard, and it is written against the constant, so it has tracked
every revision without editing.

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
- [x] (2026-09-27) Full gate suite at `807cf62c`: 6 passed, 1 failed.
      `make fmt`, `check-fmt`, `markdownlint`, `spelling`, `typecheck`, and
      `test` all
      exit 0; `make lint` failed at `python-lint`. Two branch-attributable
      defects found, both fixed in this plan's own work (see the two 2026-09-27
      entries on interrogate and mdtablefix below). Eight `make lint`
      sub-checks were **unobserved**, not passing, because that target aborts
      at its first failing prerequisite.
- [x] (2026-09-27) Gate re-run at `902b05fb`, tree frozen and clean: **all
  seven gates pass**. `check-fmt` moved ahead of `fmt` so its pass is on its
  own merits. `make lint` completed its full chain in 66s, so all eight
  previously-unobserved sub-checks are now observed green. Plain `make test`
  failed 13 release tests on this host's `BASH_ENV` defect and passed under
  `env -u BASH_ENV` with `2543 passed, 0 failed` (2530 + 13 = 2543, nothing
  regressed); that is environmental, not branch-attributable. Evidence in the
  2026-09-27 entry below.
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
  - [x] CodeScene findings on this branch's four added files cleared. Four new
    files failed the *Pay Down Tech Debt* profile (one **critical** Low
    Cohesion rule among them); all four are fixed and `cs delta origin/main`
    reports no introduced findings. The 654-line test module is split by
    subject into four modules under the same `test_line_event_emission` prefix,
    with the test inventory verified unchanged at 22 functions. **Confirmed by
    the hosted check at `dbaba9ac`:** the `CodeScene Code Health Review (main)`
    check run completed `success` with *Quality Gate Passed* and 6 of 6 gates,
    on the *Pay Down Tech Debt* profile. Two CodeScene review *threads* remain
    open on `benchmarks/_line_event_profile_model.py`, but they are
    thread-comments from an earlier revision, not the gate: the gate itself is
    green at head, and the threads are not part of the required ruleset. See
    the 2026-09-27 CodeScene discovery below.
  - [x] The split's own lint fallout cleared, and `make lint` observed to its
    **end** for the first time: `ruff check`, `interrogate` (100.0%), `pylint`
    (10.00/10), `df12-python-lints`, `ambrleaks`, `skylos`, `rust-lint`
    (`lint-clippy`, `lint-whitaker`, `spelling`) and `github-actions-lint`
    (`yamllint`, `actionlint`) all ran. Ten `ruff check` defects arrived with
    the handwritten split headers and one R9112 survived the first fix; both
    rounds are recorded below, because each round left eight sub-checks
    unobserved behind the abort.
  - [x] (2026-09-27) EP-M1 exited at `dbaba9ac`, tree frozen and clean. All six
    deterministic gates observed to their end: `check-fmt`, `markdownlint`
    (with spelling), `typecheck`, `test`, `nixie`, and `lint`. `make lint`
    cleared nine sub-checks and then hit the intermittent host-only
    `actionlint` deadlock; the tenth was re-observed separately, bounded, on
    the path CI itself takes. See the 2026-09-27 actionlint discovery below for
    why that re-run is legitimate evidence rather than a substitution, and
    also for the measurement that falsifies this plan's earlier
    "deterministic" characterization of the hang.
  - [x] (2026-09-27) EP-M1 re-confirmed at `6f82a697` on tree `75b18498`, by an
    **independent** scrutineer run that did not take this plan's word for
    anything: it re-derived the change surface itself, and captured every
    gate's exit status out of band rather than reading log tails. Result —
    `check-fmt`, `typecheck`, `lint`, `markdownlint`, `nixie` all pass;
    `make lint` passed **unbounded** (exit 0, 52 s), so the narrowed path was
    not needed at all. `make test` went red on a single host-load flake
    (`test_idle_heartbeat_coordination.py::test_a_shared_sink_keeps_the_pipeline_keepalive_on_its_own_line`,
    a file this branch never touches); a bounded re-run at 04:56 passed clean with
    `2543 passed, 63 skipped, 6 xfailed in 145.85s` and nextest
    `125 tests run: 125 passed`, exit 0, tree unchanged. The flake test passed
    3/3 in isolation, and system load fell from 22.2 to 9.1 between the two
    runs. **This run also produced the controlled A/B that settles the
    actionlint question** — see the discovery below.

  - [x] (2026-09-27) Three matched control/candidate pairs and ≥5 unprofiled
    paired rounds. Collected at `f4d1010a` (candidate) against `01ec41bd`
    (control): six profiled captures, three unprofiled scenarios at five rounds
    each. All V5 regression tolerances pass — minimum D of 17413 against a
    10000 floor, candidate share range 0.0423 against a two-point allowance,
    and no scenario slowed by more than 5% (the candidate was faster in every
    one). Committed as `docs/tee-hotpath-line-event-emission-5-2-1.md`.
- [x] EP-M2: **implemented, measured, and accepted at the revised 30% target.**
  The hoist landed as a single production edit in `cuprum/_line_callbacks.py`
  (+132/−13): `_line_event_emitter` builds a frozen slotted `_LineEventEmitter`
  that resolves `program`, `argv`, and `project` once per observed stream, and
  `_compose_line_callbacks` binds it in place of the deleted per-line
  `_event_details` call. `cuprum/_pipeline_types.py` is byte-identical to its
  pre-hoist state. Both of V1's assertions pass as ordinary tests with their
  strict markers removed.

  The blocker recorded above is **superseded, not resolved in code**: the hoist
  still cannot reach 10%, because the per-line `ExecEvent` construction remains
  and is not addressable from `_line_callbacks.py`. The user approved revising
  V5's gate to 28% instead (see the threshold-revision entry), which the hoist
  was projected to clear — and, measured, cleared at 30% after missing 28% by
  1.90 points. EP-M2's remaining acceptance was therefore the V5 measurement,
  not a further code change, and that measurement is now taken. "No longer
  reconstructs invariant fields" holds for the `_EventDetails` half only: the
  per-line `ExecEvent` is still constructed and is recorded as a residual gap
  against the original wording of the roadmap item.

  Done: the hoist; V1 green; the dispatch path proven covered by sabotage (four
  scheduled-prefix tests fail when the emitter bypasses `_emit_event`). Also
  done, both found by the gate runs rather than by inspection: the classifier's
  DOC502 docstring defect, and the C0302 module-ceiling violation that a skipped
  `make lint` had concealed since `01ec41bd` (the classifier engine is now its
  own module, with the split proven behaviour-preserving by byte-identical
  output on a real capture). Also done: the V5 candidate captures (three
  matched control/candidate pairs, D >= 10000, candidate share range <= 2
  percentage points) and the gate suite at `6da258a6`. Both items this note
  once listed as not done were completed later: the full gate suite at the
  30%-revision commit, and EP-M3's closeout at `3315c5c3`.
- [x] EP-M3: commit representative profiler evidence, documentation, and
  completion of roadmap item 5.2.1 after all acceptance conditions pass.
  **Done.** The design revision EP-M2 was waiting on was approved and applied;
  the evidence is committed at `79302ae6` (six captures, thirty unprofiled
  runs, the 30% reclassification), the roadmap tick with the changelog entry at
  `16917587`, and the four documentation edits at `3315c5c3` (design §8.1.3,
  developers' and users' guides, contents index). The closeout gate run at
  `3315c5c3` passed all eight gates — `check-fmt`, `markdownlint` (with
  `spelling`), `typecheck`, `lint`, `test`, `nixie`, and `test-act` — with the
  tree clean before and after, so the evidence is valid for that HEAD and no
  sub-check was bounded or skipped. Logs under
  `/tmp/closeout-*-5-2-1-hoist-the-invariant-exec-event-and-event-details.out`.
- [x] (2026-09-28) CodeRabbit review dispositions landed at `d98fb5c9`, and the
  **full suite was re-run there** on a frozen tree: `check-fmt`, `markdownlint`
  with `spelling`, `typecheck`, `lint` (interrogate 100.0%, pylint `10.00/10`),
  `test` (`2549 passed, 63 skipped`; nextest `125 passed`), and `nixie` all
  pass, with `make lint` reaching `github-actions-lint`'s final recipe line
  unbounded. `make test-act` was not re-run and the omission is justified in
  the retrospective: the delta's only Python hunks are inside docstrings, and
  no workflow file or Rust source changed. Logs under
  `/tmp/final-*-5-2-1-hoist-the-invariant-exec-event-and-event-details.out`.
- [x] (2026-09-28) Ten documentation commits above `d98fb5c9`, each behind its
  own hash-pinned docs-scoped gate sweep, forming a chain re-checked
  **mechanically at `e4a53306`** rather than asserted: all ten links MATCH, and
  the commit-to-sweep mapping is a bijection over `d98fb5c9..HEAD`. The
  full-suite runs therefore remain the last word on `lint` for the code, with
  the documentation delta covered separately by re-running the pinned `skylos`
  over both documentation revisions — the sweeps alone do not cover it, because
  `make lint` reads Markdown (see the lint discoveries below).
- [x] (2026-09-28) PR description rewritten and applied to match the
  implementation, then re-applied after the review-driven and chain-driven
  revisions, each time verified by an independent read-back rather than by the
  PATCH response. PR title de-prefixed from "Plan: " and the Lody session link
  added to `## References`.
- [x] (2026-09-28) Hosted CI observed green at `e4a53306`, the current head: 19
  checks pass, 3 deliberate skips (`Kody Code Review`, `Loom model smoke test`,
  `automerge`), zero failures — including `coverage`, `benchmark-ratchet`,
  `lint-test`, all four `Typecheck and test` jobs, all five
  `build-native-wheels` jobs, and `verify-wheel-install`. `CodeRabbit` reports
  `pass` *because* the PR is still a draft and the app skips drafts; that is a
  skip wearing a pass, and it is recorded as one rather than counted as review.

  **Still outstanding, and owned by the user rather than this plan:** whether
  to take PR #433 out of draft. Until that happens the CodeRabbit *app*
  produces no GitHub-side threads, so the four review dispositions rest on the
  CLI agent's JSON stream and cannot be cross-checked against a threads API
  (see the caveat in the retrospective).
- [x] (2026-09-28) One further documentation commit, closing the last stale
  target reference: EP-M2's instruction still read "If it misses the **28%**
  threshold, record BLOCKED" three revisions after the bar moved to 30%.
  Corrected, with the revision noted inline; the dated quotation of the same
  sentence in this Progress section was deliberately kept. It went in behind
  the branch's eleventh docs-scoped sweep, whose gates are named and logged in
  the chain paragraph under Outcomes & retrospective rather than counted here.
  All five Markdown gates exited 0 with the gated file's digest identical
  before and after — so no gate observed a mutation mid-run — and, because the
  edit touches credit-bearing `_LineEventEmitter.emit_line` mentions, `skylos`
  was re-run over both documentation revisions as the sweeps alone cannot cover
  `lint`: control and edited give identical verdicts (2198 credited, 135
  uncertain, `unused_functions: 0`). The digests and the chain link are in the
  run logs rather than quoted here: a document cannot state its own content
  hash, since writing the value in changes it (the `chain9`/`chain9b` trap
  above). See the new Decision-log entry for why a stale number *in an
  instruction* is a different defect from one in a record.

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
which a re-run of the committed classifier reproduces exactly on every
gate-consumed figure.

**The control capture's source revision was not recorded, and V5 requires it.**
V5 says to "identify generated constructor frames from their callers and the
source revision". The capture itself predates the hoist by construction (that
is what makes it a control), and the commit that was checked out when it was
taken is written down nowhere — not in this entry, not in `worker-result.json`,
not in the profile directory. The revision is recoverable by inference: it is
the commit whose `_pipeline_types.py` still contains `_event_details` and
`emit_line`, i.e. the parent of the hoist commit. For the matched-pair
collection below, every run records its `HEAD` SHA beside its artefacts so this
gap does not recur. The captured data itself is sound; what is missing is the
provenance field, and the fix is procedural rather than a re-capture.

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

### 2026-09-27: the first full gate run — two real defects, and a corrupted verdict

EP-M1's gate run reported 6 passed and 1 failed. Both defects were
branch-attributable and are fixed; the more useful lesson is *how* the run's
evidence failed to be good evidence, in two independent ways.

**`make fmt` is not read-only, so it can launder the next gate's verdict.** The
first gate rewrote
`docs/execplans/5-2-1-hoist-the-invariant-exec-event-and-event-details.md` in
place, because the plan had been hand-edited since its last reflow and
`mdtablefix` had table separator padding to fix. `make check-fmt` then ran
against the *repaired* tree and passed. That pass therefore says nothing about
the revision that was actually checked out: `mdtablefix --check` against the
committed blob at `807cf62c` fails with `+96 -95`. `make fmt` is the one gate
here that mutates tracked files, so it must be run *after* `check-fmt` — or its
dirt read as a finding rather than absorbed. This is the same failure mode the
plan already records for rebases; it was not previously recorded for `fmt`.

**An aborting gate hides everything downstream of it.** `make lint` stops at
its first failing prerequisite, so after `python-lint` failed on five
undocumented closures, seven sub-checks never ran at all: `df12-python-lints`,
`ambrleaks`, `skylos`, `lint-clippy`, `lint-whitaker`, `spelling` (Rust), and
`github-actions-lint`. Their status is **unobserved**, and this plan's own
Validation section already requires that word. The corollary is that
"`make lint` passes" is a claim no single run can support unless it reached the
end, so the fix for `python-lint` is only half the work — the seven unobserved
checks still need a run that gets past it.

The five interrogate misses were one-line `async def record()` closures nested
in the hook-contract helpers; `interrogate --fail-under 100` is strict enough
that a nested coroutine's docstring counts, and a partial `~[~]`-style
tolerance is not available. Worth noting that `origin/main` sits *exactly* on
that threshold already, with 2 pre-existing misses in `scripts/tests/` that
round up to 100.0% — so this gate has no headroom, and any branch that adds
even one miss fails it. Those 2 are not branch-attributable and were left alone.

**A concurrent writer invalidates a run even when the gates pass.** HEAD moved
from `807cf62c` to `859f6489` mid-run — this plan's own commits, landed while
the gate suite was executing — and two of them touched the very `.md` file
`make fmt` had just repaired. So the run's docs-axis verdict describes a
revision that no longer exists. The mitigation for this plan is to freeze the
tree for the duration of a gate run and to state the tested revision explicitly.

**The re-run: green, with the freeze honoured.** After both fixes, all seven
gates pass at `902b05fb` with the tree clean before, during, and after, and
`git write-tree` (`5b87bf7f`) identical at both ends. Run order was changed to
put `check-fmt` *first* and `fmt` second, which is the concrete remedy for the
laundering above: `check-fmt` then passes on its own merits, and `fmt` leaving
the tree clean independently proves the file was already formatted. `make lint`
completed its whole chain in 66s, so the eight sub-checks the abort had hidden
(seven listed above, plus `pylint`, which was also skipped) are now
**observed** green rather than assumed.

**Two host conditions surfaced, neither branch-attributable.** The plain
`make test` run failed 13 release tests with
`failed to run git: fatal: not a git repository`; the identical gate passed
under `env -u BASH_ENV` with `2543 passed, 0 failed`, and 2530 + 13 = 2543
exactly, so nothing regressed. The cause is
`BASH_ENV=/home/leynos/.lody/bashenv`, which re-prepends `~/.lody/bin` inside
every non-interactive `bash -c` so the real `gh` wrapper shadows the test's
stand-in. The committed fix for it is not an ancestor of this branch, so
`env -u BASH_ENV make test` is the authoritative local invocation here; CI is
unaffected. Separately, this worktree is carrying `actionlint` processes from
the session's Stop-hook gate runs that have been blocked in `futex_wait_queue`
for over 27 hours — the documented shellcheck stdin deadlock, which `make lint`
did *not* hit when run directly. Neither changes this branch, and neither
should be "fixed" here.

### 2026-09-27: CodeScene failed the PR on four files this branch added

The CodeScene check reported `failure` on the pushed head while the seven
`make` gates were green, so it was reviewed before being dismissed. It is not
in the required status-check ruleset — but every one of the last twelve merged
PRs *passes* it, including ones that added a dozen new Python files, so passing
is this repository's real bar and the finding is branch-attributable rather
than ambient.

Reproduced locally with `cs delta origin/main --output-format json`, which
matched CI finding for finding. All four files are new on this branch
(`old-score: null`), and the gate profile is *Pay Down Tech Debt*, which
requires every new file to reach code health 10.00 — hence zero headroom:

| file                                      | score | findings                                                                                                     |
| ----------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------ |
| `test_line_event_emission.py`             | 8.03  | Lines of Code in a Single File (654 > 600), **Low Cohesion** (critical), Excess Number of Function Arguments |
| `benchmarks/_line_event_profile_model.py` | 8.79  | Complex Method ×2, Overall Code Complexity                                                                   |
| `_structured_events_steps.py`             | 9.38  | Code Duplication                                                                                             |
| `test_line_event_emission_properties.py`  | 9.68  | Excess Number of Function Arguments                                                                          |

**The three smaller findings.** The properties module's 5-argument assertion
helper took a 4-tuple; giving the generator a frozen `_InterleavedCase` record
reduced it to 4 arguments and removed the tuple unpacking. The behaviour module
duplicated one three-line body across three `when` steps, which now resolve the
scenario's command once in a shared `_run_scenario` helper. The benchmark
model's complexity came from inline validation, now factored into
`_require_object` / `_require_key` / `_require_array`; `load_rules` and
`_rule_from_json` each dropped under the threshold without changing a single
error message that a test asserts (only `"consume_frames"` is matched on).

**The 654-line module.** Two of its three findings are size and cohesion, and
they are the same root cause: the file carried four responsibilities. It is
split by subject into four modules, all keeping the `test_line_event_emission`
prefix so the plan's `FOCUSED_TESTS` glob still covers them:

| module                 | lines | subject                                               |
| ---------------------- | ----- | ----------------------------------------------------- |
| `..._support.py`       | 180   | stand-ins, the observation factory, the two recorders |
| `..._parity.py`        | 216   | payload parity against the generic `emit` oracle      |
| `..._prep_cost.py`     | 352   | the red preparation-cost assertions                   |
| `..._hook_contract.py` | 353   | hook scheduling and failure contract                  |

The fifth finding was `pending=` on `_make_observation`. That parameter was
redundant: `_StageObservation` already owns the list it extends, so the five
call sites now read `observation.pending_tasks` back, which is exactly what the
parameter took as input. The helper is down to four arguments.

Test inventory is unchanged, verified by comparing totals rather than by
inspection: the original module defined 22 test functions and the three split
modules define 22 between them, and every case count matches — 11 payload
parity, 5 hook contract, 21 properties, and 5 passed + 6 strict-xfailed for the
red cost assertions, which is the 21 passed / 6 xfailed the single file
reported. `cuprum/unittests/**` is excluded from both wheel builds, so the
split does not touch packaging.

**Meaning an empty `cs delta` result was checked before it was trusted.** A
tool that fails silently would look identical to a clean tree, and `cs` does
have a real failure mode here: it prints a version-upgrade banner ahead of the
JSON, so an unguarded `json.load` on its output raises. Its result was
therefore probed by re-adding the deleted 654-line module as an untracked file
and re-running: it reported the same three findings, proving the tool was live.
With the probe removed, the delta reports no introduced findings at all.

### 2026-09-27: the module split's leftover imports, and what `make lint` hides

Splitting a 1012-line test module by hand means writing four new import headers
by hand, and `ruff check` found ten defects in them — the split's own cost, not
a pre-existing one. Five were imports the moved tests no longer used; four were
annotation-only imports of private symbols, which belong in the
`if typ.TYPE_CHECKING:` block because nothing resolves them at runtime (this is
a *test* module, so the public-annotation exception recorded in Decision log
does not apply); one was `typing.Callable` where the repo bans it.

The last one is worth recording because the obvious fix is wrong. R9112
(`prefer-type-statement`) requires `type _RunHelper = ...`, and the alias names
two imports that are themselves `TYPE_CHECKING`-only, so a bare `type`
statement at module scope would put `CommandCatalogue` and `Runnable` in a
runtime scope that does not import them. The repository's answer is a guarded
pair — a `TYPE_CHECKING` branch with the real signature and an `else` branch
with the unsubscripted `cabc.Callable` — matching the four existing `type`
aliases in `cuprum/`.

**The gate ordering hid eight sub-checks for two rounds.** `make lint` aborts
at its first failing prerequisite, so the ten `ruff check` errors meant
`interrogate`, `pylint`, `df12-python-lints`, `ambrleaks`, and `skylos` never
ran, and neither did the whole of `rust-lint` (`lint-clippy`, `lint-whitaker`,
`spelling`) or `github-actions-lint`. Fixing the ruff errors did not finish the
job either: the next run got past ruff and then failed at `df12-python-lints`
with the R9112 above, leaving those same checks unobserved a second time. Only
the third run reached the end. This is the same trap the plan already records
for `make lint` generally, applied twice in succession, and it is why each
round's verdict is reported as passed / failed / **unobserved** rather than as
a count of green checks.

The third run passed the **entire** chain, `actionlint` included. The
shellcheck stdin deadlock noted above did not recur, which is consistent with
the recorded finding that it is a race rather than a deterministic failure.

### 2026-09-27: C0302 was masked twice over — once by an abort, once by a skip

`make lint` at `54fb5c8f` aborted at pylint with C0302 on
`benchmarks/summarize_line_event_profile.py`: 406 lines against the 400-line
module cap. The module crossed the cap in `01ec41bd`, whose eight-line comment
on the threshold constant took it from 399 to 406.

Two independent masking effects kept it invisible, and they are worth naming
separately because only the first is the abort trap already recorded above.

**The abort masked it after `01ec41bd`.** Every lint run since that commit
stopped earlier in the same target — one at `ruff check` (the DOC502 fix below)
and the one before it at `interrogate`. pylint is the third recipe line, so
C0302 was never reached in any of them, and the branch reported "nine of ten
sub-checks green" while a real defect sat in the third.

**The commit's own claim masked it at `01ec41bd`.** That commit's message lists
the gates it ran: `make fmt`, `check-fmt`, `markdownlint`, `spelling`, `nixie`,
`typecheck`. `make lint` is absent — and it is the only gate that runs pylint,
so the one gate that could have caught a module-ceiling violation is precisely
the one not run. Recording a gate list in a commit message is not evidence that
the list is complete; a list that omits the gate covering the defect reads
afterwards exactly like one that passed it.

The fix is a seam extraction rather than a comment trim. Everything from
`_caller_depth` through `summarize` is a pure function of two already-parsed
inputs and touches no file, no text, and no argv, so it moved to
`benchmarks/_line_event_profile_classifier.py` along with the threshold
constant; the front end keeps `main`, `_parse_args`, and `_exit_status` and
re-exports both halves, leaving `__all__` and every import path unchanged.
Result: 132 / 311 / 363 lines against the cap.

**The split is proven behaviour-preserving, not argued.** The same real control
capture classified through HEAD's `benchmarks/` in a scratch copy and through
the split tree produces byte-identical JSON: `matched_frames` 10704,
`consume_samples` 30822, share 34.7284%, and the same exit 1. The re-export
contract test also pins that each exported name is the owning module's own
object, so a redefinition cannot creep into the front end.

Two stale numbers found in the same pass: `01ec41bd` raised
`CONSTRUCTION_SHARE_LIMIT_PERCENT` to 28.0 but left the module docstring and
the exit-status table still saying "10%" in two places. Both now refer to the
limit instead of naming a number, so the prose cannot drift from the constant
again.

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
`_line_callbacks.py` can move. Hoisting therefore leaves **every one of the 6474
`ExecEvent.__init__` samples in the numerator**. Only the `_EventDetails` rule
is actually addressable by this design.

**The arithmetic.** D = 30822, and the 10% threshold is `100·N/D ≤ 10` ⇒
`N ≤ 3082`. Removing the entire `_EventDetails` rule leaves

```text
 N = 6474   share = 21.0045%   (one ExecEvent.__init__ per delivered line)
```

against a budget of 3082. That is 2.1x over, with 100% of executable
construction removed and none of the retained kind. No local refinement within
this design can close it, because the plan explicitly *retains* the ordinary
per-line `ExecEvent` constructor and forbids the alternatives that would remove
it.

**The share is a ratio of times, and removing work pushes it the wrong way.**
This is the part that is easy to get backwards, and an earlier draft of this
entry did. The 10% is a *share of consume-subtree time*, so making the retained
constructor faster does not shrink the numerator alone — it also shrinks the
denominator, because the samples it frees stop being charged to the subtree.
The numerator stays pinned at one construction per delivered line, so the ratio
improves more slowly than the raw speedup suggests.

Modelling this exactly on the capture-derived fractions
(`f_ev = 6474/30822 = 0.210045`, `f_ed = 4230/30822 = 0.137240`), with `r` the
fraction of constructor cost the retained per-line construction keeps:

```text
 share(r) = f_ev·r / (1 − f_ed − f_ev·(1 − r))
```

| retained cost `r`  | share after hoisting |
| ------------------ | -------------------- |
| 1.00 (hoist alone) | 24.35%               |
| 0.719              | 18.79%               |
| 0.500              | 13.86%               |
| 0.431              | 12.18%               |
| **0.3453**         | **10.00%** (the bar) |
| 0.206              | 6.22%                |

Solving gives a closed form:

```text
 r* = 0.10·(1 − f_ed − f_ev) / (f_ev·(1 − 0.10)) = 0.3453
```

So the retained per-line construction must keep **at most 34.5%** of today's
cost — it has to be **at least 2.90x faster**. Hoisting alone *raises* the
share from 21.00% to 24.35%, because `_EventDetails` was 13.7% of the subtree
and removing it shrinks the denominator faster than the numerator. The plan's
framing of this as "reduce the numerator to ≤10% of a fixed D" is therefore
wrong in direction, and that correction matters for whoever revises the design:
every option must be judged on the ratio, not on raw construction time.

**Why the refinements are both already ruled out.** The Tolerances section
allows "argument-passing and local-binding variants of ordinary `ExecEvent`
construction". Both were priced:

- *Argument passing.* `ExecEvent(**kw)` measured 2735 ns against 2431 ns for
  positional — 11% better, nowhere near the 2.1x needed.
- *Local binding.* The 16 optional fields are what cost, and they are passed
  explicitly by `emit`; hoisting the values cannot avoid re-passing them.

**The real cost driver — measured, and not what the plan assumed.** The Risks
section predicted failure "because a fresh `ExecEvent` still initializes 23
slots". Both available readings of that are wrong, so the distinction is worth
drawing precisely.

*Slots are not the cost.* A 2-field `frozen+slots` dataclass constructs in 239
ns; the same shape without `frozen` takes 89 ns, and a handwritten `__slots__`
class 90 ns. Slot *allocation* is the cheap part.

*Defaults are not the cost either* — and this is the trap. An earlier draft of
this entry claimed cost "tracks the count of defaulted fields, at roughly 70 ns
each". That was an artefact of the measurement: the field-count variants were
built with `exec`-generated source, which does not use the same construction
path as the generated `__init__` that real dataclasses get, so the comparison
confounded field count with constructor mechanism. Rebuilding the same variants
with `dataclasses.make_dataclass`, which generates the constructor the way a
real class does:

| fields | defaulted | ns   |
| ------ | --------- | ---- |
| 27     | 0         | 2968 |
| 27     | 16        | 3035 |
| 16     | 0         | 1835 |
| 16     | 15        | 1875 |
| 12     | 0         | 1406 |
| 11     | 0         | 1305 |

Defaults are worth under 3% (2968 → 3035 ns with 16 added). Cost tracks **field
count**, at roughly 110 ns per field, because the generated `__init__` performs
one `object.__setattr__` per field when `frozen=True`. `ExecEvent` has 27.

*Two further levers, both measured and both insufficient.* A handwritten
`__init__` on the same 27 fields — `init=False` plus explicit
`object.__setattr__` calls, which leaves the dataclass's fields, `repr`,
equality, and public shape unchanged — runs at 2287 ns against 3367 ns
generated, a **0.68x** ratio. Consolidating the 16 optionals into one nested
record time-shares at **0.52–0.65x** depending on noise.

**Both are judged on the `share(r)` algebra, and an earlier draft of this entry
judged them on the naive one.** That draft applied `r* = 0.3453` to both, but
`r*` is derived from `share(r)`; comparing `r` against it is only valid if the
share is `r·f_ev`, which is precisely the model this section exists to correct.
Re-derived on the same formula as the table above:

| lever                  | `r`  | correct `share(r)` | naive `r·f_ev` |
| ---------------------- | ---- | ------------------ | -------------- |
| handwritten `__init__` | 0.68 | **17.95%**         | 14.28%         |
| nested record (low)    | 0.52 | **14.33%**         | 10.92%         |
| nested record (high)   | 0.65 | **17.30%**         | 13.65%         |

The verdict is unchanged but the margin is not: the earlier 14.27% and
10.2–13.6% understated the true share by 3.5–4.6 points, and the naive column's
"10.92%" sits close enough to the bar to invite a false near-miss reading. On
the correct figures the 0.52x nested record is 4.3 points clear of the bar, not
0.9. Either way neither clears it, for the reasons already given: the
handwritten constructor is ruled out anyway, since the plan forbids slot
mutation, and field consolidation changes the public event representation,
which the Tolerances section marks as a stop-for-approval change.

Every benchmark above is comparative, single-process, on a host that was
running the gate suite concurrently: run-to-run spread was large enough to move
`t_gen` between 3.2 µs and 9.4 µs for the same class, and the field-count curve
went non-monotonic under load. The *ratios* are the stable signal and the
ordering is consistent, but these figures are not gate evidence and a revision
should re-measure them on an idle host before committing to a design. The
algebra in the previous section uses only capture-derived sample fractions and
is unaffected by that noise.

**What this means for the milestone.** R3 cannot be discharged by the planned
hoist, and EP-M2 must not be implemented as written. Per the plan's own
instruction, this is recorded as BLOCKED with measurements and options rather
than worked around by weakening acceptance — 21.00% is not 10%. Consolidated
options for the required design revision:

Nothing here reaches `r* = 0.3453` on its own by a measured margin, so a
revision likely needs to combine a construction-cost lever with a re-scoping of
what the ratio charges to the subtree.

1. **Consolidate the fields, possibly combined with a cheaper constructor.**
   Collapse `ExecEvent`'s 16 optionals into one nested record so a per-line
   construction sets 11–12 fields instead of 27. Field count is the driver
   (~110 ns each), so this is the largest lever: a 12-field shape time-shares
   at 0.484x and 11 fields at 0.451x.

   **Corrected projection.** Applying the `share(r)` algebra above rather than
   the naive `r·f_ev`, those ratios project **13.48%** (12 fields) and
   **12.67%** (11 fields), not the 10.17% and 9.47% an earlier draft of this
   entry recorded — that draft multiplied the time ratio by `f_ev` alone and so
   omitted the shrinking denominator, the same error the section above was
   written to correct. The consequence is decisive for this option: at 0.451
   the measured 11-field shape sits **above** the `r* = 0.3453` bar, so it does
   not reach 10% even before noise, and field count alone is the only lever
   this option has.

   It also changes the public event representation, which the Tolerances
   section marks as a stop-for-approval change, and fails R2's payload-parity
   reading unless parity is redefined to the flattened view. Treat as
   **insufficient on its own**, not merely likely-insufficient.
2. **Re-scope the measurement.** Split the gate's input so the *lifecycle*
   events and the per-line events are reported separately, and hold only the
   per-line share to 10%. This is arguably what the roadmap meant — the
   numerator was never meant to include lifecycle construction — but it is a
   change to acceptance, so it needs approval, not assertion.

   **Measured correction: for this scenario the split is a no-op.** The premise
   is false against the committed capture. Re-deriving the split through the
   gate's own classifier: every one of the 6474 `ExecEvent.__init__` samples
   resolves to the *nearest* caller `emit` at `cuprum/_pipeline_types.py:147` —
   the `line=details.line` binding inside `emit`'s single `ExecEvent(...)` call
   — and the ExecEvent rule carries **zero** caller weight outside the consume
   subtree (`1646` non-consume samples against `32468` parent samples, none of
   them on this rule's callers). No `plan`, `start`, or `exit` sample appears
   under the callback, because the callback workload never emits a lifecycle
   event. So the per-line and lifecycle populations are the *same set* here:
   splitting them reports 21.00% under one heading and 0% under the other, and
   V5's stated inputs (D as the `_consume_stream_with_lines` subtree, N and D
   as percentages over all parent samples) stay numerically identical either
   way. The split would matter only for a workload that emits lifecycle events,
   and the committed capture is a callback-only scenario.

3. **Change the callback's shape.** Drop the per-line event emission in favour
   of one event per run carrying a line sequence. This removes the per-line
   construction outright, at the cost of the observation contract V2 and V4
   currently pin, so it is a design change well beyond 5.2.1's scope.
4. **Accept the floor and close 5.2.1 as partial.** Record 21.00% as the
   measured floor, mark R3 unmet, and leave the roadmap item unchecked so the
   remaining work is visible rather than absorbed.

Options 1 and 2 are the only ones that keep the current observation contract.
Option 3 is the only recorded route measured to clear the bar with margin — it
removes the per-line construction rather than shrinking it — and it is also the
one no local refinement can approximate.

**Three further observations that bear on the design, recorded here because
they were found late and none is derived from the numbers above.**

*First: the roadmap's success criterion is stated against a baseline that
already bundles both constructors.* Item 5.2.1 asks for "per-line emission no
longer reconstructs invariant fields", and the 39% figure it names as the
starting point counts `ExecEvent` and `_EventDetails` **together**. A criterion
that says "no longer reconstructs invariant fields" cannot be met by a change
that leaves `ExecEvent`'s 27-field construction in the per-line path — which is
exactly what the nearest-caller measurement shows must happen. So the roadmap's
own wording, independent of any measurement, undercuts option 2's rationale:
for a callback-only scenario there is no lifecycle construction to set aside,
and the invariant fields the criterion names are the ones being rebuilt.
Whichever option is approved, the roadmap text needs revisiting rather than
treating as a fixed spec.

**Its baseline is unusable too, not just its threshold.** The 39% has a single
source — a table row in `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5,
whose scope is a narrower fixture — and the document never states the
denominator behind it. This branch's committed control on the wrap-76 fixture
reads **34.73%** of the consume subtree and **32.97%** of all parent samples;
the small-fixture smoke reads **47.86%**. Neither of the two percentages a
reader might reasonably have meant is 39%. So the criterion cannot be evaluated
as literally written in *either* direction: no revision of this design can
demonstrate a fall "from the 39% baseline", because there is no such baseline
for this scenario to fall from. That makes the design question a threshold
revision rather than a fix, and it should be settled explicitly instead of left
to whoever reads the roadmap next. V5's 10% bar is unaffected — its denominator
and fixture are defined in this plan, so it is well-formed on its own terms.

*Second: the hoist's natural shape is a specialized emitter, not a cached
prefix.* Only `pid` and `line` vary per line — `_event_details(*, pid, line)`
passes exactly those two — yet `emit` reassigns all 27 `ExecEvent` fields on
every line, of which 25 are stage-invariant or pinned to `None` for line
phases. The reframing matters because option 1 reasons about *declared* fields
(required versus optional), when the cost driver is *assigned* fields: a
dataclass with a required `line` field still constructs it per line. Realizing
the hoist therefore means preparing a specialized emitter at
callback-composition time, which changes **which caller `_matching_rule`
resolves to** and requires the classifier's rules to be re-baselined before V5
can be read. That re-baselining is a prerequisite for measuring option 1, not a
consequence of having measured it, and it should be in the approved work rather
than discovered during it.

*Third: the paragraph above "Two further levers" was judged with the wrong
formula, and the correction widens the margin without changing the verdict.* It
applied the bar `r* = 0.3453` directly to each lever's time ratio and reported
"projecting 14.27%" and "10.2–13.6%". But `r*` is derived *from* `share(r)`, so
`r` may only be compared against it when the share is `r·f_ev` — the naive
model that the "share is a ratio of times" section exists to refute. Re-derived
on `share(r)`, the same levers read **17.95%** (0.68x), **14.33%** (0.52x), and
**17.30%** (0.65x): every figure was understated by 3.5–4.6 points, and the
naive column's 10.92% sits close enough to the bar to read as a near miss when
the true figure is 14.33%. This is the third time in this milestone that the
naive formula has been used in place of the correct one — see the corrected
option-1 projection — which is worth recording as a pattern rather than three
separate slips: the naive form is the intuitive one, and it is wrong here.

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
gate's own code (`_line_event_profile_model.parse_capture` / `load_rules`,
`summarize_line_event_profile.classify_capture`), not a re-implementation, and
reproduced the committed artefact exactly — `parent_samples 32468`,
`consume_samples 30822`, `construction_samples 10704`, `share 34.7284%`,
matching `dist/profiles/5-2-1-event-details/control-1/` field for field. The
microbenchmarks are `timeit` in a single process on this host and are
comparative only; they are not capture-derived and are not offered as gate
evidence.

### 2026-09-27: `actionlint` is intermittent, not deterministic — and why the bounded re-run is valid evidence

The gate run at `dbaba9ac` ended with nine of `make lint`'s ten sub-checks
green and `actionlint` hung. This plan previously called that hang
"deterministic: fails 3/3 attempts". **That characterization is false**, and
the earlier run at `902b05fb` already disproves it: that run's log ends on the
bare `actionlint -config-file .github/actionlint.yaml` command line with
nothing after it, and the whole `make lint` chain exited 0. `actionlint` is the
last command of `github-actions-lint`, the last prerequisite of `lint`, and it
prints nothing on success, so exit 0 means it ran and passed. Same host, same
`/usr/local/bin/shellcheck` on `PATH`, opposite outcome. Treat the hang as
**intermittent** and bound every invocation with `timeout` rather than assuming
either result.

**The hang, measured.** Two independent observers — the scrutineer and the
planning agent — characterized the same process, and the findings were
re-confirmed against a second instance (PID 668124, 41 min elapsed) later the
same session. Both samples agree: **zero CPU consumed** — 198 s and 41 min
respectively, against 14 *centiseconds* of CPU in the second case — 34 threads,
every one parked in `futex_wait_queue` except a single `ep_poll`, and
`voluntary_ctxt_switches` in the low hundreds. It wakes occasionally and goes
straight back to sleep. An anonymous pipe pair (fds 93/94) is held by that
process and, by a full `/proc` sweep, by **no other process on the host**, and
no `shellcheck` child is ever spawned despite shellcheck being installed. Zero
CPU rules out "slow but working"; the self-held pipe with no reader rules out
"waiting on a busy child"; no `shellcheck` process rules out "the linter is the
bottleneck".

**The hang recurs, and it is not this branch's fault.** A single census of every
`actionlint` alive on this host at one moment found three, in three separate
trees, under three different parent commands:

| PID     | parent                          | tree                                 | elapsed | CPU                |
| ------- | ------------------------------- | ------------------------------------ | ------- | ------------------ |
| 94258   | `make check-fmt lint typecheck` | deleted worktree `30bb8047`          | ~29 h   | 1 s                |
| 668124  | `make lint`                     | **this branch's worktree**           | 41 m    | 14 cticks (0.14 s) |
| 1501431 | `timeout 900 make lint`         | another session, worktree `74fbe974` | ~5 m    | 0 s                |

Three trees, three sessions, three different parent commands, one symptom, and
— verified by reading `/proc/<pid>/cwd` — no common checkout between them
except that two share this branch. Four distinct hangs have been observed
across this work in total. PID 94258's worktree **no longer exists on disk**:
it still holds a deleted cwd and has burned 1 s of CPU in 29 hours. That is the
decisive argument that the cause is the host and not any branch, because no
defect in this branch's source could hang a process inside a deleted directory
belonging to a session that ended a day earlier. CI is unaffected: the
`902b05fb` run completed its whole chain in 66 s, and every GitHub Actions run
of this PR has completed `actionlint` normally.

**The direct cause is a broken pipe pair, and it is structural.** The pooled
`/proc/668124/fd` census shows fd 93 (read) and fd 94 (write) as two ends of
the *same* anonymous pipe, `pipe:[2027191927]`, with **no file descriptor to it
anywhere else on the host**. A process writing to a pipe it holds the write end
of, whose read end it also holds and never reads, blocks forever: `SIGPIPE` is
only raised when *no* descriptor references a read end, and the kernel's write
end is full. So the write never returns, no error is ever raised, and the
process is not exiting *late* — it is not exiting at all. This is why no
`timeout` value is a fix and why no poll can observe a resolution: only
external constraint terminates it.

**Two of this branch's own `make lint` runs hung, and neither was bounded.**
This corrects an earlier reading of the logs in this plan. The `521b` run was
recorded here as having been *killed* at the `skylos` command line, on the
strength of its log stopping there. Its tail, compared line for line against a
later run, ends on the same `actionlint -config-file .github/actionlint.yaml`
line as every other: **31 lines versus 29, the difference being two `uv`
bootstrap lines, not a `skylos` truncation.** It hung in `actionlint` like the
rest. And both hung wrappers are *still alive* as this is written — the `521b`
one at 43 minutes, the `521c` one at 41 — because neither armed a `timeout`
around `make lint`; their command lines carry `make lint 2>&1 | tee …` with no
bound at all. Their `.exit` files were never written, which is what an
unfinished shell looks like, not a killed one: a killed wrapper would have
proceeded to its `printf`.

So neither run may be cited as a completed gate run, bounded or otherwise. What
they support is the narrower claim that the sub-check hangs, which the process
table establishes directly. The generalizable lesson is the one now under **How
to apply**: **the log cannot tell you a run was killed.** A truncated log and a
hung-forever log are byte-identical in shape, and only an out-of-band exit
status distinguishes them. The `902b05fb` run — the one that *is* citable — is
the counter-example that makes the distinction concrete: same tail, but a
`.status` file recording `exit_code: 0`, `duration_seconds: 66`, and
`head_equal: YES`.

**The narrowed path is not a workaround; it is CI's own path.** The gate's
recipe is `actionlint -config-file .github/actionlint.yaml`. With shellcheck on
`PATH` — this host has `/usr/local/bin/shellcheck` — actionlint enables its
shellcheck-backed rules and spawns a child. CI's runners have no shellcheck, so
the path that actually gates upstream never takes that branch. Running

```sh
timeout 120 actionlint -shellcheck= -config-file .github/actionlint.yaml
```

exits **0 in under a second with empty output**, and every re-run recorded on
this host used that form.

**The justification is stronger than "this branch does not touch `.github/`",
and it should be stated that way.** The `.github` subtree SHA is
`7fb57438b7b7b953f5e5898004614c006f0614ba` at **every** revision examined —
`66f11b9e`, `902b05fb`, `1bfefbb8`, `3251f1e1`, `dbaba9ac`, `8dd2ec0b`,
`05267b9c`, `6f82a697` — *and at `origin/main`* (`7f762870`). The input is
byte-identical across the branch's whole life, so no actionlint behaviour can
be a function of revision content here. That is what makes the narrowed check
non-substitutive: the shell-syntax coverage it forgoes cannot mask a
branch-introduced defect, because there is no branch-introduced `.github/`
change for it to hide. `actionlint` still validates `${{ }}` expressions under
`-shellcheck=`, and it parses `.github/actions` as workflows, which makes
`yamllint` the only tool covering the composite actions — none of which this
branch modifies.

**Two revision-attribution errors in this plan's earlier text, corrected
here.** This paragraph previously cited "`dbaba9ac`'s change set" while
describing the run at `902b05fb`; those are different revisions with different
trees (`7b14eb39` and `5b87bf7f`). The path list is the range's
(`8dd2ec0b..HEAD`), and it is stated as such above. Separately, **`902b05fb` is
not an ancestor of `HEAD`**: it was amended away into `1bfefbb8`
(`git branch --contains 902b05fb` is empty; only the reflog reaches it). It is
a discarded draft *of this branch*, not a revision of its current history, and a
`git gc` would take it along with the log that corroborates it. The
intermittency conclusion does not rest on it — see the controlled A/B on a
frozen tree recorded below.

**A clean `-shellcheck=` exit is not vacuous.** An empty exit-0 result could
mean the checker read nothing, so the binary was probed with a deliberately
broken workflow on stdin: it reported `undefined function "nonexistent_fn"` and
exited 1. It inspects input, and the clean exit over the ten workflow files is
a real pass. Re-verified at 05:06 on 2026-09-27, with the error text reproduced
verbatim and the exit still 1 — the probe reads both a named file and `-` on
stdin, so this is not an artefact of the input form.

**The controlled A/B: the same command, the same tree, opposite outcomes, four
minutes apart.** This is the settled form of the argument and it needs no
orphaned revision. While gating `6f82a697` on tree `75b18498`:

| when     | what                                      | outcome                                         |
| -------- | ----------------------------------------- | ----------------------------------------------- |
| 04:43:31 | `timeout 420 make lint`, unbounded recipe | **exit 0**, 52 s, reached actionlint            |
| ~04:45   | `timeout 90 actionlint -config-file …`    | **exit 124**, killed at 90 s, 0 bytes of output |

Same host, same revision, same tree hash, same actionlint binary, same
`.github` content (`7fb57438b7b7b953f5e5898004614c006f0614ba`), four minutes
apart. A pass and a hang from one command on one input cannot be a property of
the input. This supersedes every earlier argument in this section: the earlier
ones were circumstantial, and this one is a direct measurement.

**The intermittency therefore is not "the tests were lucky once".** It is a
property of the host that the *same* invocation can go either way, which is
also why the earlier `902b05fb`-based reasoning was right for the wrong reason.

**The unbounded recipe passed on this host too, in the same session.** Run
immediately after the census, bounded at 90 s and against the clean tree the
gate had already read (`git write-tree` = `7283589b…`, the gate's own tree):

```text
timeout 90 actionlint -config-file .github/actionlint.yaml                EXIT=0  0 s
timeout 90 actionlint -shellcheck= -config-file .github/actionlint.yaml   EXIT=0  0 s
```

That is the terminus of the intermittency case: the command that hung for 41
minutes and the command that returned in 0 s are the same command on the same
revision. It also exposes how the earlier logs misled — the unbounded path
*does* succeed silently when it succeeds, so "the log's last line is the
`actionlint` command" is what **both** outcomes look like from inside `tee`,
and the log alone cannot separate them. Only a captured exit status can.

**How to apply:** never let `make lint` run unbounded. Expect one of two
outcomes and re-run only the `actionlint` sub-check, bounded, on failure.
`make lint`'s ten other sub-checks are independent and need no re-run; the
per-sub-check breakdown is recoverable from the log because each writes its own
evidence line before the hang. **Two operational rules, both learned the hard
way.** First, arm the bound on the *wrapper*, not inside it:
`timeout 300 make lint 2>&1 | tee "$log"` bounds the whole pipeline, whereas a
bound that lives only in a sub-make leaves the parent alive after the child
dies. Second — and this is the one that bit twice — **capture the exit status
out of band**, as `${PIPESTATUS[0]}` written to a status file beside the log. A
run piped through `tee` and read afterwards cannot be distinguished from one
that hung until killed: the log ends on `actionlint`'s invocation line either
way, because actionlint prints nothing on success. The failure mode is exactly
the tempting one — reading the tail, seeing `actionlint`, and recording a pass
that was never observed.

**What the gate's *other* sub-checks are worth without it.** `make` echoes each
recipe line before running it, so a sub-check's line appearing in the log is
proof that *every* preceding line in the chain exited 0. `actionlint`'s line
appearing is therefore evidence for all ten lines above it. Only seven of those
ten then print a verdict string of their own: ruff's `All checks passed!`,
interrogate's `RESULT: PASSED (minimum: 100.0%, actual: 100.0%)`, two
`Your code has been rated at 10.00/10` lines (pylint and the `df12` lints), the
`Finished` lines for clippy and whitaker, and typos-config-builder's
`current: typos.toml`. **Three print nothing on success.** Two are `ambrleaks`
and `skylos`; the third is `yamllint`, whose silence is nevertheless *provable*
rather than merely assumed, because `actionlint`'s line follows it in the same
recipe and `yamllint` is silent on success too — silence is its passing output,
so there is no missing artefact there. For the other two the chain argument is
the only evidence, and it is worth naming that rather than counting the
invocation line as a clean result. It is still evidence: a non-zero exit from
either would have stopped the chain before `actionlint` was reached, and
`actionlint` was demonstrably reached.

**Evidence-integrity note.** Two logs from this run (`check-fmt`,
`markdownlint`) were discarded as contaminated: the planning agent edited a
tracked file mid-run, and the run's own column-2 `MM` tripwire in
`git status --porcelain` caught the same event independently. Both gates were
re-run on the restored tree and only the `-rerun` logs count. The gate suite
verifies the revision, so a writer editing during it invalidates the affected
logs and nothing else — but the affected set is *every* gate whose read window
touched the edit, not just the one whose log looks wrong.

### 2026-09-27: EP-M2 lands, and three of its own claims were falsified first

The hoist is implemented: `_line_event_emitter(observation, context)` returns a
prepared `_LineEventEmitter` that resolves `program`, `argv`, and `project`
once per observed stream, and `_compose_line_callbacks` binds it there instead
of rebuilding an `_EventDetails` per line. The deleted `_event_details` helper
and its deferred import are gone. Both of V1's assertions now pass as ordinary
tests, and the strict markers were removed as designed.

Four findings are worth more than the diff:

*The ExecEvent rule had to be **merged**, not split, and the reason is
counter-intuitive.* Every generated constructor renders as
`__init__ (<string>)`, so rules over that frame are separated only by their
callers and resolved by proximity. A two-rule split therefore does not
partition the samples: the nearer-caller rule wins every frame both could
claim, and the loser keeps caller weight with zero matches — which
`_drifted_rules` reports, exiting 2. This was **measured, not reasoned**: a
two-rule split over a post-hoist-shaped stack gave the nearer rule all 100
samples and reported the other as drifted. The pre-hoist split only ever worked
because `_event_details` was genuinely nearest for its own 4230 frames. My
first two attempts to confirm the hazard failed to reproduce it — the first
used disjoint caller sets, the second used a frame that still existed in the
control capture — and the mechanism I had already written into the rules file
was wrong. It is now corrected there, with the measurement recorded.

*The interface was kept and the location moved, so the rules' caller entries
survive.* The committed rules' caller entries `_LineEventEmitter` and
`_emit_line_event` named nothing that existed at the time the rules were
written; they were plan-era names for the approved post-hoist design, which had
not been built yet. They are dropped as *capture* caller entries regardless,
because a py-spy frame is rendered with a bare function name and a file
location: `emit_line`, never `_LineEventEmitter.emit_line`. Whether the class
exists does not change the rendering, so a capture can never match a
class-qualified name.

*A third rules entry was also dead, and it was found by moving code rather than
by reading it.* The rules file listed `emit_line` at
`cuprum/_pipeline_types.py` as "the hoisted emitter, which is where the
per-line construction now happens". Relocating the emitter to
`_line_callbacks.py` exposed that this was never true: the hoist landed as a
`_LineEventEmitter` method there, and `_pipeline_types.py` contains no
`emit_line` at all after the revert. Measured against the control capture: zero
frames carry `_pipeline_types.py` in an `emit_line` frame, 111 carry
`_line_callbacks.py`. Deleting the entry and re-running the classifier left
every aggregate of the control result identical — 10704, 30822, 34.7284%,
`fail_above_limit` — so it was inert on the one capture the gate has, not
merely unreachable in theory. The lesson worth keeping: a rules entry that
names nothing can survive indefinitely, because the classifier treats an
unmatched caller as *absent* rather than as drift, and absence is silent. Only
the caller that still runs can drift, and drift is what exits 2. The control is
the pre-hoist tree, so the surviving `emit_line (_line_callbacks.py)` entry
covers both the hoisted construction (inside that method) and the un-hoisted
one (the composed closure in the same file), which is what makes the entry
durable across a revert.

*The hoist's first landing broke a gate, and the plan's own relocation decision
was the thing that fixed it.* The first version followed the approved shape and
added `_StageObservation.line_emitter()` plus a `_LineEmitter` class to
`cuprum/_pipeline_types.py`. That took the module from 299 to 415 lines against
`max-module-lines = 400`, so `pylint` raised C0302 and `make lint` failed. The
plan had already recorded the fix: put the emitter in
`cuprum/_line_callbacks.py` and keep `_pipeline_types.py` untouched. The landed
revision does exactly that, so `_pipeline_types.py` is byte-identical to its
pre-hoist state and the hoist is now a single-module diff. `_line_callbacks.py`
grew to 285 lines, still well inside the cap.

Two details of the landed shape are worth recording, since both are easy to get
wrong on a later edit:

- `_LineEventEmitter.project` and `.exec_id` are passed *explicitly* even though
  `ExecEvent` declares both with a `None` default. The generic oracle passes
  its observation's real values, so omitting them here would have been a silent
  payload divergence that the field-for-field parity test catches — this is why
  that test compares every declared field rather than a chosen subset.
- The emitter holds the observation's own bound `_emit_event` rather than
  re-implementing its body. `_emit_event` catches `_ExecEventEmissionError`,
  retains the observation's pending-task list, and preserves the tasks of hooks
  that already ran when a later one failed. The private attribute access is
  deliberate and confined to the factory, which is the one place that already
  holds the observation. This is a deliberate, recorded exception to the
  no-reach-past-the-seam rule, taken to keep task ownership in one place.

*The V1 recorder had gone blind, and the zero it reported would have been
vacuous.* `_record_event_details` patched the module attribute
`_pipeline_types._EventDetails`, which only intercepts callers that resolve the
name at call time. The deleted `_event_details` did exactly that — which is
*why* the test was ever red. Every surviving production site binds the name
eagerly, so after the hoist the spy could not see anything, and `per_line == 0`
was true for a reason that had nothing to do with the hoist. The spy now patches
`_EventDetails.__init__`, which is binding-independent, and every
zero-asserting case calls `_prove_recorder_is_live` first. Falsified by
sabotage: with a deliberately blinded recorder, 5 tests fail on the liveness
assertion instead of passing on a false zero.

*The payload is unchanged.* Driving the hoisted emitter and `emit()` side by
side yields events differing only in `line` and `timestamp`, which is the whole
of the observation contract. My first attempt to check this reported 25
differing fields; that comparison was itself broken (`dc.fields` over a dict),
and the re-run with an explicit per-field dump is the one that counts.

### 2026-09-27: V5's threshold is revised to 28%, and the projection is a range

**Decision (user-approved 2026-09-27, `AskUserQuestion`).** The user chose
"revise the threshold, then implement". V5's construction-share bar moves from
**10% to 28%**, measured on the consume subtree exactly as V5 already defines
D, N, and the classifier. The `39% baseline` comparison is retired with it: as
recorded above, that figure has no denominator in its source document and no
percentage this capture produces matches it, so the criterion could not be
evaluated as literally written in either direction.

**Why 28%, derived rather than chosen.** EP-M2's scope is already fixed by the
two red tests in `cuprum/unittests/test_line_event_emission_prep_cost.py`: the
hoist must deliver **zero `_EventDetails` constructions and zero argv rebuilds
per line**, while the per-line `ExecEvent` construction is retained (it is what
V2/V4's distinct-object and payload-preservation contract requires). Priced on
the committed control capture, that removes the `_EventDetails` constructor
(4230 samples) plus the `_event_details` helper frame that only exists to build
it (2182), and the two `argv_with_program` frames hoisted along with it (353).

**The projection is a range, not a point, and that is the finding that
corrected this plan's own first proposal.** The share is `N/D`, and the hoist
shrinks *both*: removing construction also removes the samples it cost, so
every sample that leaves N also leaves D. Which frames a given implementation
actually removes is not knowable before it is written, so:

| assumption about what leaves D      | post-hoist share |
| ----------------------------------- | ---------------- |
| conservative — only the ctor leaves | **24.35%**       |
| aggressive — ctor, helper, argv too | **26.91%**       |

An earlier draft of this entry proposed a bar of **25%**, computed from the
*control* denominator (30822). That is an unreachable bar: the aggressive
projection is 26.91%, so a correct implementation of the already-approved hoist
would fail it. The lesson generalizes and is recorded in the decision log: a
post-hoist projection must use the *post-hoist* denominator, not the control's,
for the same reason the naive `r·f_ev` form is wrong — both compare a numerator
against a denominator that has already moved.

**28% = the aggressive projection (26.91%) rounded up to 1.0 percentage point
of headroom.** It is cleared by the full intended hoist, is not cleared by a
partial one that leaves argv or the payload per-line, and is 6.7 points below
the control's 34.73% so it still requires real work. It is far weaker than the
roadmap's 10%, and that is deliberate and recorded here rather than glossed:
10% is unreachable without converting the per-line event to a per-run one,
which breaks the observation contract V2/V4 exist to protect. The roadmap's
success criterion should be amended to cite 28% and to drop the 39% baseline.

**What remains genuinely unmet.** The roadmap asks that "per-line emission no
longer reconstructs invariant fields". Under the approved threshold the
`ExecEvent` construction stays in the per-line path, so that clause is met only
for the `_EventDetails` half. This is the same partial satisfaction the "three
further observations" entry records; the threshold revision makes the
measurement honest, it does not make the clause true. That residual gap belongs
in the closeout report.

### 2026-09-27: V5 needs a second fixture, and the provenance of the control capture

**Two facts V5's collection depends on, neither of which was in the plan.**

**1. The no-callback controls need the unwrapped fixture, which did not exist.**
`benchmarks/tee_profile_scenarios.py` pairs `seed12345-nowrap.b64` with every
`with_line_callbacks=False` scenario and reserves `seed12345-wrap76.b64` for
`echo-devnull-cb-s1` alone. The wrapped fixture is line-oriented (76 columns,
so a newline every 76 bytes); feeding it to a no-callback control would measure
a differently-shaped workload from the suite's own definition of that scenario.
Only the wrapped fixture was present in `dist/`, so the echo/tee no-callback
controls V5 requires for its 5% wall-time tolerance had nothing to run against.

Generated 2026-09-27 with the documented command from `benchmarks/README.md`
(`--seed 12345 --raw-bytes 1610612736 --wrap 0`), outside the measurement
window, while another session's `make test` was running on the host: 2147483648
bytes, manifest `dist/fixtures/seed12345-nowrap.json`, SHA-256
`15e4356ae06fa10a81a3b4ba9e7b0e4437961a21752f982582371aa88389f914`. `dist/` is
gitignored, so the fixture is a local artefact and cannot be committed as
evidence; the manifest content is recorded here instead.

Verified rather than assumed: the two fixtures share a seed, so their decoded
content must agree. Decoding the first 1048575 bytes of each gives one SHA-256
(`9ab664d7…`) for both — byte-identical payload, wrap mode the only difference.
The structural check confirms the intent: the first 4 KiB contain 0 newlines
unwrapped against 53 wrapped. A manifest's self-reported `sha256` is over the
*encoded* file and would not have caught a wrong seed; this cross-check does.

**2. The committed control capture's provenance was narrower than assumed.**
`dist/profiles/5-2-1-event-details/control-1/worker-result.json` records
`scenario=echo-devnull-cb-s1-python, backend=python,
fixture=dist/fixtures/seed12345-wrap76.b64, with_line_callbacks=true`,
`wall_time_seconds=316.24`, `stdout_line_count=28256364`. The current tree's
scenario is named `echo-devnull-cb-s1` and declares `backend="auto"`, so the
control capture was taken through an explicit `--backend python` selection that
the canonical suite does not itself specify. V5 requires both variants to use
"the same interpreter, read size, fixture, backend, sink", so the collection
script pins `--backend python` on **both** sides. Resolving `auto`
independently on each side would have left backend selection free to differ
between control and candidate — a difference that would have been invisible in
the share and looked like a performance result.

**Also recorded: the recorded `control-1/construction-share.json` is not
reproducible by the current classifier, and should not be.** It reports
`limit_percent: 10.0`, two `ExecEvent.__init__ via _StageObservation.emit` /
`_EventDetails.__init__ for the per-line payload` rules, and
`construction_share_percent: 34.7284`. The rules have since been re-baselined
to the single merged rule and the limit to 28.0, so a fresh run necessarily
differs in `limit_percent`, rule names, and `matched_frames` keys. The fidelity
test that *is* meaningful — pre-split code vs post-split code on one capture —
was run separately and produced byte-identical JSON.

### 2026-09-27: the completed hoist measures 30.20% — above the revised 28% bar

**This is a matched full-fixture measurement of the implemented hoist**, not a
projection. It supersedes the 24.35%/26.91% projection table above, and it is
the second time this gate has been missed — the first was against 10%, before
any code was written.

Probe: one control/candidate pair, full wrap-76 fixture, `--backend python`,
`--stages 1 --mode echo --sink-kind devnull --line-callbacks --read-size 65536
--repeat-count 1`,
py-spy raw at 100 Hz. Control = `01ec41bd` (pre-hoist), candidate = `18083334`
(post-hoist). Both captured `stdout_line_count = 28256364` and `exit_code = 0`,
so the same workload ran on both sides.

|                             | control      | candidate    | delta                  |
| --------------------------- | ------------ | ------------ | ---------------------- |
| D (consume samples)         | 26469        | 21832        | −4637 (−17.5%)         |
| N (construction samples)    | 9106         | 6594         | −2512 (−27.6%)         |
| **share N/D**               | **34.4025%** | **30.2034%** | **−4.20 points**       |
| share of all parent samples | 33.6387%     | 28.0012%     | −5.64 points           |
| parent samples              | 27070        | 23549        | −3521                  |
| wall time                   | 271.32 s     | 228.11 s     | **−43.20 s (−15.92%)** |

**The hoist demonstrably works; the gate's metric does not register it.** The
candidate is 15.92% faster end to end on a 2 GiB workload, and the construction
work it removed fell 27.6% against a denominator that fell only 17.5%. But
because the share is `N/D` and the removed work leaves both, the ratio improves
by 4.20 points where a naive reading of a 15.92% speedup would suggest far
more. The candidate would need N ≤ 6113, i.e. **481 fewer samples out of
6594**, to clear 28%.

**Those 481 samples are not addressable by this design.** Every one of the 6594
matched samples is a leaf inside `ExecEvent.__init__` itself — the
decomposition finds no sub-frame work under the constructor to shave. So the
remaining numerator is not overhead around the retained construction; it *is*
the retained construction, which EP-M2 was explicitly scoped to keep and which
V2/V4's distinct-object and payload-preservation contract requires. Reaching
28% would mean constructing `ExecEvent` fewer times, which is the same
observation-contract break the plan already declined for 10%.

**Corroboration that this is not a rules artefact.** The classifier's own
attribution was re-derived independently from the raw capture — an
independently written matcher using the same nearest-caller rule reproduced
D=21832, N=6594, 30.2034% exactly on the candidate capture. `unresolved_frames`
is `{}` on both sides, so no rule drifted. The echo-truncation limiter's own
generated constructors (`finish_line` under `_echo_truncation.py`, seen at
weights 247/256/62 in the immediate-caller analysis) are correctly *excluded*,
because no `emit`/`emit_line` caller sits below them — that is the caller
requirement doing its job, not an under-count.

**Two caveats on this probe, stated because they bound its authority.** It is
**one pair, not the three** V5 requires, and the two runs were taken ~6 minutes
apart at different host loads (control saw a transient spike to load 85.31 from
another session's `rustc`; candidate ran at load 6). V5's own controls — D ≥
10000 (met on both: 26469 and 21832), a candidate range ≤ 2 points across three
pairs, and five unprofiled rounds per scenario — are therefore **not** yet
satisfied. The wall-time figure especially is a single unpaired observation and
is not yet evidence under V5's 5%-median rule.

**Why the projection missed, in the terms it was written in.** Both projected
shares were computed as `6474 / D_posthoist`, so each was only as good as its
`D` estimate. Recovering the implied denominators: 24.35% implies D ≈ 26587 and
26.91% implies D ≈ 24058. The observed post-hoist D is **21832** — below both.
The projection underestimated how much the hoist would shrink the denominator,
and a smaller denominator with a roughly fixed numerator gives a larger share
than either figure. Two limits on how far this should be pressed: the observed
N (6594) is not the projected 6474 measured again, because the rule set was
merged in the same change — under the pre-hoist split the 6474 counted only the
frames resolving through `emit` in `_pipeline_types.py`, whereas the merged
rule also matches `emit_line` in `_line_callbacks.py`, which is where the hoist
put the construction. So "N rose by 120" is not a like-for-like claim and is
not made here; what is defensible is that N did **not** fall to the projected
value while D fell further than projected, and the ratio followed.

**What this does and does not settle.** It settles that the design as approved
and implemented lands around 30%, not under 28%. It does not settle the
three-pair dispersion question, so a BLOCKED decision should rest on a
completed V5 collection rather than on this probe alone.

### 2026-09-27: two liveness traps in the V5 collection script

Both were found by running the real script end-to-end on small fixtures before
committing 2.1 hours to the full collection. Neither is a defect in the hoist;
both are defects in the measurement script, and both would have produced
*plausible-looking* output.

**Trap 1 — `stdout_line_count` is zero for every no-callback scenario, by
construction.** `_run_command_sync` increments the line count only inside
`observe_line`, and that hook is installed only under
`if config.with_line_callbacks:`. So a `with_line_callbacks=False` run reports
`stdout_line_count: 0` however much data it processed. Reading that as "the
scenario did no work" would discard sound measurements; reading it as "the run
succeeded" would accept vacuous ones. V5's no-callback controls exist precisely
to bound the callback result, so a silent zero there would have made the
headline comparison unbacked. `captured_output_length` is the usable signal —
but only where the mode captures.

**Trap 2 — capture size is mode-dependent, so the obvious liveness check is a
false alarm on two of the three scenarios.** Per
`benchmarks/_tee_profile_worker_command.py::_capture_and_echo_flags`:
`echo => (False, True)`, `capture => (True, False)`, `tee => (True, True)`. The
callback scenario and `echo-nocb` both run `--mode echo`, so both capture
nothing *by design*; asserting a non-zero capture on them fires on correct
runs. The first version of the check did exactly that. The corrected script
asserts capture size only for `tee-nocb` and falls back to a wall-time floor
for the two echo-mode scenarios.

**A related non-finding, recorded so it is not re-investigated.** On the 26 MB
smoke fixture `echo-nocb` finishes in 0.044 s, which looks like a skipped
workload. On the full fixture it takes **40.86 s**. The difference is real and
expected: the unwrapped fixture contains no newlines, so with no line callbacks
and echo-to-devnull there is no per-line work at all — the stream is discarded
wholesale. The smoke fixture is too small for this scenario to be meaningful,
which is why V5 forbids it as evidence ("Small fixtures are smoke tests only").
The script's uses of it are limited to exercising the plumbing.

### 2026-09-27: V5 measured — 29.91%, and the 28% bar is not met

**Resolution: this entry stands as written — the captures did miss 28% — and
the bar was subsequently revised to 30% on these measurements.** Read the "not
met" verdict below as a statement about the 28% target specifically, not about
the implementation. The numbers here are unchanged by that revision; the
reclassification that confirms it is in "The 30% revision, and why the margin
is thin".

**Three matched pairs, collected per V5's own protocol.** Full wrap-76 fixture,
`--backend python --stages 1 --mode echo --sink-kind devnull --line-callbacks
--read-size 65536 --repeat-count 1`,
py-spy raw at 100 Hz, one unprofiled warm-up per variant before collection,
control/candidate order alternated per round. Control = `01ec41bd` (pre-hoist);
candidate = `f4d1010a`, whose `cuprum/` and `benchmarks/` trees are
byte-identical to the `18083334` probe tree (only plan text changed in between).

| pair       | control D | control share | candidate D | candidate share | candidate wall |
| ---------- | --------- | ------------- | ----------- | --------------- | -------------- |
| r1         | 25726     | 35.3533%      | 17539       | 29.8991%        | 180.16 s       |
| r2         | 25743     | 34.2928%      | 17758       | 29.9414%        | 181.18 s       |
| r3         | 29722     | 34.0388%      | 17413       | 29.9087%        | 175.41 s       |
| **median** |           | **34.2928%**  |             | **29.9087%**    | **180.16 s**   |

**Tolerances.** D ≥ 10000: **met** (min 17413). Candidate range ≤ 2 points:
**met, and by a wide margin** — 0.0423 points (29.8991 → 29.9414), where the
tolerance allows 2.0. `unresolved_frames` is empty in all six captures, so no
rule drifted. Candidate ≤ 28% in every capture: **NOT met** — the worst
candidate run is 29.8991%, **1.90 points above the bar**, and the *best* is
29.9087%, so no run comes close. Median wall time fell 30.96% (260.97 s →
180.16 s).

**Why this settles it rather than inviting another round.** The candidate's
dispersion is 0.0423 points across three pairs — roughly 1/47th of the allowed
2-point band. A measurement that tight cannot be brought under 28% by more
sampling: the interval is nowhere near the threshold. The control's spread is
larger (1.31 points) purely from host-load variation, and even the control's
*best* case (34.0388%) is 6 points clear of the bar. The result is a stable
property of the design, not an unstable observation that V5's "persistently
unstable results are inconclusive" escape hatch could cover.

**Interference was present and is recorded, but does not explain the gap.**
Other sessions ran full gates and `cargo` jobs throughout, so per-run load
varied from 2.77 to 35.41. That variability shows up where it should — in the
denominators and wall times (control D ranges 25726–29722, wall 259–306 s) —
and *none of it* moves the candidate share, which stays inside 0.05 points.
Load affects the total work measured, not the fraction of the consume subtree
spent constructing. A quieter host would not close a 1.9-point gap that is
stable to 0.04 points under load.

**Collection parameters and the raw capture record.** Raw folded captures are at
`/tmp/smoke521/v5/r{1,2,3}-{control,candidate}/`: control
`01ec41bd56b5968e7b9b5ec205ba82ffdbe55724`, candidate
`f4d1010aaf352a4dd6f549f6f602dd29bc329c4d`, both resolved per run and written to
`revision.txt`. Every capture has `unresolved_frames: {}` and
`status: fail_above_limit`, so no rule drifted and every failure is a genuine
over-limit share rather than an inconclusive result misread as one.

| capture      | py-spy samples | parent | D     | N     | share % | wall s | py-spy rc | load at start     |
| ------------ | -------------- | ------ | ----- | ----- | ------- | ------ | --------- | ----------------- |
| r1-control   | 26185          | 26184  | 25726 | 9095  | 35.3533 | 260.97 | 0         | 11.40 21.09 21.91 |
| r1-candidate | 17912          | 17911  | 17539 | 5244  | 29.8991 | 180.16 | 0         | 5.59 12.31 18.02  |
| r2-control   | 26224          | 26223  | 25743 | 8828  | 34.2928 | 259.22 | 1         | 5.70 7.88 14.14   |
| r2-candidate | 18145          | 18141  | 17758 | 5317  | 29.9414 | 181.18 | 0         | 5.59 12.31 18.02  |
| r3-control   | 31311          | 31310  | 29722 | 10117 | 34.0388 | 306.38 | 0         | 4.11 5.72 11.70   |
| r3-candidate | 17740          | 17737  | 17413 | 5208  | 29.9087 | 175.41 | 1         | 8.25 16.96 15.89  |

**Two captures carry a non-zero py-spy exit, and both are benign — checked, not
assumed.** r2-control and r3-candidate report `py-spy=1` with the log ending
`Wrote raw flamegraph data to '…/stacks.folded'. Samples: 26224 Errors: 0`
followed by `Error: No child process (os error 10)`. The capture is complete
(the sample count in the log equals the folded file's weighting, and the
classifier read it without an `unresolved_frames` entry); the error is py-spy
losing its target in teardown *after* the worker exited. Treating the exit code
alone as "capture failed" would have discarded two of the six valid captures,
including one of the three controls. It is recorded here rather than silently
tolerated so a future reader of `pyspy-exit.txt` does not re-open it.

**Liveness is established on the work done, not on a wall-time heuristic.** All
six captures report `stdout_line_count` of exactly **28256364** with
`exit_code` 0 and `read_size` 65536, so every run did identical work. (This is
the check the collection script *should* have used from the start; its first
version asserted capture size, which is structurally zero for `--mode echo`,
and its fallback asserted a wall-time floor. See the liveness-traps entry.)

**Mechanism of the miss: the projection subtracted equal weights from N and D;
the implementation removed far more from D.** The threshold-revision entry
projected a post-hoist share by subtracting the same absolute frame weights
from numerator and denominator — `N` 10704 → 6474 and `D` 30822 → 26592, giving
24.35%. Measured on one rule set (r2, the middle pair), the hoist removed:

|                 | control | candidate | removed  | share of control |
| --------------- | ------- | --------- | -------- | ---------------- |
| N (numerator)   | 8828    | 5317      | **3511** | 39.8%            |
| D (denominator) | 25743   | 17758     | **7985** | 31.0%            |

So 7985 samples of denominator work disappeared, but only 3511 of them were in
stacks the numerator counted. The other **4474 samples were pure denominator**:
stacks inside the consume subtree that carried a hoisted frame but never had a
generated constructor on the stack at the moment they were sampled. Removing
work that only D was counting necessarily *raises* the share, and it is the
whole of the 5.6-point gap between the 24.35% projection and the 29.91%
measurement. N fell by a larger *fraction* than D (39.8% vs 31.0%), which is
why the share still moved the right way; it did not fall far enough to clear a
bar that had been sited on the assumption that the two removals were equal.

**This is the same trap the threshold entry named, one level up.** That entry
correctly diagnosed that a post-hoist projection must use the post-hoist
denominator, and correctly refused a 25% bar computed from the control's
denominator. What it did not do was measure *which* frames the hoist removes
from D — it estimated that the removals were equal because they were the same
frames. They are the same frames, but D and N count them differently: N counts
a stack once however many matching frames it holds, while D counts every stack
the frames appear in. The revision's own lesson — "which frames a given
implementation actually removes is not knowable before it is written" — was
applied to the numerator and not to the denominator.

**Second confound, recorded so the derivation is not read as cleaner than it
is.** The projection was computed on the *pre-re-baseline* classifier and
compared against a *post-re-baseline* measurement; re-baselining the rules to
the single merged construction rule moved the control's own accounting (the
control capture reads 34.73% / N 10704 / D 30822 before, and 34.29% / N 8828 /
D 25743 after). The 34%-to-29.9% movement is therefore not attributable to the
hoist alone across that boundary. The r2 control-vs-candidate table above is
the clean comparison: both sides measured with the identical rule set at the
identical revision of the classifier, so the 3511/7985 decomposition is
unconfounded.

**This is the second miss of the same gate and the first with real code.** The
first (10%, pre-implementation) produced the threshold revision to 28%, which
was approved on a projection of 24.35–26.91%. The implemented design measures
29.91%. So the approved bar rests on a projection that the implementation
falsified — which also means the revision did not, as hoped, "sit one point
above the aggressive projection"; it sits 3.0 points *below* the measured
result.

### 2026-09-27: the residual numerator is the constructor itself, and `frozen=True` is why

EP-M2's own instruction is to "present the measured limitation for design
revision", so this characterizes *what* the remaining 29.91% is before anyone
proposes a replacement bar. The answer is narrower than expected and it is the
single most useful input to that decision.

**Every numerator sample is inside the constructor, not around it.**
Decomposing the r2 pair's numerator by innermost executing frame gives, under
both variants, **100.00% in a frame whose leaf is `__init__ (<string>:N)`** —
the generated `ExecEvent.__init__` itself. Zero numerator samples are
attributed to a caller that merely has the constructor on its stack
(`emit_line`, `emit`, `emit_fail_fast`). The recount reproduces the classifier
exactly (control D=25743/N=8828/34.2928%, candidate D=17758/N=5317/29.9414%),
so this is the third independent confirmation of the same numbers and the first
with attribution. The consequence is that the numerator is not "event-emission
overhead" in general; it is the cost of one generated `__init__` per line,
measured.

**That cost is dominated by `frozen=True`.** Microbenchmarked on this host's
interpreter (3.14.4, 300k reps, 27 fields, defaults included), constructing the
shipped type and three comparisons:

| construction                                                         | ns/ctor    | ratio to shipped |
| -------------------------------------------------------------------- | ---------- | ---------------- |
| **shipped** — `@dc.dataclass(frozen=True, slots=True)`               | 1890.3     | 1.0000           |
| control — `@dc.dataclass(slots=True)`, `frozen=False`                | 341.6      | **0.18×**        |
| frozen, handwritten `object.__setattr__` `__init__`, 27 named params | 1765.8     | 0.9342           |
| **frozen, handwritten descriptor `__init__`, 27 named params**       | **1278.3** | **0.6763**       |

**Why.** `dataclasses` implements `frozen=True` by emitting, per field, a full
`CALL` to `object.__setattr__` — 27 load-attr/call/pop sequences in one
function body (disassembled from the shipped type; `co_names` is
`('__setattr__',)` and the body is 27 uniform 30-byte blocks). So the frozen
guard costs roughly 5.5× the entire construction of the same 27 fields without
it. Dropping `frozen=True` is by far the largest lever measured here, and it is
a 5.5× reduction — with the descriptor route below recovering 0.68× of it while
keeping every invariant.

**An invariant-preserving lever also exists, and it is larger than first
measured.** A named-parameter `__init__` that hoists the slot-descriptor
setters out of the loop measures **0.6763×** the shipped constructor and passes
the full dataclass protocol surface, not merely the three properties an earlier
draft of this entry checked. **This supersedes a 0.88× figure recorded earlier
in this same entry**: that measurement used an `*args` `__init__`, which cannot
accept keywords, so `dc.replace` and keyword construction break and the variant
is not a drop-in — see the entry below for the correction and the protocol
matrix that caught it. The corrected 0.68× is a 32% reduction in constructor
cost, comfortably larger than the 1.90-point miss.

**Any post-change share must be measured, not scaled.** This plan has now seen
two projections falsified by measurement (the 10% bar, then the 24.35%
revision), and the second failed for the structural reason this entry supplies:
the share is a ratio in which the constructor appears in both terms, so a
cheaper constructor moves N and D together. The model is therefore not
`share × 0.68`; it needs the coupling applied explicitly, which is what the
superseding entry below does before quoting 22.42%. Even then it is arithmetic
on a microbenchmark, and the same three-pair protocol that produced the 29.91%
is what confirms it.

**One implementation consequence worth flagging now.** A handwritten `__init__`
in `cuprum/events.py` renders as `__init__ (cuprum/events.py:N)`, whereas the
generated one renders as `__init__ (<string>:N)`. The classifier's construction
rule matches on the `<string>` location specifically, so adopting this lever
requires re-baselining `classifier-rules.json` and re-verifying the control
capture with it — the same re-baseline discipline the merged rule already went
through, not a one-line edit.

**This is not a licence to drop `frozen=True`, and the plan does not propose
doing so.** Three things hold it in place: it is *pre-existing public API* —
`@dc.dataclass(frozen=True, slots=True)` on `ExecEvent` dates to `2aa9b2c1`
("Implement structured pipeline events and telemetry", PR #16) and this branch
does not touch `cuprum/events.py` at all; the type is hashable as a result, so
immutability is part of its contract rather than an internal detail; and three
suites assert `FrozenInstanceError` on event writes (`test_line_events.py`,
`test_line_event_emission_parity.py`,
`test_line_event_emission_properties.py`), one of them a property test.
Weakening it is a public-API change that the tolerances require be approved
separately, not a tuning move inside 5.2.1.

**What this means for the threshold decision, stated plainly.** If
`frozen=True` is held fixed and the generated constructor stays, the floor for
any design that constructs a fresh 27-field `ExecEvent` per line — which V2/V4
require — is this constructor, and the measured 29.91% sits about 1.9 points
above a bar that no further hoisting can move. Three routes exist and they are
not equivalent: re-site the threshold on the measured artefact; keep the bar
and take the 0.68× descriptor lever, measuring the result; or reopen
`frozen=True` itself, a public-API change worth 5.5× and therefore a
roadmap-level decision rather than tuning. The one thing this entry rules out
is a third threshold revision derived from another projection.

### 2026-09-27: the hoist removed denominator-only work — the mechanism, at source level

The reason the share did not fall as projected is now fully traceable, and the
trace is the most direct answer to "why did a correct implementation miss?"
that this plan can give.

**Split D by leaf frame class on the r2 pair** (stacks in the consume subtree,
partitioned by whether a generated constructor is also on the stack):

|                                          | control   | candidate | removed  |
| ---------------------------------------- | --------- | --------- | -------- |
| D (consume subtree)                      | 25743     | 17758     | 7985     |
| N (has the constructor on the stack)     | 8828      | 5317      | 3511     |
| **D-only** (no constructor on the stack) | **16915** | **12441** | **4474** |

Of the 7985 samples the hoist removed from the denominator, only 3511 were in
stacks the numerator counts. The other **4474 were denominator-only** — and the
twelve largest sources of that removal are *exactly the three things EP-M2 was
scoped to remove*:

| leaf frame                                   | control | candidate | removed                        |
| -------------------------------------------- | ------- | --------- | ------------------------------ |
| `emit (cuprum/_pipeline_types.py)`           | 4471    | 0         | **4471**                       |
| `_event_details (cuprum/_line_callbacks.py)` | 1212    | 0         | **1212**                       |
| `argv_with_program (cuprum/sh/safe_cmd.py)`  | 337     | 0         | **337**                        |
| all other leaves combined                    | 10895   | 12441     | −1546 (noise, both directions) |

Those three sum to **6020 of the 4474** — they do not merely dominate the
removal, they over-explain it, and the excess is offset by ordinary sampling
noise in the unchanged leaves (some, like `bound_line`, actually *rose*). Zero
samples remain in any of the three under the candidate: every call site is
gone, which is the signature of a hoist that removed the work rather than
moving it.

**Why they were D-only, verified rather than inferred.** A representative
control stack for the largest source ends
`emit_line (cuprum/_line_callbacks.py:109)` →
`emit (cuprum/_pipeline_types.py:135)`. The sample lands *inside* `emit` while
it is assembling the per-line payload — before the constructor is reached — so
the stack has no generated frame and the numerator cannot count it. The
candidate's `_LineEventEmitter.emit_line` calls
`self.emit_event(ExecEvent(...))` directly, so the `emit` hop does not exist at
all. Removing a frame that only ever appeared below the numerator is
arithmetically guaranteed to raise `N/D`, because it subtracts from D alone.

**So `emit` is the decisive one.** Not the `_EventDetails` constructor as the
projection assumed — that was 1212 samples here — but the 4471 samples spent in
the *hop* that built the payload before constructing the event. The projection
priced the constructor and the helper; it could not price the frame the helper
was reached through, because that frame's weight only appears in a real
capture. This is precisely the plan's own recorded lesson — "which frames a
given implementation actually removes is not knowable before it is written" —
and it turned out to bind hardest on the term the projection treated as
unchanged.

**A consequence that generalizes beyond this gate.** Because 100% of the
numerator is inside the constructor (previous entry), *any* optimization that
removes sample weight from the consume subtree participates in the numerator
only if the sampled frame happens to sit inside the constructor. Work
eliminated before the constructor call lands in D alone and raises the share.
So on this classifier, a strictly faster emission path can score *worse* than a
slower one, which is exactly what happened: 30.96% less wall time, 1.9 points
more share. That property, not the 29.91% itself, is what a design revision
needs to weigh — a percentage-of-total-work gate measures how the total is
spent, and this work was spent so that the total shrank.

### 2026-09-27: the next roadmap item will raise this same number (5.2.2)

The decomposition generalizes further than this gate, and the consequence lands
on the next item in the phase, so it is recorded here rather than left for that
item's own plan to rediscover.

Roadmap **5.2.2** — "Remove the per-hook `inspect.isawaitable` call from the
per-line path" — depends on 5.2.1 and is scoped against the same kind of
evidence: "a committed profiler artefact shows `inspect.isawaitable`
contributes 0 sampled frames in the per-line hot path". In the *candidate*
capture that symbol is **589 samples, every one of them D-only**, with the
stack ending

`emit_line (_line_callbacks.py:216)` → `emit_line (_line_callbacks.py:135)` →
`_emit_event (_pipeline_types.py:203)` →
`_emit_exec_event (_observability.py:108)` → `isawaitable (inspect.py:371)`

so it is genuinely per-line work of exactly the kind 5.2.2 targets, reached
through the hook dispatcher rather than through the constructor.

**Removing it subtracts 589 from D and 0 from N.** At the r2 candidate's
D=17758 and N=5317, that moves the share from 29.9414% to **30.9686%**, a rise
of **+1.03 points** — a *worse* number on this gate than the one 5.2.1 is
currently blocked on, from work the roadmap explicitly wants done. That is not
an argument against 5.2.2; the work is worth doing and the 0-frame goal in its
own success criterion is stated on the frame count, which 5.2.2 will meet
cleanly. It is an argument that **5.2.1's construction-share gate cannot be the
acceptance instrument for 5.2.2**, and more sharply, that a threshold sited
just above a predecessor's measured result is a threshold the next optimization
will breach by succeeding.

**The structural reason, restated once more in its most general form.** The
numerator counts samples *inside* `ExecEvent.__init__` and nothing else
(measured: 100%, previous entry). D counts all work in the consume subtree. Any
change that reduces consume-subtree work without reducing the number of
constructor calls lowers the denominator while holding the numerator, so it
raises the ratio. Every optimization in this phase has that shape. A
percentage-of-total gate therefore cannot express "the hot path got faster"
across a series of such changes, however well it discriminates the first one.

**Recommended for whoever re-sites the threshold.** Either (a) state the gate
as an absolute numerator rate — constructor samples per emitted line, which is
what "per-line emission no longer reconstructs invariant fields" actually
claims and which is invariant under unrelated work being removed — or (b) keep
the share form and re-measure the bar per item against its own predecessor,
accepting that the number is a description of the current workload rather than
a target. What this plan can supply for either route is a measured,
reproducible artefact and the decomposition above, which separates the
constructor's contribution from everything else.

### 2026-09-27: the bar is clearable — the descriptor lever, at 0.68× (superseding entry)

**An earlier version of this entry is superseded and its numbers were wrong.**
It reported a 0.8770 descriptor ratio clearing 28% by 0.74 points, measured on
a candidate whose `__init__` took `*args`. That candidate is not a drop-in
replacement: `*args` rejects keywords, so `dc.replace(inst, phase=...)` and
ordinary keyword construction both raise `TypeError`. The protocol-equivalence
check that caught it also showed the same candidate failing `pickle` and
`copy` — and both earlier measurements were blind to this because each verified
only the three properties named in the entry text (slots,
`FrozenInstanceError`, equality) rather than the protocol surface. Recorded
rather than quietly replaced, because "I verified the invariants I thought to
name" is how a 0.88 became a recommendation.

**The corrected result is better, not worse.** Generating a *named-parameter*
`__init__` that hoists the slot-descriptor setters out of the loop, and
reproducing by hand the `__getstate__`/`__setstate__` hooks dataclasses
installs alongside `slots=`, gives a full protocol-equivalent drop-in:

| construction                                            | ns/ctor    | ratio to shipped |
| ------------------------------------------------------- | ---------- | ---------------- |
| shipped — generated `frozen=True, slots=True`           | 1890.3     | 1.0000           |
| descriptor `__init__`, 27 named params, hoisted setters | **1278.3** | **0.6763**       |
| `object.__setattr__` `__init__`, 27 named params        | 1765.8     | 0.9342           |

Checked against the shipped type on the full surface, each comparison running
on both: `dc.fields` (27), `dc.replace`, `dc.asdict`, `dc.astuple`, keyword
construction, `pickle` round-trip, `copy.copy`, `copy.deepcopy`, `hash`/set
membership, `repr`, `FrozenInstanceError` on write, and 27 slots. **All twelve
pass on all three classes**, and the shipped and descriptor rows agree in every
column. The `object.__setattr__` variant is also equivalent, which is what
makes it a useful control: the win is the descriptor dispatch, not the named
parameters.

**Share model at r2 (D=17758, N=5317):**

| construction                     | ratio      | numerator | denominator | share        | against 28%     |
| -------------------------------- | ---------- | --------- | ----------- | ------------ | --------------- |
| shipped                          | 1.0000     | 5317      | 17758       | 29.9414%     | fail, +1.94     |
| `object.__setattr__` named-param | 0.9342     | 4967      | 17408       | 28.5329%     | fail, +0.53     |
| **descriptor named-param**       | **0.6763** | **3596**  | **16037**   | **22.4231%** | **pass, −5.58** |

The model is the one the decomposition licenses: samples inside the constructor
scale with its cost, and what leaves the numerator leaves the denominator with
it. It is the same `N/D` coupling that defeated the 24.35% projection — applied
here with a measured ratio for the constructor rather than an assumed frame set.

**Why this margin is credible where the superseded 0.74 was not.** −5.58 points
against a control between-pair spread of 1.31 and a candidate spread of 0.0423
is a gap no plausible load effect closes, and unlike the earlier figure it
comes from a candidate that passes the protocol surface the shipped type
defines. It is still a model, not a capture: no three-pair V5 collection has
been run against a descriptor-based constructor, and two projections in this
plan have already been falsified by measurement, so confirmation by the same
protocol that produced the 29.91% remains required before any number here is
citable.

**Two implementation facts a future implementer needs, both verified.**
*Defaults:* 16 of the 27 fields carry defaults, all exact literals — no
`default_factory` anywhere in the type — so a generated `__init__` reproduces
them by binding `field.default` into the generated code's globals, with no
sentinel and no `_MISSING` handling. *Drift:* a handwritten `__init__`
duplicates the signature, so a field added to the class would leave
`dc.replace` /`asdict` inconsistent with the constructor. Generating the
`__init__` *from* `dc.fields(cls)` removes that risk — tested by rebuilding
with an extra field, where the signature grew to match (9 params, 9 fields),
`dc.replace` worked on the new field, and `asdict` keys stayed equal to the
field set. A class whose `__init__` is written by hand rather than generated
fails `dc.replace` on an added field with `TypeError`, which is the drift the
generation exists to prevent.

**Recommendation, now with a number behind it.** Take the descriptor lever (or
re-site the threshold, or reopen `frozen=True` for 5.8×) — but if the lever is
taken, budget for the re-baseline it requires: a handwritten `__init__` renders
as `__init__ (cuprum/events.py:N)`, so the classifier's `<string>` construction
rule stops matching and `classifier-rules.json`, the control capture, and the
three-pair collection all have to be redone before any number from it is
citable. That is a milestone of work, not a commit, and it is the real cost of
route 2 compared with the other two.

### 2026-09-27: V5 collection complete — verdict FAIL; two timings re-measured

**The collection finished with all 30 unprofiled rounds and no liveness
failure.** `/tmp/smoke521/v5-verdict.py` derives the verdict from the directory
rather than from hand arithmetic:

```text
PROFILED FAIL: r1-candidate: candidate share 29.8991 > 28.0
PROFILED FAIL: r2-candidate: candidate share 29.9414 > 28.0
PROFILED FAIL: r3-candidate: candidate share 29.9087 > 28.0

UNPROFILED (per scenario, never pooled)
cb           control median  242.534s candidate median  178.789s delta  -26.28% (n=5/5)
echo-nocb    control median    2.156s candidate median    2.140s delta   -0.70% (n=5/5)
tee-nocb     control median    4.688s candidate median    3.306s delta  -29.49% (n=5/5)

UNPROFILED: ALL TOLERANCES MET

V5 VERDICT: FAIL
```

So the regression tolerances all pass — no scenario is more than 5% slower, and
the callback scenario is 26.28% faster unprofiled (broader than the 30.96% the
profiled pairs showed, because that figure included py-spy's own overhead). The
only failing tolerance is the share itself. All 30 unprofiled runs were checked
for liveness under the corrected rules: 30/30 rc=0, `tee-nocb` capturing
2147483648 bytes each time, `echo-nocb` and `cb` clearing the wall-time floor
that substitutes for capture size in echo mode.

**`tee-nocb`'s −29.49% is a host artefact, and this was tested rather than
excused.** It is the one timing that did not make sense: `tee-nocb` runs
`with_line_callbacks=False`, and both the control and the candidate return
`None` from `_compose_line_callbacks` before the hoisted code is reached
(verified in both trees — the early return at control `_line_callbacks.py:102`
and candidate `_line_callbacks.py:208`), so the production diff of 119 lines in
that one file cannot reach it. Re-measured alone on a quieter host (8
alternating rounds, load 2.7–3.2 against the collection's 5.95–7.64):

|           | median     | min     | max     |
| --------- | ---------- | ------- | ------- |
| control   | 4.574 s    | 4.517 s | 4.665 s |
| candidate | 4.582 s    | 4.497 s | 4.668 s |
| **delta** | **+0.18%** |         |         |

The distributions overlap completely (candidate min 4.497 below control min
4.517; candidate max 4.668 above control max 4.665) where the collection's two
sets were disjoint (control min 4.462 above candidate max 4.171). The
collection's apparent separation was load, not code. This is recorded because
it is the one place where the collection produced a *large favourable* result
in a scenario the change cannot touch — the direction that invites being
reported without checking — and because it is a concrete demonstration of why
V5's "documenting the interference" clause needs the load numbers it already
requires.

**`echo-nocb`'s −0.70% is the honest null**, and its consistency with the
isolated `tee-nocb` re-measurement (+0.18%) is the check that the no-callback
path is genuinely untouched by the hoist. Two independent no-callback scenarios
landing within a percentage point of parity is what "the change does not affect
this path" looks like in measurement.

### 2026-09-27: the evidence artefact is committed, and one of its claims was false

`docs/tee-hotpath-line-event-emission-5-2-1.md` now carries the EP-M3 evidence:
the verdict, the protocol, both commands, the folded-format sample counts, the
decomposition of the miss, the residual-numerator finding, and the acceptance
table. It is committed so the R3 decision rests on a reviewable artefact rather
than on this plan's prose. Its verdict line was later revised from "FAIL
against 28%" to "PASS at the approved 30%" without re-running the collection;
the reclassification is recorded in "The 30% revision, and why the margin is
thin" and the artefact carries the same check in its own words.

Four numbers in the first draft were wrong and were caught by re-deriving each
from the raw captures rather than from working notes: the measured revision
(recorded as the branch tip `94ebcda1` instead of the `f4d1010a` that actually
ran), the framing of the section (written as "the share did not fall" when it
fell 34.2928% → 29.9087%), a percentage column that mixed two denominators, and
a table row that held its neighbour's load figures. The first is the dangerous
one: `git diff f4d1010a..94ebcda1` touches only this plan, so the SHA the draft
named by mistake referred to a revision that was byte-identical where it
mattered — the artefact would have been *wrong but checkable as consistent*.

A fifth defect was found later and is the reason for the correction commit
`dd2df8d0`. The protocol section explained variant selection as "`cuprum` is
imported from the current working directory rather than from `site-packages`".
The first clause is true; the second is false in the direction that matters.
The candidate venv's `site-packages` **does** contain a `cuprum.pth`, and it
points at the **candidate** worktree. Had that entry won the `sys.path` race,
both variants would have imported the candidate and the collection would have
reported a near-zero spread — a clean-looking, entirely meaningless result.

The mechanism that actually holds is ordering, verified empirically from the
control worktree: under `python -m` the empty string is `sys.path[0]`, so the
cwd precedes `site-packages` (index 4) and the `.pth` path it adds (index 5);
`cuprum.__file__` resolved to the control tree, `_LineEventEmitter` was absent
and `_event_details` present. The measurement stands unchanged — only the
stated reason was wrong.

The generalizable lesson is recorded in the artefact's own terms: a validity
argument that *sounds* structural ("cwd beats site-packages") deserves the same
empirical check as a number, because here the superficially-similar true
statement and the false one differ by one path index.

### 2026-09-27: the gate suite is green at `6da258a6`, and `test-rust` needed covering

All six deterministic gates now pass at `6da258a6` with HEAD unmoved
(`head_before = head_after` on every run, tree clean throughout): `check-fmt`,
`markdownlint` (chaining `spelling`), `typecheck`, `test`, `lint` (all ten
sub-checks reached, so none was left unobserved), and `nixie`. The `actionlint`/
`shellcheck` deadlock did not reproduce under the `</dev/null` redirect.

The first `make test` observation **failed**, and the way it failed is the part
worth recording. It exited 2 on

```text
cuprum/unittests/test_idle_heartbeat_coordination.py::test_a_shared_sink_keeps_the_pipeline_keepalive_on_its_own_line
```

a known load-induced flake inherited from `main`, carrying a recorded symptom
(keepalives preceding the consumer's own first bytes, `_INTERVAL = 0.2`,
interpreter boot exceeding two intervals) that matched this failure exactly.
`make test`'s prerequisite order is `test: makeutil test-python test-rust`; the
abort at `test-python` meant **`test-rust` was never invoked**. That is an
unobserved check, not a passing one, and it is the reason a "5 of 6 passed"
headline would have been wrong: the suite's Rust half — the nextest run and the
doctest pass — had not run at all.

Both gaps are now closed. `test-rust` was run on its own
(`125 tests run: 125 passed, 0 skipped`, doctests `ok`), and the full suite was
re-run to exit 0 with `2549 passed, 63 skipped`, reaching `test-rust` this time
and seeing the previously-failing keepalive test **pass**. The two full-suite
observations reconcile exactly: both collected 2612 items, with one flipping
from failed to passed and nothing else changing.

**Two earlier citations in this plan named the wrong test, and the near-miss is
worth recording.** The plan twice recorded the flake as
`test_a_shared_sink_keeps_the_keepalive_on_its_own_line` — the name used by
`cuprum/unittests/test_idle_heartbeat_execution.py`. The failing test above is
`cuprum/unittests/test_idle_heartbeat_coordination.py::test_a_shared_sink_keeps_the_pipeline_keepalive_on_its_own_line`.
Two real tests, in two files, differing by the single word `pipeline_`, and
both present in every full-suite log — so grepping a log for either name finds
both and cannot disambiguate. `coordination.py` has carried `pipeline_` since
the file was added in `b63a0f21`, so no version of the tree ever supported the
short name in that file.

The recorded symptom does disambiguate, and it identifies the coordination
test. Its distinguishing detail is smaller than expected: both tests assert
that a sink's leading bytes are unchanged, and their messages differ by three
words — `coordination.py:258` says "the **stage's** own bytes must be
unchanged" while `execution.py:292` says "the **child's**". Only the
coordination test writes `partial-final`; the execution test writes
`partial\n`. The 04:56 entry records "the stage's own bytes must be unchanged"
alongside `partial-final`, so it is the coordination test on both counts. Both
older entries are corrected above.

A separate background search found a genuine historical `FAILED` line for the
**execution** test as well — so that test does flake too, which is presumably
how the name came to be written down. Its log has since been recycled from
`/tmp` and cannot be dated, so it is not claimed as the source of the 04:56
citation. What the surviving evidence supports is narrower and sufficient: the
04:56 failure was the coordination test, and the execution test independently
flakes with a near-identical message.

The generalizable part: when two identifiers differ by one word and both appear
in the same logs, a prose citation of either is unverifiable except against the
tree, and a gate will not catch it — no linter resolves prose identifiers. Here
the ambiguity was worse than the names: the two assertion messages differ by
three words and both describe the same failure mode, so a reader checking the
citation against a remembered symptom would confirm it. Cite the file alongside
the test name, and quote assertion text verbatim rather than paraphrasing it.

**A prediction of mine failed here, and the measurement is stronger for it.**
Before dispatching the second run I read `uptime`, saw the 1-minute load fall
from 16–27 to 3.08, and told the gate runner that this was "the condition under
which the flake was predicted not to fire" — framing the re-run as a valid
observation rather than a retry. That premise did not hold: load was back to
**17.53** by the time the full suite ran, and the pass came at essentially the
load that produced the failure. So the result is *not* explained by "the host
got quieter". The flake conclusion survives on the change-surface evidence and
on the recorded symptom, both of which were already independent of load — and
it is now better supported, because a passing run at the failing load rules out
load as a sufficient explanation for the failure. The lesson is narrow and
worth keeping: `uptime` read at dispatch time does not predict host load for
the duration of a four-minute suite, so a load-based prediction about a gate
outcome is not something to assert in advance.

### 2026-09-27: the 30% revision, and why the margin is thin

The user approved a second revision, **28% → 30%**, quoting this plan's own
tolerance clause back at it — "a missed performance target is a design
exception, not permission to weaken acceptance… seek approval for a revised
design" — and then granting it: "Treat 30% as the new target." The plan had
been set to BLOCKED precisely so that this decision was the user's to make
rather than a re-siting performed unilaterally, so the process worked as
designed: the measurement was produced first, the options were laid out, and
the target moved on the measurement.

**The revision is a change of target, not of measurement, and that was checked
rather than asserted.** All six captures were re-classified at the 30% limit
using the committed classifier and rule file, unchanged from the collection:

| capture      | share    | at 28% (as collected) | at 30% (re-run)  |
| ------------ | -------- | --------------------- | ---------------- |
| r1-candidate | 29.8991% | fail_above_limit      | **pass**         |
| r1-control   | 35.3533% | fail_above_limit      | fail_above_limit |
| r2-candidate | 29.9414% | fail_above_limit      | **pass**         |
| r2-control   | 34.2928% | fail_above_limit      | fail_above_limit |
| r3-candidate | 29.9087% | fail_above_limit      | **pass**         |
| r3-control   | 34.0388% | fail_above_limit      | fail_above_limit |

Every `construction_share_percent` is byte-identical between the two runs; only
`limit_percent` and `status` moved. That is the check that the revision did not
touch the quantity being judged, and it is the reason the artefact's Result
section can be rewritten without re-running the collection (30.96 minutes of
profiled wall time per pair).

**The margin at 30% is thin, and is recorded as thin rather than as comfort.**
In denominator terms:

| pair         | N    | D     | share    | D for 30% | headroom | as % of required |
| ------------ | ---- | ----- | -------- | --------- | -------- | ---------------- |
| r1-candidate | 5244 | 17539 | 29.8991% | 17480.0   | +59.0    | 0.34%            |
| r2-candidate | 5317 | 17758 | 29.9414% | 17723.3   | +34.7    | 0.20%            |
| r3-candidate | 5208 | 17413 | 29.9087% | 17360.0   | +53.0    | 0.31%            |

The tightest pair clears by 0.0586 points, which is larger than the
0.0423-point spread across the three pairs — so the pass is not sampling noise
— but the same order of magnitude, which means the criterion sits close to
being undecidable on this instrument. Two consequences are carried into the
artefact: the measurement should not be trusted to detect a small regression,
and the metric's systematic inversion (below) applies to any future change here.

**The metric is structurally non-monotonic, and at 30% that becomes a dated
forecast rather than a caveat.** 100% of the numerator sits inside the retained
generated `ExecEvent.__init__`, so *removing pre-constructor work lowers D
while holding N and raises the share*. Item 5.2.2 removes `inspect.isawaitable`
from the per-line path — 589 denominator-only samples — which by succeeding
would move this same number from 29.9414% to **30.9686%**, i.e. 0.9686 points
*above* the bar. So this criterion is met at this commit and will be
contradicted by the next roadmap item. That is not a reason to withhold the
pass — 5.2.1's Success text is written as a state, not a trend — but it is why
5.2.2 must be judged on its own criterion (`inspect.isawaitable` contributes 0
sampled frames in the per-line path) and why anyone re-running this gate after
5.2.2 lands should read a failure as the metric's known inversion rather than
as a regression.

**The two design changes that would have closed the 28% gap were rejected, and
the grounds matter for the retrospective.** Neither was rejected for cost:

- A handwritten descriptor `__init__` on `ExecEvent` measures 0.68× the stock
  constructor (1278.3 vs 1890.3 ns/ctor), which the artefact **models** as
  reaching **22.42%** — under even the original 28% bar with room. It was
  rejected because it replaces the generated constructor with hand-maintained
  code that must reproduce field order, defaults, `__eq__`, `__repr__`, and
  `dataclasses.fields()` introspection exactly. The field-drift objection does
  **not** apply: the variant's constructor is generated from `dc.fields()` and
  was checked against an added field (signature grew to match, `dc.replace`
  worked on the new field, `asdict` keys stayed equal to the field set), so a
  field addition is caught rather than missed. What remains is the ongoing cost
  of a hand-rolled generator — a public-API code surface traded for a
  percentage the user has since granted by revising the target instead. Note
  also that 22.42% is a modelled share derived from a microbenchmark, not a
  measured workload result.
- Reopening `ExecEvent`'s `frozen=True` measures 0.18× (341.6 ns/ctor) and is
  the single largest available lever, because `frozen=True` dominates the cost:
  it emits 27 `object.__setattr__` calls per construction. It was rejected as a
  public-API semantics change — hashability and immutability for every consumer
  — outside 5.2.1's approved scope and unable to be justified by a threshold
  that was itself derived from a projection.

Both rejections are the legibility-and-maintainability trade-off the user asked
to have set out, and they are detailed in the retrospective below. Recording
them here is what makes 30% an honest number: the target moved because the
measurement said the design's floor was real, not because no cheaper design
existed.

### 2026-09-28: `make lint` reads Markdown, so a docs-only edit is not out of scope

This plan asserted, in the section now superseded below and in the PR
description, that a documentation-only edit "cannot invalidate a suite whose
scope excludes docs" except through the four Markdown gates. **That is false**,
and it was one commit away from being the justification for not re-running the
full suite after the documentation commits.

`make lint` → `python-lint` → `skylos` reads the repository's `.md`, `.rst`, and
`.txt` files to credit symbols as live, and this branch's *own* new class is
on the receiving end: `cuprum._line_callbacks._LineEventEmitter.emit_line`
carries `documented_public_api` evidence sourced from the class-qualified
`` `_LineEventEmitter.emit_line` `` mentions in this plan and in the evidence
report. A prose edit that disturbs such a mention changes the lint verdict —
the failure mode is a rename or a reflow in a document, and it is invisible to
every Markdown gate.

The claim was tested rather than argued, with the pinned CLI (`4.33.2`) run over
`cuprum` three ways: (1) on the content `611f0f2d` shipped → 22
liveness-credited symbols, `unused_functions: 0`; (2) with both documents
reverted to `d98fb5c9` → byte-identical verdict; (3) with the class-qualified
mentions degraded to a bare `LineEventEmitter.emit_line` → credit lost,
`unused_functions: 1`, an `SKY-U001` failure. Step 3 is the control that shows
the mechanism is live rather than dormant; step 2 is what licenses the
documentation-only delta.

Step 3 also needed a **second** step, which is the part worth remembering. The
raw `sed` degradation shortened some lines, so `mdtablefix` and `check-fmt`
failed on *rewrapping* rather than on the semantic change — the first attempt
at the control proved nothing. Only after re-canonicalizing both files with
`mdtablefix --in-place` did the intended state appear: all four Markdown gates
exit 0, and `skylos` still fails. That two-step form is the experiment; the
one-step form is refuted by anyone who runs it.

The practical rule: before calling a delta "docs-only, so gates are
unnecessary", list the gates' actual inputs. Here the honest statement is
narrower than the one first written — `lint` *does* read these documents, and
the delta above the full-suite runs is covered by re-running `skylos` over both
documentation revisions, not by the sweeps.

### 2026-09-28: a content SHA-256 is not a git object id

The ten-link sweep chain is verified by comparing each sweep's recorded digest
against the revision it gated. The obvious way to do that is wrong in a way
that reports failure on a sound chain:

```bash
git rev-parse "$commit:$path"      # 40-hex SHA-1 object id — NOT comparable
git show "$commit:$path" | sha256sum   # content SHA-256 — the right form
```

`sha256sum` hashes the file's bytes; `git rev-parse <commit>:<path>` returns a
SHA-1 over a length-prefixed payload (`blob <n>\0` + contents). Both are bare
hex strings that a log presents as "the hash of this file", which is what makes
this a trap rather than a typo. Comparing them reported MISMATCH on **every**
link of a chain that was in fact sound — and that all-links-fail signature is
itself the tell: ten independent regressions do not happen at once, so a
uniform failure indicts the comparison, not the artefact.

The chain record is also anchored by *content digests* rather than commit
labels, and that choice was forced by a second near-miss. An earlier version of
the `skylos` evidence cited a commit name for an arm whose output file carries
no revision field at all — the label came from the filename it had been saved
under, i.e. from the archivist rather than from the measurement. Both arms were
re-run under a run window that logs the digest of each input, so the pairing is
now checkable by a reader instead of trusted: plan `aa723ff8` + report
`cf276c88` against plan `452fd262` + report `b910858d`.

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
- 2026-09-27: Split the 654-line `test_line_event_emission` module by *subject*
  into four modules under the same name prefix, rather than trimming it to fit
  CodeScene's Low Cohesion rule. The rule is a **critical** finding on the *Pay
  Down Tech Debt* profile, and every one of the 12 most recent merged PRs
  passes that check, so it is the repo's real bar even though it is not in the
  required ruleset. The prefix is load-bearing: the plan's `FOCUSED_TESTS` glob
  is `test_line_event_emission*.py`. Test inventory verified unchanged at 22
  functions, `42 passed, 6 xfailed`.
- 2026-09-27: **EP-M2 is BLOCKED at 29.91% against the 28% bar**, per this
  plan's own instruction: "If it misses the **28%** threshold, record BLOCKED
  and present the measured limitation for design revision." The implementation
  is complete and correct (V1–V4 pass, candidate 30.96% faster in median wall
  time, identical work in every capture at 28256364 lines); it is the
  *threshold derivation* that the measurement falsifies. The miss is fully
  decomposed — 7985 samples of D removed against 3511 of N, with the 4474
  difference being pure-denominator stacks — so a revision can be derived from
  a real capture instead of a projection. **No threshold is proposed here**:
  the previous revision was approved on a projection and this plan has now
  falsified two of them, so the next one should be a design decision taken on
  the committed artefact, not another estimate from the plan. **Resolved later
  the same day:** the design decision was taken on the committed artefact, as
  this entry asked, and the bar moved to 30%. EP-M2 is no longer BLOCKED.
- 2026-09-27: **V5's threshold is revised from 28% to 30%** (user-approved),
  and R3 is met. The revision is sited on the measurement — 29.9087% median,
  29.9414% worst of three pairs — rather than on a further projection, which is
  the one property this entry was careful to require of its successor. The
  plan's own tolerance clause was invoked to obtain it: the plan was BLOCKED,
  the measurement and the four options were put to the user, and the target
  moved by explicit decision. Recorded in full in the discovery "The 30%
  revision, and why the margin is thin"; the roadmap's success criterion is
  amended to match.
- 2026-09-27: **The margin is 0.0586 points and must be reported as thin, not as
  a comfortable pass.** The three pairs spread 0.0423 points, so the pass is
  not sampling noise, but it is the same order of magnitude — the criterion is
  close to undecidable on this instrument. State that in the artefact, and
  state alongside it that the metric inverts: 100% of the numerator is the
  retained `ExecEvent.__init__`, so removing pre-constructor work raises the
  share. Item 5.2.2 would move 29.9414% to 30.9686% by succeeding. A pass that
  will be contradicted by the next scheduled item is recorded with the forecast
  attached, so the contradiction is not later mistaken for a regression.
- 2026-09-27: Record the two **corrected projections** in the BLOCKED entry
  rather than leaving the earlier draft's numbers. Both corrections were
  re-derived through the gate's own classifier and arithmetic, not restated:
  option 1's shares become 13.48% / 12.67% once the shrinking denominator is
  applied, and option 2's lifecycle/per-line split is a **no-op** on this
  capture because the ExecEvent rule carries zero caller weight outside the
  consume subtree. A design revision that proceeded on either earlier number
  would have been arguing from a figure this plan itself had already shown to
  be wrong.
- 2026-09-27: **V5's threshold is revised from 10% to 28%** (user-approved),
  and the 39% baseline comparison is retired with it. Derived from the
  aggressive post-hoist projection of EP-M2's already-fixed scope (26.91%)
  rounded up to one point of headroom. Recorded in full in the discovery above;
  the roadmap's success criterion should be amended to match.
- 2026-09-27: **A post-hoist projection must be computed on the post-hoist
  denominator.** The first draft of this entry proposed a 25% bar by dividing
  the projected numerator by the *control* denominator (30822). The aggressive
  projection is 26.91%, so that bar would have failed a correct implementation
  of the already-approved hoist. This is the same error as the naive `r·f_ev`
  form in a different guise — a numerator compared against a denominator that
  the change itself moves — and it is the fourth instance in this milestone.
  Projections are therefore stated as a **range** over what a given
  implementation might remove, not as a point estimate.
- 2026-09-27: **Judge every construction-cost lever on `share(r)`, never on
  `r·f_ev`.** The naive form has now been used three times in this milestone in
  place of the correct one (the option-1 projection, and the "two further
  levers" figures), each time understating the post-hoist share by 3.5–4.6
  percentage points and once producing a 10.92% that reads like a near miss
  against a true 14.33%. It is the intuitive form, which is why it keeps
  returning; the rule is to re-derive any projected share through the closed
  form before it is written down.
- 2026-09-27: **The roadmap's 39% baseline is unusable, not merely historical.**
  It traces to one table row in a narrower July-fixture document that states no
  denominator; this branch's committed control reads 34.73% (consume) and
  32.97% (all parent), and the smoke fixture reads 47.86%. The criterion
  therefore cannot be evaluated as written in either direction, which makes the
  open question a **threshold revision** rather than a design fix. Record this
  in the approval request; do not let a future reader re-derive a bar from a
  number with no denominator behind it.
- 2026-09-27: Treat the bounded `actionlint -shellcheck=` re-run as **valid
  evidence for this revision** rather than as a weakened substitute for the
  hung gate. The narrowing removes shell-syntax checking inside `run:` blocks,
  and the `.github` subtree SHA `7fb57438…` is identical at every revision in
  `8dd2ec0b..HEAD` *and* at `origin/main`, so the narrowed check covers every
  file the branch could have changed; `yamllint`, which shares the sub-check,
  is green alongside it. Record the *intermittent* character of the hang
  explicitly, because the earlier "deterministic" characterization would
  otherwise license a false permanent exemption.
- 2026-09-27: **Do not infer a gate's outcome from a log tail.** Two hung runs
  (`521b`, `521c`) were read as "killed at the `skylos` command line" purely
  from where their logs stopped; both actually ended on the `actionlint`
  invocation line like every other run, and neither wrapper had armed a bound
  at all. The general rule, now in the discovery: `make` echoes a recipe line
  before running it and actionlint prints nothing on success, so a **pass and a
  hang are byte-identical in the log**. Only an out-of-band exit status
  distinguishes them — `timeout 300 make lint 2>&1 | tee "$log"` with
  `rc=${PIPESTATUS[0]}` written to a status file beside the log. The scrutineer
  was asked to work this way and confirmed it independently.
- 2026-09-27: **Prefer a controlled A/B to an argument from circumstance.**
  The intermittent hang was argued for a long time from log tails, process
  counts, and an orphaned revision. It was settled in four minutes by running
  the same command twice on one frozen tree: `timeout 420 make lint` → exit 0
  in 52 s at 04:43, and
  `timeout 90 actionlint -config-file .github/actionlint.yaml` → exit 124 at
  04:45. Same tree hash, same binary, same `.github` content. When a phenomenon
  is suspected to be environmental, measuring it directly on a frozen input
  beats accumulating circumstantial cases.
- 2026-09-27: **A red gate in an untouched file is a load hypothesis to test,
  not a conclusion to assert.** `make test` went red on
  `test_idle_heartbeat_coordination.py::test_a_shared_sink_keeps_the_pipeline_keepalive_on_its_own_line`
  while the tree was frozen and the file byte-identical across the range. The
  response was to check load (22.2 falling to 9.1), run the test in isolation
  (3/3 pass), and re-run the suite bounded (exit 0, `2543 passed`), rather than
  either dismissing it or chasing a fix. The flake has no `/tmp` precedent, so
  "there is no earlier instance" is recorded as the actual state of the
  evidence rather than upgraded to "known flake".
- 2026-09-28: **An evidence directory's own manifest is part of its evidence.**
  The review of `docs/profiling/5-2-1-line-event-emission/README.md` found it
  claiming nine files per capture directory when only seven are committed;
  `.gitignore`'s `*.log` excludes `classifier.log` and `pyspy.log`. For a
  directory whose whole purpose is that a reader can recompute `N/D` instead of
  trusting a report, that is a correctness defect in the evidence, not a
  cosmetic one — it promises data the clone does not contain. The fix was to
  check whether anything depended on the missing files and then say so: an
  untracked file that a committed one already duplicates (`classifier.log`
  versus `construction-share.json`, byte-identical in all six directories) or
  that is derivable from a committed one (`pyspy.log`'s `Samples: <n>` line is
  the weight sum of the committed `stacks.folded`) costs the reader nothing.
  Where that is true, state it; where it is not, commit the data. The
  repository's own convention — no `.log` file is tracked anywhere — was
  treated as deliberate and left intact.
- 2026-09-28: **A docstring that names a value should name the constant that
  holds it.** `test_line_event_profile.py` said the classifier decides against
  "the plan's 10% share" two revisions after the limit became 30.0, because it
  restated a number instead of pointing at `CONSTRUCTION_SHARE_LIMIT_PERCENT`.
  It now names the constant, so the next revision cannot stale it. The two
  sites that *should* keep the old numbers — the classifier's header comment
  and this plan's decision log — are dated audit trails of each revision and
  were deliberately left alone; the distinction is between a live claim and a
  record of a past decision.
- 2026-09-28: **Record a sweep chain as a shape plus a verified snapshot, never
  as a count.** The paragraph recording this branch's docs-scoped gate sweeps
  first said "five commits sit above `d98fb5c9`" — and committing it made the
  count six, and every later documentation commit would have done the same to
  any number written there. It is now a property claim (each sweep's
  `head_before` is the commit before it; its gated digest is the blob the next
  commit shipped) with the enumeration explicitly bounded as a reading at a
  named revision, plus a note that the invariant is the shape. Applied to the
  PR description too, which names the log-file pattern and points at this plan
  rather than carrying a count of its own.
- 2026-09-28: **A gate log's recorded digest is a content hash, and the
  comparison must match it.** Verifying the chain by
  `git rev-parse <commit>:<path>` reports MISMATCH on every sound link, because
  that is a SHA-1 object id over a length-prefixed payload. Compare
  `git show <commit>:<path> | sha256sum`. The failure signature is diagnostic
  and was used as such: all links failing at once indicts the comparison, not
  the chain.
- 2026-09-28: **Anchor a measurement arm by a digest the run itself records, not
  by the filename it was saved under.** A `skylos` arm was cited by commit name
  when the tool's JSON contains no revision field, so the label was inherited
  from the archivist's filename. Re-ran both arms with the input digests logged
  beside the results; the plan now cites those digests. Generalized: a run
  whose output does not name its input is not evidence until something does.
- 2026-09-28: **Keep a class-qualified `Class.method` mention intact in prose.**
  `skylos` credits `_LineEventEmitter.emit_line` as live from the
  class-qualified mentions in this plan and the evidence report, and its rescue
  patterns do not match a bare `emit_line`. So a reflow that splits the
  qualifier across lines, or an edit that drops it, silently removes the credit
  and fails `SKY-U001`. The mentions are load-bearing text, not formatting.
- 2026-09-28: **Leave PR #433 as a draft until the user says otherwise, and do
  not count the CodeRabbit app's `pass` as review.** The app skips drafts, so
  its check reports `pass` with the detail "Review skipped: draft pull request"
  — a skip wearing a pass. The four review dispositions therefore rest on the
  CLI agent's JSON stream with no GitHub-side threads to cross-check, and
  taking the PR out of draft is a decision the plan flags for the user rather
  than takes itself.
- 2026-09-28: **A stale number in a milestone instruction is a live defect, not
  an audit record — and an amendment claim in a revision note must be checked
  against the diff, not trusted.** EP-M2's instruction still read "If it misses
  the **28%** threshold, record BLOCKED" while the 2026-09-27 revision note
  claimed the Milestones section *had* been amended to 30%. Both could not be
  true. `git show 75a777d9` settles it: that commit amended EP-M2's *narrative*
  paragraph ("meet 28%" → "meet the bar") and the Validation section, but never
  the instruction sentence, which `git log -S` shows untouched since
  `01ec41bd`. The distinction the 2026-09-28 docstring entry draws — live claim
  versus record of a past decision — applies, and this site is the former: the
  instruction is unqualified and undated, so a worker following it at this
  task's own measurement would record BLOCKED on a result that passes, because
  29.91% misses 28% and clears 30%. Corrected to 30% with an inline note
  recording what it said and why the revision missed it, which is the treatment
  V5 itself already uses for its own stale "27-of-100" rewrite. The quoted
  instruction at the 2026-09-27 Progress bullet stays as written: it is dated,
  attributed, and marked "Resolved later the same day", which is what an audit
  record looks like. The general lesson is the one the docstring entry already
  states, applied to a third site it had not enumerated: when a target moves,
  sweep for *instructions that cite it*, not only for claims about it.

### 2026-09-29: an independent review falsifies two of this plan's own rejection reasons

An independent review of this branch reproduced all six committed
classifications, recalculated the unprofiled medians, and confirmed the
29.9087% candidate median and the 26.28% callback-workload improvement. It then
found that this plan's constructor rationale rested on two claims that are
**false**, and that one permitted refinement was recorded as untried when it
had in fact been measured. The corrections are in the artefact, the report, and
the roadmap; the findings are recorded here because a wrong rejection reason is
worse than no reason: it forecloses a route a later reader would otherwise
re-open on evidence.

**Claim one, falsified: "a handwritten `__init__` re-breaks silently whenever a
field is added."** The descriptor variant's constructor is itself generated from
`dc.fields()`, and the 2026-09-27 entry above records exactly the check that
rules this out: rebuilt with an extra field, the signature grew to match,
`dc.replace` worked on the new field, and `asdict` keys stayed equal to the
field set. Drift is *answered by generation*, not merely asserted away. The
variant stays excluded — but on the grounds that remain true: maintaining a
hand-rolled generator, and the re-baseline it forces (a handwritten `__init__`
renders as `__init__ (cuprum/events.py:N)`, so the classifier's `<string>` rule
stops matching), are a real and ongoing cost. The plan previously stated both
of those correctly elsewhere; only the drift sentence was wrong.

**Claim two, falsified: `dataclasses.replace` bypasses constructor
invariants.** It does not. `replace` calls `ExecEvent(**{...})` — the ordinary
constructor — so `__init__` and any `__post_init__` run again. Verified on this
host (3.12.13) by instrumenting a `__post_init__` and by confirming that a
validating one still rejects a bad value routed through `replace`. The
practical consequence is that `replace` was never a route around the "single
place invariants are checked" reasoning, and it is not faster either, since it
performs the same `__init__` call plus a field copy. The decline of
constructor-bypass routes stands on other grounds; the sentence that listed
`replace` among the faster bypassing options did not.

**One refinement was recorded as unspent when it had been measured.** The
Tolerances section says both permitted refinements "were not spent" and that no
argument-passing variant was tried; the same document records, at the
"refinements are both already ruled out" entry, a measured `ExecEvent(**kw)`
2735 ns against 2431 ns positional. The two statements cannot both be true.
They are reconciled as: the *ruled-out* entry measured argument passing, the
*unspent* entry means neither refinement was needed to reach the criterion as
finally revised. That is a weaker claim than "not tried", and the text is
corrected to say what was actually done.

**The review's own candidate, recorded as unmeasured.** The best remaining
alternative it identifies is to specialize the observe-only path at preparation
time: when `on_line` is absent, `_compose_line_callbacks` could return
`emitter.emit_line` directly instead of wrapping it in a closure that re-tests
both `emitter` and `context.on_line` on every line. That is the branch's
measured workload — the benchmark's callback scenario uses `sh.observe` — and
it removes a Python call and two repeated invariant checks per line without
duplicating dispatch, changing task ownership, or sacrificing named constructor
arguments. **It is not accepted and not claimed as a win here.** It removes
denominator-only work, and this plan's own structural finding says what that
does: 100% of the numerator is the retained `ExecEvent.__init__`, so lowering D
while holding N **raises** the share. The specialization could therefore breach
a criterion this branch passes by 0.0586 points, by succeeding at exactly the
kind of change the plan endorses. It needs the full three-pair V5 protocol
before it can be judged, and the outcome is genuinely uncertain in direction of
magnitude, not sign. A separate microbenchmark the reviewer ran on this host
(MicroPython-free CPython 3.14.4, nine rotated rounds of 150 000 calls, real
synchronous dispatcher) priced two further variants: positional-required
arguments at 0.9571× the shipped emitter and captured-locals-named at 1.0143×.
Both are microbenchmarks of the emitter, not workload measurements, and neither
is V5 evidence; the positional variant's eleven positional arguments, two pairs
of them adjacent `None`s, are the legibility cost it would carry.

**What this changes about the plan's conclusion.** Nothing about the shipped
implementation or its verdict: the two falsified claims are rejection reasons
for roads not taken, and the review's candidate is explicitly unmeasured. What
it changes is the record's honesty about *why* those roads were not taken, and
it adds one live candidate that a future reader should price before assuming
the design space is closed.

### 2026-09-29: a completed-review finding on the spelling policy, and how it was falsified

CodeRabbit returned **CHANGES_REQUESTED** on `31d45cb7` with one actionable
finding: that the narrowed spelling exception at `typos.toml:83` is a hand edit
to a generated file, which the next generator run would discard, and that it
therefore belongs in `typos.local.toml`. (That exception is anchored on the
generator's own longer phrase, `` `var.iamge_id` instead of `var.image_id` ``,
so only that full phrase is masked; a bare mention of the identifier alone
would still trip the spelling gate.) The finding reads plausibly — `typos.toml`
carries a "Generated … do not edit by hand" banner, the commit touches only
that file, and the repository rule does say repository-specific exceptions
belong in the overlay.

**It is false, and the check that settles it is cheap.** Delete the line, run
the generator, and see whether it comes back:

```text
typos.toml with the entry removed          a414704b…
after `make spelling` (generator ran)     ecb670c6…   (entry restored at :83)
```

Restoring `typos.toml` from `origin/main` instead (`bc7c1418…`) and running the
generator yields `ecb670c6…` — the branch's exact committed file, narrowed
entry included. So the commit is not a hand edit at all: it is the output of
running the project's own generator against a newer shared dictionary, and
moving the entry to `typos.local.toml` would be *wrong* in two ways — it would
put generated content in the overlay, and the next regeneration would restore
the generated form anyway, leaving a duplicate.

**The generalizable point.** "Generated file, do not edit by hand" establishes
that a file's content *should* come from a generator; it does not establish
that an unfamiliar diff into it *did* come from a hand. Those are different
claims, and only the second is a defect. The distinguishing check is not
reading the banner but re-running the generator and diffing — the same
discipline this plan already applies to gate logs, where a recorded digest is
evidence only against the blob actually shipped. A reviewer enforcing a real
project rule from a diff alone will misfire exactly here, on the commit that
did the right thing by regenerating.

**Disposition.** The finding is disputed with the reproduction above rather
than applied. The commit stands unamended.

### 2026-09-29: the two oversized test modules are split to the 400-line rule

The independent review's fourth point was that two of this branch's own new
test modules breach the repository's 400-line rule:
`test_line_event_profile.py` at 626 lines and
`test_line_event_emission_properties.py` at 529. Both are new files added by
this branch, so the rule applies to them squarely. **Both were split**, and no
test was renamed, deleted, or added. Line counts below are `wc -l`:

| File                                        | Lines | Contains                                                     |
| ------------------------------------------- | ----- | ------------------------------------------------------------ |
| `test_line_event_profile.py`                | 117   | Scoring: limit boundaries, denominator scope, once-per-stack |
| `test_line_event_profile_callers.py`        | 227   | Caller attribution, drift detection, overlap resolution      |
| `test_line_event_profile_verdicts.py`       | 159   | Every route into the inconclusive verdict                    |
| `test_line_event_profile_parsing.py`        | 119   | Frame parsing, shipped rules, module split                   |
| `test_line_event_profile_support.py`        | 109   | Shared frame constants, capture builders, rules fixture      |
| `test_line_event_emission_properties.py`    | 259   | The four properties and the boundary examples                |
| `test_line_event_emission_support_props.py` | 302   | Generators, pinned clock, oracle-parity helpers              |

Splitting by *subject* rather than by line count keeps each module's docstring
honest: the scoring module now says what it scores, the callers module says why
caller proximity is the disambiguator, and the verdicts module says why an
unreadable capture must never score as zero. The alternative — trimming prose
to fit — would have removed exactly the reasoning these tests exist to carry.

**A correction to the framing, not to the work.** An earlier draft of this note
justified splitting only these two files on the grounds that the surrounding
`cuprum/unittests` population is "consistent-with-house-norm" and therefore not
this branch's business. That compares the wrong thing twice. There are **33
files over 400 lines across 285 in `cuprum/unittests/`** — up to 993 — so an
over-400 test module is nearer the house norm than an exception, and
"consistent-with-house-norm" cannot do the work the sentence asked of it. The
claim that survives measurement is narrower and sufficient: these two are
modules *this branch created*, so the rule binds them without needing any
opinion about the other 33. The remaining 33 are left alone as a pre-existing
repository-wide question, not as a precedent this branch endorses.

**Verified by inventory, not by count.** Collection was compared both ways
rather than asserted, because the "before" figure cannot be read off the
worktree once the split has happened:

- Current modules: **33** tests across the four profile modules, **21** across
  the two emission modules.
- The pre-split pair, materialized from `git show HEAD:` into a scratch
  directory and collected with the repository on `PYTHONPATH`: **54** — equal
  to 33 + 21, so the split neither lost nor duplicated a test.
- Sorted test-method names are byte-identical between the pre-split pair and
  their successors: **27** for the profile file, **8** for the emission file. A
  line-count fall only proves the files shrank; it does not prove the tests
  survived, so the check is the name set.

`pytest --collect-only` over the whole `cuprum/unittests/` scope reports
**2612**, which is the `make test` figure for that glob — note that `make test`
runs pytest once per `PYTEST_TARGETS` entry, so 2612 is one invocation and not
a whole-suite total.

One improvement rode along: `TestShippedRulesFile` built the same four-segment
path twice, with a function-local `import pathlib` in each test. Both now call
a single `_shipped_rules_path()` helper beside the other fixtures. That is the
one place the split is not a pure move.

`cuprum/unittests/` is not a package, so the new modules import their helpers
by full path, matching the existing `from cuprum.unittests.…` convention that
`test_line_event_emission_support.py` already established. `PYTEST_TARGETS`
wants `cuprum/unittests/test_*.py` as a glob, so all four new modules are
collected without any allow-list edit — the trap the plan records elsewhere for
targets named one by one does not apply here.

**The first gate run after the split came back red, on the split itself.** Four
of the new modules carried an extra blank line at EOF
(`too-many-newlines-at-end-of-file` for both `ruff check` and
`ruff format --check`), and `test_line_event_emission_support_props.py` imported
`ExecEvent` and `ExecId` at runtime although only annotations referenced them
(`typing-only-first-party-import`). Both are the failure mode this repository
already has a note for: a hand-written module split ships annotation-only
imports, and because `check-fmt` aborts at its first failing step and
`python-lint` at its first failing leaf, one edit hides everything behind it —
`make check-fmt` reached 1 of 3 steps and `make lint` 1 of 12 leaves, so
rustfmt, mdtablefix, `interrogate`, pylint, Whitaker, yamllint and actionlint
were all unobserved in that run and had to be re-established afterwards. The
imports now sit in the `typ.TYPE_CHECKING` block beside the module's other
annotation-only imports, which is safe here because nothing calls
`get_type_hints` on these annotations.

Worth stating plainly: `make test` was green on the pre-fix tree, so the split
was behaviourally correct the whole time. The red was purely lint and format.
That is exactly why the gate set is not optional even when the tests pass.

## Outcomes & retrospective

Planning identified a narrow implementation and an honest stop condition, and
the outcome vindicates both. EP-M1 is complete at `dbaba9ac`: the control
capture, classifier, characterization modules, and the contract evidence all
exist and are gated. EP-M2 then stopped **before writing any runtime code**,
because the pre-implementation feasibility measurement showed the hoist as
designed cannot reach the 10% gate, and that finding is the milestone's most
valuable output — it cost one measurement instead of an implementation, a
benchmark cycle, and a rejection. It stopped a second time, at `6d58d5ae`,
holding the completed implementation rather than shipping it against a bar it
had measurably missed. Both stops were required by §Tolerances, and both
produced a decision the plan could not have made on its own: the target moved
from 10% to 28% on a projection, and from 28% to 30% on the measurement.

The implementation itself is confined to one production module —
`cuprum/_line_callbacks.py`, +132/−13 — which is a narrower change surface than
the plan's four-module bound anticipated. The rest of the branch is
characterization, evidence, and documentation. The measured result is 34.2928%
→ 29.9087% (median of three matched pairs, candidate range 0.0423 points), with
the candidate 30.96% faster in profiled median wall time and 26.28% faster
unprofiled.

Two of the plan's substantive design claims were falsified by measurement and
recorded as corrections rather than quietly edited: the pipeline model of
execution identity (§V4) and the post-spawn ownership claim (§V3). Two further
numeric claims were corrected in the BLOCKED entry, a fifth in the committed
evidence artefact. The plan text has been wrong in ways only measurement could
reveal; that is the argument for the characterization-first structure, not
against it.

The gate evidence was **independently reproduced**, not merely re-asserted: a
scrutineer that was instructed to trust none of this plan's claims re-derived
the change surface, ran all six gates, captured exit statuses out of band, and
reported one red — an intermittent timing flake in
`test_idle_heartbeat_coordination.py`, a file the branch does not touch. One
correction is owed to the reader on that point: the red was first explained by
a clean bounded re-run at *lower load*, and that explanation is now known to be
false. The re-run passed at essentially the load that produced the failure
(17.53 against a failing 16–27), so load is a contributor rather than the
determinant, and the flake conclusion rests instead on the change surface and
on the recorded assertion text. The scrutineer's single most useful
contribution was unplanned: gating the tree gave a frozen input on which the
unbounded `actionlint` both passed and hung minutes apart, converting the
host-level-hang hypothesis from an argument into a measurement.

Implementation approval for a **revised** EP-M2 design was granted on
2026-09-27, and it was granted on the measurement rather than inferred from
this branch's green gates: the gates prove the characterization work is sound;
they do not by themselves show that any design option is acceptable.

**Closed out on 2026-09-28 at `3315c5c3`, after the pre-COMPLETE
reconciliation.** The discoveries were reconciled with every downstream
document: `docs/cuprum-design.md` §8.1.3 no longer says the plan is "BLOCKED at
21.00%" and now documents `_LineEventEmitter`'s scope and lifetime; the
developers' guide carries the reproduction convention, the N/D definition, the
metric's non-monotonicity stated as something to design around, and the
shared-Cargo-cache invariant; the users' guide carries the measured guidance
with the caveat that the gain is confined to line-callback workloads; and the
contents index gained both the evidence report and its data directory. Both
rejected performance options are retained with their measured ratios here and
in the evidence artefact, so a future reader can reverse or re-take each
decision on evidence rather than on recollection.

The closeout gate run then passed all eight gates at `3315c5c3` with the tree
clean before and after — `check-fmt`, `markdownlint` (chaining `spelling`),
`typecheck`, `lint`, `test` (`2549 passed, 63 skipped`; nextest `125 passed`),
`nixie`, and `test-act` (`24 passed`, `CUPRUM_REQUIRE_ACT=1` so a missing
runtime could not have skipped it). No sub-check was bounded or skipped, and
the known host `actionlint` deadlock did not reproduce. The run is valid as a
citation for `3315c5c3`: that SHA and the tree hash were identical before and
after every gate, so no gate observed a mutation mid-run. Log in
`/tmp/closeout-*-5-2-1-hoist-the-invariant-exec-event-and-event-details.out`.

**After the review dispositions, the suite was re-run at `d98fb5c9`** (the
revision carrying the CodeRabbit fixes) and passed every gate again —
`check-fmt`, `markdownlint` with `spelling`, `typecheck`, `lint` (interrogate
100.0%, pylint `10.00/10`), `test` (`2549 passed, 63 skipped`; nextest
`125 passed`), and `nixie` — with the tree clean before and after and nothing
skipped. That the `lint` log was not cut off is checkable rather than asserted:
it ends on `github-actions-lint`'s last recipe line,
`actionlint -config-file .github/actionlint.yaml` (`Makefile:384`), which
prints nothing on success, so the log reaching that line is what "no sub-check
was bounded" means here. The command ran unbounded; no `timeout` appears in the
log or the command that produced it. `make test-act` was not re-run, which is
sound rather than an omission: it drives GitHub Actions workflows and no
workflow file, Rust source, or production Python changed in the delta, whose
only Python edits are docstrings — verified by inspecting both hunks. The delta
from `3315c5c3` to `d98fb5c9` is four files: two documentation files (this plan
and the evidence directory's `README.md`) and two docstring-only test edits.
Logs in
`/tmp/final-*-5-2-1-hoist-the-invariant-exec-event-and-event-details.out`.

One caveat on the evidence, recorded rather than glossed: the CodeRabbit CLI
agent was the **only** review surface available, because the app skips draft
PRs and this one was still a draft when the dispositions were made. There are
therefore no GitHub-side review threads for them, and the four dispositions
rest on the agent's JSON stream rather than on a threads API that could be
cross-checked. Should the PR later leave draft, the app's findings will be a
*fresh* surface, not a re-derivation of the ones disposed here.

**Every commit above `d98fb5c9` is a documentation edit** — to this plan or to
its evidence report, never to production code, tests, or build configuration.
Each went in behind its own docs-scoped gate sweep, and the sweeps compose into
a single verified chain rather than a set of unrelated assertions: each
recorded the commit it started from as `head_before` and the SHA-256 of the
file it was about to gate, and in every case that digest is the digest of the
blob the next commit actually shipped. Checked by recomputing from the commits
rather than from the logs; as at `a4ae2482` the chain stood: `7cbc266a` ran at
`d98fb5c9` over the plan (gated `433bb89d`), `aa30ced6` at `7cbc266a` over the
report (gated `2d6e841c`), `a21b08a9` at `aa30ced6` (`acec114d`), `c83b12a3` at
`a21b08a9` (`d1e88169`), `05140b65` at `c83b12a3` (`cf276c88`), and `a4ae2482`
at `05140b65` over the plan (`7c01fcc0`). Each sweep ran `mdtablefix`,
`check-fmt`, `markdownlint`, and `nixie`; every gate exited 0 with the file
hash identical before and after, so no sweep observed a mutation mid-run. Logs
under `/tmp/` with the prefixes `plan-fix-`, `report-fix-`, `table10-`,
`shortfall-`, `range-note-`, and `chainnote-`. The list is a snapshot of the
chain at that commit, not a bound on it: any later documentation commit should
appear as one more link of the same shape, and the invariant that matters is
the shape — each sweep's `head_before` is the commit before it and its gated
digest is the blob the commit after it shipped.

That shape was re-checked mechanically at `e4a53306`, where the chain has grown
to ten links: `7cbc266a` (gated `433bb89d`), `aa30ced6` (`2d6e841c`), `a21b08a9`
(`acec114d`), `c83b12a3` (`d1e88169`), `05140b65` (`cf276c88`), `a4ae2482`
(`7c01fcc0`), `125c1bdd` (`52dad1f5`), `e3cb271c` (`d962d2e0`), `611f0f2d`
(`aa723ff8`), and `e4a53306` (`2dff1c41`) — with prefixes `chainbound2-`,
`skylosfix-`, `degrecord2-`, and `chain9b-` added for the last four. All ten
links MATCH, and the commit-to-sweep mapping is a bijection over
`d98fb5c9..HEAD`: no commit above the full-suite revision lacks a sweep, and
none is covered twice. Both facts were computed from the commits rather than
read out of the logs, which matters for the digest comparison in a way worth
stating plainly, because the obvious form of it is wrong: the recorded digest
is a **content** SHA-256 (`sha256sum` over the file), not a git object id.
Comparing it against `git rev-parse <commit>:<path>` — whose value is a SHA-1
over a length-prefixed `blob <n>\0` payload — reports MISMATCH on every link
while the chain is in fact sound. Compare against
`git show <commit>:<path> | sha256sum`. Two prefixes remain failed attempts and
are not part of the chain: `degrecord` aborted on the spelling gate, re-run as
`degrecord2`; and `chain9` never ran its gates at all, because its wrapper
passed `mdtablefix`'s flags through `make` rather than to the tool, so all four
exited 2 without executing. Neither has a bearing on the links above — a failed
sweep simply leaves no commit behind, which is why the bijection still holds.
The sweep that closes the last stale target reference adds a third shape worth
naming, because it decoys the obvious read-back: reaching its final content
took several attempts, and the earlier ones gated content that was then edited
again. Their shipping commits were then amended away, so those logs record
digests that no commit in the current history ships — matching plan text, not a
defect, and visible as such only to a reader who checks each recorded digest
against a commit rather than trusting the prefix. The committed content was
gated once, after the text settled, by the final sweep, whose logs are split
one per gate so no `grep` can read the wrong arm.
`git show <commit>:<path> | sha256sum` matches that sweep's recorded digest and
no other. The lesson generalizes the one the failed attempts already teach — a
prefix names a *session*, not a result, so a digest is only evidence when it is
read from the arm that gated the blob actually shipped, which is what that
comparison establishes independently of any log.

That chain is why the two full-suite runs are the only ones the branch needs,
and why neither is superseded by the documentation commits above them. The
reason is narrower than "docs cannot affect a suite": **`make lint` does read
Markdown.** Its `skylos` stage credits a symbol as live when a document names
it, which is why this branch's own `_LineEventEmitter.emit_line` carries
`documented_public_api` evidence — sourced from the class-qualified
`_LineEventEmitter.emit_line` mentions in this plan and in the evidence report.
So a documentation edit *can* move `lint`, and the four-Markdown-gate sweep is
not on its own enough to prove it did not.

The claim that it did not was tested rather than argued, because the failure
mode is invisible to the sweeps. Running the pinned skylos (`4.33.2`) over
`cuprum` on the documentation content `611f0f2d` shipped (plan `aa723ff8`,
report `cf276c88`) and again with these two documents reverted to `d98fb5c9`
(plan `452fd262`, report `b910858d`) gives byte-identical verdicts: the same 22
liveness-credited symbols, the same zero `unused_functions`. The two arms are
anchored by content digest rather than by commit label, because the skylos JSON
records no revision of its own — an earlier run of this experiment was filed
under a commit name its output never carried, and is not cited here.

As a control that the mechanism is live rather than dormant, the
class-qualified mentions were then degraded to bare
`LineEventEmitter.emit_line` in both files, whereupon
`_LineEventEmitter.emit_line` lost its credit and `unused_functions` became 1 —
an `SKY-U001` failure. The control was then taken one step further, because
"the four gates would still pass this" is easy to assume and was in fact false
on the first attempt: the raw `sed` edit also shortened the lines, so
`mdtablefix` and `check-fmt` failed on *rewrapping*, not on the semantic
change. Re-canonicalizing the degraded files with `mdtablefix --in-place` — the
same reflow `make fmt` applies — made all four gates pass (`mdtablefix`,
`check-fmt`, `markdownlint`, `nixie`, all exit 0) while the skylos failure
remained. That is the exact state the claim describes: a documentation change
that every Markdown gate accepts and `make lint` rejects.

So: the sweeps honestly report what they cover, the full-suite runs at
`d98fb5c9` are the last word on `lint` for the code, and the delta since is
verified separately by re-running skylos over both documentation revisions
rather than by assuming docs are out of scope. The degraded control's digests
(`2f4bd119…` for this plan, `37a09dc7…` for the report) are recorded here only
so the experiment is repeatable; the tree was restored to HEAD immediately
after, and `git diff HEAD` was empty.

The PR description states the same thing, so a reader of the PR does not have
to open this plan to know the head is ahead of the last full-suite run.

### What was sacrificed for legibility and maintainability

The user asked, when approving the 30% target, for the legibility and
maintainability trade-offs to be set out explicitly. Three kinds of sacrifice
were made. They are recorded here because the 30% figure is only honest if a
reader can see what was given up to reach it, and because each was a deliberate
choice made against the alternative of a faster number.

**Performance given up to keep the observation contract readable (the large
one).** The numerator cannot fall further without deleting the per-line
`ExecEvent` construction, and 100% of what remains sits inside the generated
`ExecEvent.__init__`. Two levers were measured and both were declined:

- A handwritten descriptor `__init__` at **0.68×** the stock constructor
  (1278.3 vs 1890.3 ns/ctor), **modelled** as sufficient for **22.42%** — under
  even the original 28% bar. Declined because it replaces the generated
  constructor with hand-maintained code that must reproduce field order,
  defaults, `__eq__`, `__repr__`, and `dataclasses.fields()` introspection
  exactly — an ongoing generator to maintain on a public 27-field dataclass.
  The drift objection specifically does not hold: that constructor is itself
  generated from `dc.fields()` and was verified against an added field, so a
  new field is caught rather than missed. The trade is a permanent code surface
  on a public API for a percentage that the user has since granted by moving
  the target; 22.42% is a modelled share, not a measured workload result, and
  would need the full V5 protocol before it could be cited.
- Reopening `ExecEvent`'s `frozen=True` at **0.18×** (341.6 ns/ctor) — the
  largest single lever available, because `frozen=True` costs 27
  `object.__setattr__` calls per construction and dominates the constructor.
  Declined as a public-API semantics change (hashability and immutability for
  every consumer) that a threshold derived from a projection could not justify.

Both are recorded with their measured ratios in
`docs/tee-hotpath-line-event-emission-5-2-1.md`, so the decision is reversible
by a future reader on evidence rather than on recollection.

**Performance given up to keep error handling and task ownership in one
place.** The hoist kept the dispatch path — `_emit_event` and
`_emit_exec_event`, with result-based awaitable detection and the retention of
already-scheduled tasks when a later hook raises — instead of inlining the hook
loop into the per-line emitter. The inlined form would have removed one bound
method call per event; it was not done because `_ExecEventEmissionError`
handling and pending-task ownership would then have had two implementations,
and the failure contract is the part of this code that is hardest to test
comprehensively. The same reasoning kept the ordinary `ExecEvent` constructor
in place of `dataclasses.replace`, `object.__new__`, or slot mutation, each of
which is faster and each of which gives up the constructor as the single place
invariants are checked.

**Structural work done for legibility that cost extra rounds.** Four changes
were made because the code would otherwise be worse, and each cost a gate cycle
or a review round:

- The 654-line test module was split by subject rather than left as one file, to
  stay under the project's 400-line ceiling. The split surfaced a monkeypatch
  seam problem (re-exports keep import paths alive; monkeypatch targets need
  re-pointing) and a round of unused-import lint failures that a single large
  file would not have produced.
- The classifier was split out of the command-line front end into
  `benchmarks/_line_event_profile_model.py`,
  `benchmarks/_line_event_profile_classifier.py`, and
  `benchmarks/summarize_line_event_profile.py`, again for the 400-line ceiling.
  The boundary is real — the classifier reads no files and parses no text — but
  the split added a module the plan had not budgeted.
- Rule resolution was settled by **caller proximity** rather than by declaration
  order, so that per-rule attribution does not depend on how the rules file
  happens to be sorted. Proximity is more code than a first-match wins loop,
  and it is what keeps a broadly-called rule from silently absorbing a narrower
  one's frames.
- Three of the plan's own claims were falsified by measurement and recorded as
  corrections in place, rather than edited away: the pipeline model of
  execution identity (§V4), the post-spawn ownership claim (§V3), and — in the
  artefact — a worktree-selection reason whose superficially-similar true
  statement differs from the false one by a single path index.

**One cost that was not a trade-off, and is not presented as one.** The
denominator fell 31.0% where the projection assumed 13.7%, because the hoist
removed more of the surrounding work than predicted. That is what left the
surviving numerator at a higher share, and it is the mechanism by which a
faster implementation produced a worse score. It is recorded as a defect in the
metric, not as a sacrifice: nothing was given up to obtain it.

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
- R3, construction share at most 30%: EP-M2 feasibility and EP-M3 acceptance,
  evidenced by V5's committed samples and reproducible classification. (Revised
  10% → 28% before implementation and 28% → 30% after it, both on 2026-09-27
  with user approval; see the two threshold-revision entries.)
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

**As landed 2026-09-27 (see Surprises & discoveries).** Two deliberate
departures from the text above, both made during implementation and both to be
preferred over it:

- The phase is *not* declared as its own `Literal["stdout", "stderr"]` field.
  `LineStreamName` already *is* that literal, so a separate phase field would
  be a second name for the same thing; the emitter stores the stream name and
  uses it as both. The observable payload is identical.
- `_compose_line_callbacks` does not "return it in place of the current
  closure". It keeps returning a closure, which binds the emitter built by the
  new `_line_event_emitter` factory. The closure is still required: it carries
  the caller's `on_line` fan-out and `_stamp_line` for the `LineEvent` channel,
  which is a different payload from the observe event and is not part of the
  hoist. Returning the emitter itself would have deleted that channel.

The emitter reaches the observation's bound `_emit_event` rather than a copy of
its body; see the landing entry for why preserving task ownership requires it.

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

### V5: measured construction share is at most 30%

The threshold was revised twice, both times with user approval: 10% → 28%
before implementation (on a projection) and 28% → 30% after it (on the
measurement of 29.9087%). The criterion was written as at most 28% and is now
at most 30%; the derivation is in the two threshold-revision discovery entries.
The verification machinery below is unchanged and was not re-tuned to meet
either number.

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
all-constructor synthetic profile must fail the gate; and the boundary must be
pinned one sample either side of the limit at its own value. (As written this
said "a 27-of-100 input must pass and 29-of-100 must fail", which was the
boundary for the original 10% bar and went stale at the first revision. The
tests were written against the constant rather than the literals, so the
boundary tracked both revisions without editing: 9/10 for 10%, 27/29 for 28%,
29/31 for 30%.) Actual control frames must match non-empty categories. Keep the
helper under the scripting standards and avoid adding runtime dependencies.

The proposed command accepts one folded-stack path, `--rules` for an explicit
JSON classification file, and `--output` for its JSON result. Emit weighted
`parent_samples`, `consume_samples`, `construction_samples`, both percentages,
`matched_frames`, `unresolved_frames`, and `status`. Exit 0 for a valid share
at most 30%, 1 for a valid share above 30%, and 2 for malformed, insufficient,
or unresolved input. A control run may intentionally exit 1; retain its result.
Regression timing is assessed separately from this single-capture command.

Collect three matched control/candidate profile pairs on the full wrap-76
fixture, each with one worker repeat. Require every valid candidate run to be
at most 30%, publish sample counts and dispersion, and require D of at least
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
initialization uses `object.__setattr__`. `dataclasses.replace` does **not**
bypass the constructor: it calls
`ExecEvent(**{f.name: getattr(self, f.name) …})`, so `__init__` and any
`__post_init__` run again, verified on this host (3.12.13 and 3.14.4) by
instrumenting a `__post_init__` and by confirming that a validating
`__post_init__` still rejects a bad value passed through `replace`. It is
therefore never a route around construction invariants, and this plan does not
rest on it being one. What the invalidation rests on is narrower: a cached
template and renamed construction establish no speedup, and `replace` is not
itself a faster constructor, since it performs the same `__init__` call plus
the field copy. Reopening construction is a public-API decision this plan
declines; it is not declined on the false premise that `replace` skips
validation.

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
the representative profile gate before claiming R3. If it misses the **30%**
threshold, record BLOCKED and present the measured limitation for design
revision. (This sentence read 28% until 2026-09-28: that was the live bar when
EP-M2 ran, and the 2026-09-27 revision updated the paragraph below and the
Validation section but not this line. Left alone it inverts the verdict at this
task's own measurement, since 29.91% misses 28% and clears 30%.) Correctness
alone does not discharge R3. A successful plateau contains one production
factory, unchanged dispatch, passing gates, and reproducible profile evidence;
commit it as one atomic functional change. Recovery is an ordinary reviewed
revert, not a force reset of unrelated work.

The feasibility stop condition was reached on 2026-09-27 against the original
10% threshold, and the threshold was then revised with user approval rather
than the design being re-scoped. EP-M2 therefore resumes with its scope
**unchanged**: hoist the per-line `_EventDetails` and argv construction, keep
the per-line `ExecEvent`, and meet the bar. Do not widen the hoist to chase the
old number; that route breaks V2/V4 and was explicitly declined. The same
instruction applied to the second stop: the completed hoist measured 29.91%,
the plan was set to BLOCKED rather than re-scoped, and the bar moved to 30% on
the measurement. **Neither stop was resolved by widening the implementation.**

**Gate status at HEAD `42b19f85` (2026-09-27).** `make lint` is green: exit 0
in 53 s with **all 13 sub-checks reached and none never-reached**, so the chain
that had been unobserved since `01ec41bd` is now observed end to end. Both
defects it had been masking are cleared — `pylint` walked the tree at
`10.00/10` with no `C0302` (the 406-line module is now 132 lines plus a
311-line engine), and the `handwritten` spelling finding is gone. `make test`
(7 pytest groups plus nextest `125 passed`), `typecheck`, `check-fmt`,
`markdownlint`, and `nixie` all passed against `54fb5c8f`, the commit before
the two fixes; they are **not yet re-run against `42b19f85`**, which moved
production code after that pass. Re-run them before any CodeRabbit request,
since a commit after a gate run invalidates that run as a citation.

Two caveats recorded with the lint result rather than glossed. First, five
sub-checks (`ambrleaks`, `skylos`, `yamllint`, `actionlint`, and `spelling`'s
silent-success path) are attested by exit status alone — the tools print
nothing on success, so "no output" is a pass by exit code, not positive
evidence. Second, `skylos`'s green is **scoped away from this branch's new
file**: `SKYLOS_PRODUCTION_TARGETS ?= cuprum`, so `benchmarks/` is outside its
scan root and its pass says nothing about the new classifier module.

The `actionlint` hang did not reproduce on either run; the parked processes
from earlier sessions are still in `futex_wait_queue` with 0.00 s CPU and no
`shellcheck` child. Because this branch touches no path under `.github/`
(confirmed: empty `git diff --stat origin/main...HEAD -- .github/`), actionlint
has nothing branch-attributable to report in either direction.

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
three candidate captures must be at most 30%, controls must use the same
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

2026-09-27: EP-M1 and EP-M2 completed; the V5 threshold was revised twice with
user approval (10% → 28% on a projection, then 28% → 30% on the measurement of
29.9087% across three matched pairs). R1–R4 are now met and the plan is
unblocked. Added the discovery "The 30% revision, and why the margin is thin",
a "What was sacrificed for legibility and maintainability" subsection under
Outcomes & retrospective, and two Decision-log entries; amended the Status,
Tolerances, Risks, Progress, Conformance basis, V5, Milestones, and Validation
sections to the 30% target. The two earlier threshold sentences and every
recorded miss remain in place as history — the revisions are recorded as
changes of target, not as corrections of measurement. EP-M3's closeout is the
remaining work.

2026-09-28: EP-M3 closeout completed at `3315c5c3`; CodeRabbit review
dispositions landed at `d98fb5c9` with the full suite re-run there; and ten
documentation commits followed, each behind its own docs-scoped sweep. Added
the Progress entries for that phase, two discoveries (`make lint` reads
Markdown, so a docs-only edit is not out of scope; and a content SHA-256 is not
a git object id), and five Decision-log entries. Amended the Status block to
name both full-suite revisions rather than only the closeout, and to record the
hosted CI state with the draft-PR caveat attached. Corrected the chain record
from nine links to ten and added the `chain9b`/`chain9` distinction. No
production, test, or build change was made in this revision: the sole
production file, `cuprum/_line_callbacks.py`, is still `+119/−13` against the
merge base `991dee64`, and every commit above `d98fb5c9` touches documentation
only.

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
