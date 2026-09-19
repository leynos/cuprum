# Hoist invariant execution-event fields (5.2.1)

Status: DRAFT — awaiting explicit approval before implementation.

This ExecPlan is a living execution plan. Keep Constraints, Tolerances, Risks,
Progress, Surprises & discoveries, Decision log, Outcomes & retrospective,
Conformance basis, and Verification plan current throughout implementation.
This pull request publishes planning documents only.

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
- [ ] Obtain explicit user approval before starting EP-M1.
- [ ] EP-M1: establish current control and contract characterization.
- [ ] EP-M2: implement and validate the bounded stream-factory optimization.
- [ ] EP-M3: commit representative profiler evidence, documentation, and
  completion of roadmap item 5.2.1 after all acceptance conditions pass.

## Surprises & discoveries

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

Add one private `_StageObservation.make_line_emitter` method, taking a
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

Add focused cases alongside V1, reusing existing asynchronous fixtures. Test
synchronous hooks, async hooks, synchronous callables returning awaitables, and
multiple ordered hooks. Retain events across an asynchronous yield and check
earlier lines and timestamps after later events have been emitted. A later hook
raising or cancelling must leave the previously scheduled tasks in the
observation's pending list; they must settle during existing cleanup. A clock
failure must occur before dispatch and add no tasks. Inject factory preparation
failure after spawn and prove the existing owner reaps the child. Inject this
in both command and pipeline paths; all existing reader and observer tasks must
settle, and the pipeline must also clean up earlier stages. The tests must fail
if the new path bypasses `_emit_event` or discards the scheduled prefix. Keep
unrelated exception-policy changes out of scope.

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
