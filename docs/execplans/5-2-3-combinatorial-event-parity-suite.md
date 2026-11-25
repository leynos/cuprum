# 5.2.3 Add a combinatorial event-parity suite

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT (awaiting expert review, then user approval)

Branch: `5-2-3-combinatorial-event-parity-suite`

Roadmap item: 5.2.3 in `docs/roadmap.md` (phase 5, "Reclaim the pure-Python
consume hot path"; step 5.2, "Make per-line event emission cheap for
line-callback workloads").

## Purpose / big picture

Cuprum runs commands and reports what happened to _observe hooks_. An observe
hook is a callable registered with `sh.observe(hook)`. It receives an
`ExecEvent`, a frozen record with a `phase` such as `plan`, `start`, `stdout`,
`stderr`, or `exit`. A `stdout` or `stderr` event carries one output line; this
plan calls it a _line event_. A hook may be synchronous or return an awaitable,
which Cuprum schedules as a task and awaits before the run returns.

Two completed roadmap items made line events cheaper. Item 5.2.1 computes the
fields that cannot change during a stream once, in `_LineEventEmitter`
(`cuprum/_line_event_emitter.py`), and fills in only the line and the timestamp
per line. Item 5.2.2 stopped asking `inspect.isawaitable` about every hook
result in `_emit_observe_hook` (`cuprum/_observability.py`). Both promised that
nothing observable changed. Each was checked in its own corner; nothing checks
the promise across the combinations a user can choose.

Consider this caller:

```python
with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
    cmd.run_sync(
        output=RunOutputOptions(capture=True, echo=True, on_line=callback),
        context=ExecutionContext(cwd=workdir, env={"PARITY": "yes"}),
    )
```

The hook may be synchronous or asynchronous. `capture`, `echo`, and `on_line`
may each be on or off. Today a defect that set `cwd=None` or `env=None` on
every line event would pass the tests that target line emission: the planning
probe applied exactly those edits and the focused suite of 161 tests stayed
green (see `Surprises & discoveries`).

After this change:

1. `make test` runs a matrix of 64 cells, one real child process (or two-stage
   pipeline) per cell: four hook kinds, by eight output modes, by two subjects.
2. Each cell's event payloads are compared with a committed record captured from
   the real source tree _before_ item 5.2.1, so "equivalent before and after"
   is measured rather than argued.
3. Each cell is also checked against a specification of the payload — programme,
   argv, cwd, env, pid, line, and timestamp semantics — so a failure names the
   field and the event that broke.
4. Deliberate faults prove that each check can fail.

No production behaviour, public API, or user-visible output changes. A failing
cell prints a short report naming the cell, stage, phase, event index, field,
expected value, and actual value. That report is the only new "interface".

## Constraints

Violating one of these requires escalation, not a workaround.

- C1. No production source under `cuprum/` outside `cuprum/unittests/` may
  change. If a cell exposes a real defect, stop and follow `Tolerances`.
- C2. No Rust, dependency, packaging, CI workflow, or benchmark change. Phase 5
  is Rust-free by design (`docs/roadmap.md`, phase 5 "Idea").
- C3. The committed baseline is historical evidence. It must be captured from
  commit `7b86b904b99140c041c28a6258efd4fb4c8d6186` (the parent of `71aaf3ebe`,
  which landed 5.2.1). It must never be regenerated from a tree that contains
  either optimization, and no `--snapshot-update` run may rewrite it (Decision
  D2).
- C4. Existing assertions stay as strong as they are. Existing tests may be
  strengthened (EP-M1), never weakened or deleted.
- C5. Reuse before adding. `_make_observation`, `_fields`, the `_reader` and
  `_config` drain doubles, `python_catalogue`, and the 5.2.2 dispatch oracle
  already exist; `Interfaces and dependencies` says which are reused and why
  any is not. The 5.2.2 dispatch specification stays the single authority for
  _dispatcher_ behaviour; this suite must not restate it.
- C6. Modules under `tests/helpers/` stay at or below 400 lines: that directory
  is inside the Pylint line cap. Target 300 lines per new module everywhere.
- C7. Assertions live in `test_*.py` modules. Helpers return values describing
  violations and never use the `assert` statement.
- C8. New test modules must match an existing `PYTEST_TARGETS` glob in the
  `Makefile`: `cuprum/unittests/test_*.py` and `tests/behaviour/test_*.py` do;
  a new directory directly under `tests/` would not, and no guard would notice.
- C9. Tests must not depend on sleeps, real-time thresholds, or the relative
  order of stdout and stderr, which Cuprum does not guarantee.
- C10. Python 3.12 is the oldest supported interpreter; CI runs 3.12 and 3.14.
- C11. All gates in `AGENTS.md` pass before each commit. Prose, comments, and
  docstrings use en-GB-oxendict spelling.
- C12. Completed ExecPlans (`docs/execplans/5-2-1-*.md`,
  `docs/execplans/5-2-2-*.md`, and every other completed plan) are historical
  and must not be edited.
- C13. Implementation begins only after the user approves this plan.

## Tolerances (exception triggers)

Comparison base: the merge base of the branch with `origin/main`. All ExecPlan
files, including this one, are excluded from every count. Limits are net lines
(additions minus deletions), counted with:

```bash
BASE=$(git merge-base HEAD origin/main)
git diff --numstat "$BASE" -- . ':!docs/execplans' |
  awk '{ net += $1 - $2 } END { print NR " files, " net " net lines" }'
```

Restrict the pathspec for each category:

- Code (`'*.py' '*.feature'`): at most 2,800 net lines.
- Configuration (`pyproject.toml typos.local.toml typos.toml Makefile`): at most
  20 net lines.
- Captured data (`tests/fixtures/event_parity '*.ambr'`): at most 700 net lines.
  This is generated evidence, budgeted apart so it cannot hide code growth.
- Documentation (`docs CHANGELOG.md`, excluding `docs/execplans`): at most 320
  net lines.
- Files: at most 30 non-ExecPlan files.

Each category is enforced alone; spare capacity in one does not offset another.
The survey behind these figures is in `Context and orientation`.

Other triggers:

- Divergence: if any cell differs from the baseline or the specification on
  unmodified production code, stop. Record the cell, the field, both values,
  and whether `4419c7c6` (after 5.2.1, before 5.2.2) agrees with either side.
  Do not edit the baseline, narrow the projection, or add an exemption.
- Baseline reproduction: if two captures of `7b86b904` differ in any byte, stop.
- Flakiness: if any cell fails once in 20 consecutive runs of the matrix modules
  on an otherwise passing tree, stop and find the cause. Do not add a retry.
- Run time: if the new modules add more than 60 seconds to a serial
  (`PYTEST_WORKERS=0`) `make test-python` on the development host, or any
  single test exceeds 10 seconds, stop. The planning probe ran 64 cells in
  1.1–1.9 s.
- Vacuity: if any seeded fault is accepted, or is rejected for a field other
  than the one it breaks, stop.
- Interface or dependency: any need for one stops the work.
- Iterations: stop after three failed attempts to make one red test green.
- Ambiguity: stop if two readings of the matrix would change which cells exist.

## Risks

- R1. The baseline tree differs from HEAD by far more than the two
  optimizations, so an unrelated, deliberate payload change could look like a
  regression. Severity: medium. Likelihood: low today — the probe found all 64
  cells equal. Mitigation: the baseline projection holds only the fields the
  roadmap names plus stage, phase, and order (Decision D3). `settled` events,
  which did not exist then, are outside it and are checked by the specification
  instead.
- R2. The capture could silently import HEAD's `cuprum`, making the baseline a
  copy of the code under test. Severity: high. Likelihood: medium, because
  `python -m` puts the working directory ahead of `PYTHONPATH`. Mitigation: the
  capture command uses `python -P` and refuses to write unless
  `cuprum.__file__` lies under the tree it was told to expect, and unless the
  module `cuprum._line_event_emitter` is absent from that tree.
- R3. Scheduling nondeterminism makes cells flaky. Severity: high. Likelihood:
  low. Mitigation: events are compared per stage and per phase only, each hook
  has its own recorder, clocks are injected, and no cell sleeps. The probe
  produced byte-identical traces in 12 captures, including ten pinned to one
  processor on Python 3.12 and 3.14.
- R4. A normalization masks a defect, for example by replacing every pid with
  the same marker. Severity: high. Likelihood: medium. Mitigation:
  normalization is _relational_. A pid becomes `<self>` only when it equals the
  pid that stage itself printed; any other value is rendered literally and
  fails.
- R5. The matrix passes because a mode never took effect. Severity: medium.
  Likelihood: medium. Mitigation: every cell asserts a _mode witness_ —
  captured text present exactly when capture is on, sink text present exactly
  when echo is on, and callback deliveries exactly when the callback is on.
- R6. A future `ExecEvent` field is populated by `_StageObservation.emit` but
  forgotten in `_LineEventEmitter`. This is the likeliest real regression.
  Mitigation: every field must be classified, and every invariant field of a
  line event must equal the same field of that stage's `start` event.
- R7. A child whose interpreter is a launcher stub would print a pid that is not
  the spawned pid (Windows virtual environments do this). Likelihood: low; the
  Python suite runs only on Linux in CI and on POSIX hosts locally. Mitigation:
  the workload builds its command from `sys.executable`, as existing tests do,
  and the matrix modules skip on Windows with that reason.
- R8. Adding about 170 child processes to a loaded CI host triggers the 30 s
  per-test timeout. Likelihood: low. Mitigation: one child per test item, and
  no test loops over the matrix.

## Progress

- [x] (2026-11-25 14:30Z) Branch created; governing documents, production code,
  existing tests, and 5.2.1/5.2.2 history read; Wyvern reconnaissance complete.
- [x] (2026-11-25 15:25Z) Feasibility probes run; results recorded below.
- [x] (2026-11-25 15:40Z) Initial draft completed.
- [ ] Expert review complete and findings reconciled.
- [ ] Plan approved by the user.
- [ ] `EP-M1` Line-event parity stops comparing `None` with `None`.
- [ ] `EP-M2` The pre-optimization baseline is captured and every cell meets it.
- [ ] `EP-M3` Every cell meets the specification and every fault is rejected.
- [ ] `EP-M4` Generated workloads keep the same payloads.
- [ ] `EP-M5` The behavioural feature and backend slice pass.
- [ ] `EP-M6` Documentation and roadmap are reconciled; all gates pass.

## Surprises & discoveries

- Observation: the existing parity tests compare five invariant fields only as
  `None == None`. Evidence: `_make_observation` and `_observation`
  (`cuprum/unittests/test_line_event_emission_support.py`,
  `..._support_props.py`) set `cwd=None` and `env_overlay=None` and leave
  `env_mode`, `resolved_path`, and `capture_mode` at their `None` defaults. In
  a scratch copy of `c966dbf14`, editing `_LineEventEmitter.emit_line` to pass
  `cwd=None`, `env=None`, `env_mode=None`, `resolved_path=None`, or
  `capture_mode=None` left all 161 focused tests green. Impact: EP-M1 exists,
  and those five edits are this plan's red evidence. Two of them, `cwd` and
  `env`, are fields the roadmap item names.
- Observation: the true "before" code is runnable. Evidence:
  `git archive 7b86b904 cuprum` runs under the current interpreter with no
  native extension, and its public API already accepts `capture`, `echo`,
  `on_line`, `cwd`, `env`, and `tags`. Impact: the baseline is captured, not
  reconstructed (D2).
- Observation: at the probe's projection, `7b86b904`, `4419c7c6`, and HEAD
  produce identical traces for all 64 cells. Evidence: 12 captures with one
  SHA-256, see `Artefacts and notes`. Impact: no production defect is expected.
- Observation: two hooks sharing one recorder made traces nondeterministic.
  Evidence: only the `mixed` cells differed until each hook had its own list.
  Impact: the runner gives every hook its own recorder.
- Observation: the wall clock is read once per event _plus_ once per spawned
  child. Evidence: a counting clock saw 12 reads for 11 events in a single
  command and 19 for 17 in a pipeline; the extra reads are
  `wall_clock_started_at` in `cuprum/_subprocess_execution.py` and
  `cuprum/_pipeline_spawn.py`, which set the result's `started_at`. The same
  reads exist in `7b86b904`. Impact: the clock invariant is stated with that
  term rather than as "one read per event".
- Observation: with capture, echo, and `on_line` all off, streams are bound to
  `/dev/null` and no line event exists. Impact: the silent mode is a real cell
  with its own assertion, not a skipped one.
- Observation: the profiling harness's `--line-callbacks` flag installs an
  _observe hook_, not `on_line`. Impact: see D4 for how the axis is read.

## Decision log

- D1. Decision: the matrix is the full Cartesian product of hook kind
  (`sync`, `async`, `suspending`, `mixed`), `capture`, `echo`, and `on_line`
  (each on or off), and subject (`command`, `pipeline`): 4 × 8 × 2 = 64 cells.
  Rationale: the roadmap says "every combination". When every combination of
  the chosen values is cheap, an exhaustive product needs no covering-array
  argument about interaction strength, and the probe cost about 20 ms per cell.
  `async` completes without suspending; `suspending` yields first, so its
  record proves the run waited for the task; `mixed` registers one synchronous
  and one suspending hook together. `pipeline` is a subject because pipelines
  build their observations elsewhere (`cuprum/_pipeline_observation.py`).
  Rejected: pairwise sampling, and adding `run_concurrent`, `SafeCmd.lines()`,
  byte mode, and per-stream overrides as axes; those reuse the same emitter and
  dispatcher, and `Verification plan` lists them as residual gaps. Date/Author:
  2026-11-25, planning agent.
- D2. Decision: "before" is a committed JSON record captured from the real
  pre-5.2.1 tree, compared by plain equality. Rationale: a frozen copy of the
  old functions inside the test tree would be maintained by the same people as
  the new ones and would drift; 5.2.2's plan rejected that shape (its D4). A
  syrupy snapshot would be rewritten by any contributor running
  `--snapshot-update`, which updates snapshots to match current behaviour and
  so would destroy the evidence. Rejected: frozen copies; syrupy for the
  baseline; checking out the old tree in CI, which needs full history and
  network. Retirement: when a payload field is _deliberately_ changed, the
  change's own ADR or design entry records it and the baseline comparison for
  that field is removed in the same commit. The record is never re-captured.
  Date/Author: 2026-11-25, planning agent.
- D3. Decision: the baseline projection holds `stage`, `phase`, per-phase order,
  `program`, `argv`, `cwd`, `env`, `pid`, `line`, and a timestamp judgement,
  for phases `plan`, `start`, `stdout`, `stderr`, and `exit`. It excludes tags
  and fields added later. Rationale: these are the roadmap's named fields.
  `tags` and every later field are covered by the in-run specification, so a
  deliberate new tag does not falsify history. Date/Author: 2026-11-25,
  planning agent.
- D4. Decision: "line-callback mode" means `RunOutputOptions.on_line`.
  Rationale: the observe hook is already the first axis, so reading the phrase
  as "observe hook present" would make the axis redundant. `on_line` is the
  other consumer composed into the same per-line callback
  (`_compose_line_callbacks`). Date/Author: 2026-11-25, planning agent.
- D5. Decision: the in-run oracle for invariant fields is the stage's own
  `start` event. Rationale: `start` is built by the generic
  `_StageObservation.emit`, which 5.2.1 did not change, so no patched reference
  path is needed, and a field added later is covered without editing the test.
  Because `emit` and the emitter could still drift together, the roadmap-named
  fields are also compared with literals the workload knows. Date/Author:
  2026-11-25, planning agent.
- D6. Decision: no Rust, `rstest`, `proptest`, `insta`, Kani, Verus, or
  CrossHair work. Rationale: the change adds no production logic, axiom, or
  Rust code. The obligations concern CPython object identity, asyncio
  scheduling, and child processes, which a solver cannot explore; the finite
  part is enumerated exhaustively instead, and the open-ended part uses
  Hypothesis. Items 5.2.1 and 5.2.2 reached the same conclusion (5.2.2's D6).
  Date/Author: 2026-11-25, planning agent.
- D7. Decision: syrupy is used for one thing, the rendered violation report.
  Rationale: that text is what a person reads when a cell fails, it has several
  variants, and it is deterministic because its inputs are constructed.
  Date/Author: 2026-11-25, planning agent.
- D8. Decision: no ADR. Rationale: the suite changes no architecture, interface,
  or policy that outlives it; D2's retirement rule is recorded in
  `docs/cuprum-design.md` §8.1.3 and the developers' guide. Date/Author:
  2026-11-25, planning agent.

## Outcomes & retrospective

To be completed during implementation.

## Context and orientation

Facts in this section were read at `c966dbf14` (`main`, 2026-11-25). Relocate a
symbol with `git grep -n "def <name>\|class <name>" -- cuprum tests`.

### How a line event is made

`SafeCmd.run_sync` and `Pipeline.run_sync` create one `_StageObservation`
(`cuprum/_pipeline_types.py`) per child: `cuprum/_command_internals.py` for a
command and `cuprum/_pipeline_observation.py` for each pipeline stage. Both bind
`wall_clock=time.time` and `monotonic_clock=time.perf_counter` from their own
module-level `time` name, so a test replaces that name to inject a clock. A
pipeline emits `plan` for every stage first. All stages share one
`pending_tasks` list; each has its own `exec_id`.

After spawn, `_compose_observation_line_callback` (`cuprum/_line_callbacks.py`)
builds one callback per observed stream. Commands reach it through
`_create_stream_callback` (`cuprum/_subprocess_streams.py`); pipelines through
`_create_stage_capture_tasks` (`cuprum/_pipeline_stage_streams.py`), which
observes stderr for every stage and stdout only for the last. The factory
`_line_event_emitter` returns a `_LineEventEmitter`, or `None` with no observe
hook. `_compose_line_callbacks` looks that factory up as a module global when
it runs, which is the seam the seeded faults use.

For each decoded line the callback calls `emitter.emit_line(line)`, then
`on_line`. `emit_line` reads the wall clock once, builds one `ExecEvent`, and
passes it to `_StageObservation._emit_event`, which calls `_emit_exec_event`
and retains any scheduled tasks. `_emit_observe_hook` ignores a `None` result,
schedules a native coroutine, and asks `inspect.isawaitable` only about other
shapes. `run_sync` awaits every retained task before it returns. Above 256
unfinished tasks line emission waits; every cell here stays far below that.

Non-line events (`plan`, `start`, `exit`, `settled`) go through
`_StageObservation.emit`, which takes an `_EventDetails`. Before 5.2.1 line
events took the same route:

```python
observation.emit(context.stream, _event_details(pid=context.pid, line=line))
```

A stream is piped and read only when something consumes it: capture, echo, idle
reporting, or `on_line`. Observe hooks alone do not cause a stream to be read.
The Rust extension never consumes a line-observed stream;
`CUPRUM_STREAM_BACKEND` affects only the pump between pipeline stages.

### What the events mean

`docs/cuprum-design.md` §8.1.3 and the users' guide section "Structured
execution events" define the contract: `program` is the catalogued programme;
`argv` starts with the programme; `cwd` and `env` are the execution's own;
`pid` is `None` on `plan` and the child's pid afterwards; `line` has its
terminator removed and a final unterminated line is still emitted; `timestamp`
is wall-clock seconds read when the event is emitted, one fresh reading per
event; order is guaranteed within a stream and not across streams.

### What already exists

| Existing evidence                                                       | What it covers                                                            | What it leaves open                                         |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `cuprum/unittests/test_line_event_emission_parity.py`, `..._properties` | line event equals generic `emit`, in process, stand-in command            | five invariant fields are `None`; synchronous recorder only |
| `cuprum/unittests/test_line_event_emission_hook_contract.py`            | async scheduling and failure retention on the composed callback           | no child process, no output modes                           |
| `cuprum/unittests/test_observe_hook_dispatch*.py` and `_observe_hook_*` | the dispatcher against a specification, about 20 hook shapes, log records | fixed event; no payload, mode, or stream                    |
| `tests/behaviour/test_structured_events.py`                             | real child and pipeline, payload metadata, snapshot of lifecycle          | synchronous hook; `cwd` and `env` absent on line events     |
| `tests/behaviour/test_observe_hook_dispatch_behaviour.py`               | six hook shapes, real child, stdout line order                            | default output options only                                 |
| `cuprum/unittests/test_line_observe_*.py`                               | retention bound, capture on and off, async hooks                          | no payload equality; echo never on                          |
| `cuprum/unittests/test_observe.py`                                      | `cwd` and `env` on `start`; async × output options with `-c pass`         | no line events in that matrix                               |

_Table 1: Existing event evidence and the gap this plan closes._

Not covered end to end today: non-`None` `cwd` or `env` on any line event; echo
with an async hook; an async hook on a pipeline; synchronous and asynchronous
hooks registered together in a real run; wall-clock readings beyond
`timestamp >= 0.0`; and any comparison with the code before 5.2.1.

### Frameworks and gates

pytest (below 9.1), pytest-bdd, pytest-timeout (30 s per test), Hypothesis
(profile `cuprum-test-suite`, no deadline), and syrupy are already development
dependencies. `cuprum/unittests/` ships in no wheel. Nose, Skylos, and
CodeScene's duplication checks do not scan the test trees, but Ruff (complexity
8, four arguments, ten locals), classic Pylint, df12-python-lints, interrogate
(100% docstrings), ambrleaks, and `ty` do. Feature files live in
`tests/features/`; behaviour modules bind scenarios with `@scenario`. There is
no rule requiring a changelog entry for a test-only change, and recent
test-only changes added none.

### Documentation and skills to read

- `docs/cuprum-design.md` §7.1 and §8.1.3; `docs/users-guide.md` "Structured
  execution events" and "Awaitable hooks and line backpressure".
- `docs/developers-guide.md`: "Choosing a test shape per observe hook",
  "Verifying dispatch against a specification oracle", and "The per-stream
  line-event emitter".
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §5 (Table 4),
  `docs/tee-hotpath-line-event-emission-5-2-1.md`, and
  `docs/tee-hotpath-observe-hook-dispatch-5-2-2.md`.
- `docs/adrs/adr-0002-additional-rust-components.md` and
  `docs/adrs/adr-0008-rust-pump-observation-channel.md`: why line events and
  the Rust pump's own events are separate channels.
- `.rules/python-00.md`, `.rules/python-typing.md`,
  `.rules/python-context-managers.md`; `docs/documentation-style-guide.md`.
- Skills: `execplans`, `sdlc-implementation`, `python-router`, then
  `python-testing` and `hypothesis`; `codegraph-mcp` for navigation;
  `scrutineer` for gates; `commit-message`, `pr-creation`, `pr-babysitting`.
  `rust-router` is not needed unless C2 is revisited.

### Budget survey

Comparable work: 5.2.1 added about 1,940 lines of Python under
`cuprum/unittests/` for line emission, and about 650 under `tests/behaviour/`;
5.2.2 added 1,439 lines of harness, 692 of tests, and 277 of behaviour tests.
Neither plan budgeted test size. The limits they did revise were unrelated to
size: 5.2.1's construction-share threshold moved twice, and 5.2.2 raised its
capture attempts from three to six. Both hit the 400-line module cap and split
modules late, so this plan starts with small modules.

Estimate: shared helpers 1,100 lines; unit tests and their support 1,000;
behaviour test and feature 280; total about 2,400, plus 15% for docstrings and
splits. Data: four distinct traces plus a 64-entry index, about 450 lines.
Documentation: developers' guide 130, design 35, repository layout 30, contents
5, roadmap 10, users' guide 5, plus contingency.

## Conformance basis

There is no Terms of Reference. Upstream artefacts, all at `c966dbf14`:

- `docs/roadmap.md` item 5.2.3, with its prerequisites 5.2.1 and 5.2.2, both
  marked done.
- `docs/cuprum-design.md` §8.1.3.
- `docs/execplans/5-2-2-remove-the-per-hook-inspect-isawaitable-call.md` D4 and
  D6, which assign the mode matrix to this item and keep the dispatch
  specification in `_observe_hook_dispatch_oracle.py`.
- `AGENTS.md` testing rules.

Requirements:

- `REQ-MATRIX`: sync and async hooks crossed with capture, echo, and
  line-callback modes, every combination.
- `REQ-EQUIV`: programme, argv, cwd, env, pid, line, and timestamp semantics are
  equivalent before and after the optimizations in each.
- `REQ-SPEC`: the payload contract in the design document holds in each.
- `REQ-LIVE`: each check demonstrably fails on a broken implementation.

Gaps: `GAP-NONE` (five fields compared only as `None`), `GAP-MODE` (no payload
check across modes), `GAP-HIST` (no evidence from before 5.2.1), `GAP-TIME` (no
wall-clock semantics check on a real run).

```plaintext
REQ-LIVE, GAP-NONE     -> EP-M1 -> TestEveryInvariantFieldIsPopulated, mutants M1–M5
REQ-MATRIX, REQ-EQUIV,
GAP-MODE, GAP-HIST     -> EP-M2 -> test_event_parity_baseline.py
REQ-SPEC, GAP-TIME     -> EP-M3 -> test_event_parity_matrix.py
REQ-LIVE               -> EP-M3 -> test_event_parity_seeded_faults.py
REQ-SPEC               -> EP-M4 -> test_event_parity_properties.py
REQ-MATRIX, REQ-SPEC   -> EP-M5 -> tests/features/event_parity.feature
roadmap closure        -> EP-M6 -> docs/roadmap.md 5.2.3 ticked
```

## Verification plan

### Abstract model

A _cell_ is (subject, hook kind, capture, echo, callback). Running a cell
yields a _run_: for each hook, the sequence of event objects it recorded; the
clock's readings; the callback deliveries; the result; and the text each sink
received. A run is grouped by (stage, phase). Let _L(stage, stream)_ be the
literal lines the workload writes there. A cell is _silent_ when capture, echo,
and callback are all off. The projection π maps a run to a _trace_: the fields
in D3, with machine-specific values replaced by the relation they satisfy.

### Invariants and lemmas

- `INV-COVER`: the parametrized cells equal the full product, with no duplicate
  identifier. (REQ-MATRIX)
- `INV-EQUIV`: for every cell, π(HEAD) equals the committed π(`7b86b904`).
  (REQ-EQUIV)
- `INV-FIELD`: every `ExecEvent` field is in exactly one of three classes —
  invariant, per line, or absent from line events. Every invariant field of a
  line event equals that field of the same stage's `start` event; every absent
  field is `None`. (REQ-SPEC)
- `INV-LITERAL`: `program`, `argv`, `cwd`, `env`, and `line` equal the
  workload's literals, and `pid` equals the pid the stage printed. (REQ-SPEC)
- `INV-LINES`: in a non-silent cell the lines recorded for (stage, stream) equal
  _L_, in order, once each; in a silent cell there are none, and `plan`,
  `start`, `exit`, and `settled` still appear once per stage. (REQ-SPEC)
- `INV-TIME`: each distinct event's timestamp is a distinct reading of the run's
  wall clock; the readings no event carries number exactly one per stage and
  equal the result's `started_at` values; and timestamps strictly increase
  within each (stage, stream). (REQ-SPEC)
- `INV-HOOK`: within a run, every hook records the same event _objects_ for each
  (stage, phase) in the same order, and nothing is recorded after `run_sync`
  returns. (REQ-SPEC)
- `INV-ORDER`: with a synchronous hook and a callback, each line's hook call
  immediately precedes its callback call. (REQ-SPEC)
- `INV-GEN`: for generated lines, chunk boundaries, hook sequences, modes, and
  metadata, the composed callback driven through `_consume_stream` yields, per
  hook, exactly the events a literal constructor builds from the drawn inputs.
  (REQ-SPEC)
- `L-RELATE`: π distinguishes any two runs that differ in a projected field:
  normalization replaces a value only when the stated relation holds.
- `L-WITNESS`: each output mode demonstrably took effect in its cell.

### Axioms (trusted, not verified here)

- `AXIOM-HISTORY`: commit `7b86b904` is the tree immediately before 5.2.1
  landed (`git rev-parse 71aaf3ebe^`), and `4419c7c6` is the tree immediately
  before 5.2.2 (`git rev-parse c0e871fc5^`).
- `AXIOM-FIFO`: asyncio runs ready callbacks in registration order ("Callbacks
  are called in the order in which they are registered", Python event-loop
  documentation). Holds for the default, non-eager task factory, which
  `run_sync` uses. Suspending hooks' record order depends on it.
- `AXIOM-PIPE`: one pipe delivers one writer's bytes in order.
- `AXIOM-PID`: the interpreter at `sys.executable` runs in the spawned process.
- `AXIOM-START`: `_StageObservation.emit` was not changed by 5.2.1 or 5.2.2 in
  how it maps its inputs to fields (checked by reading both diffs).

### Methods per obligation

Finite obligations use exhaustive parametrization. `INV-GEN` uses Hypothesis.

1. `INV-COVER`, `INV-EQUIV` — `cuprum/unittests/test_event_parity_baseline.py`.
   One test item per cell. A separate test requires the baseline's cell index
   to equal `MATRIX`. Evidence: `64 passed` plus the guards. Non-vacuity:
   before the JSON exists the module fails for a missing file; a cell removed
   from `MATRIX` fails the index test; seeded faults on each projected field
   fail. The digest of the file is pinned in the test, so changing history
   takes two deliberate edits.
2. `INV-FIELD`, `INV-LITERAL`, `INV-LINES`, `INV-TIME`, `INV-HOOK`, `INV-ORDER`,
   `L-WITNESS` — `cuprum/unittests/test_event_parity_matrix.py`. One item per
   cell calls `violations(run)` and asserts the result is empty. Each rule also
   reports how many events it examined, and the test requires a positive count
   in every non-silent cell.
3. `L-RELATE`, REQ-LIVE — `cuprum/unittests/test_event_parity_seeded_faults.py`.
   Table 2 lists the faults. Each must be rejected with a violation naming the
   expected field; those on projected fields must also be rejected by the
   baseline comparison. Each fault counts its applications, and a fault never
   applied fails.
4. `INV-GEN` — `cuprum/unittests/test_event_parity_properties.py`, at least 100
   examples, no more than 20 lines by four hooks, so the 256-task bound is
   never reached. The oracle builds expected field dictionaries from drawn
   inputs and does not call `_StageObservation.emit`. `hypothesis.event` labels
   hook mix, output mode, chunk splits inside a line, an empty line, a
   non-ASCII split, and no trailing newline; a test asserts that a `find`
   witness exists for each class. Negative control: the same comparison run
   with the `cwd` fault fails.
5. `INV-FIELD` in process — EP-M1 strengthens the existing fixtures; its guard
   rejects a fixture in which any invariant field is `None` or equal to another
   field's value.

| Fault                                                                           | Seam                                  | Must be named                       |
| ------------------------------------------------------------------------------- | ------------------------------------- | ----------------------------------- |
| `program` replaced                                                              | emitter field                         | `program`                           |
| `argv` without programme                                                        | emitter field                         | `argv`                              |
| `cwd=None`; `env=None`                                                          | emitter field                         | `cwd`; `env`                        |
| `pid` of another process                                                        | emitter field                         | `pid`                               |
| `tags`, `project`, `exec_id`, `env_mode`, `capture_mode` cleared                | emitter field                         | that field                          |
| line altered; line dropped; line duplicated; two lines swapped                  | `emit_event` wrapper                  | `line`                              |
| clock read once at preparation; monotonic clock used                            | emitter `wall_clock`                  | `timestamp`                         |
| one event object reused                                                         | `emit_event` wrapper                  | `identity`                          |
| emission skipped when `on_line` is set, when capture is off, or when echo is on | factory                               | `line`, only in the cells concerned |
| second hook's awaitable dropped                                                 | `_emit_observe_hook`                  | `hook`                              |
| callback before hook                                                            | `_invoke_on_line` / `emit_line` order | `order`                             |

_Table 2: Seeded faults and the violation each must produce._

The three mode-coupled faults are the reason the matrix exists: each is
invisible in some cells and must be caught in exactly the cells it affects. The
test asserts both halves.

### Formal verification decision

None; see D6. Residual gaps, stated so they are not rediscovered as omissions:
`resolved_path` is `None` in the process-level matrix because the baseline tree
has no executable binding (EP-M1 and EP-M4 populate it in process);
`run_concurrent`, `SafeCmd.lines()`, `run_bytes`, per-stream overrides, and the
deferred path above 256 tasks keep their existing tests; failing hooks are
covered by the 5.2.2 specification and `..._hook_contract.py`; Windows is
skipped.

### Suggested BDD features

Proposed file `tests/features/event_parity.feature`, bound by
`tests/behaviour/test_event_parity_behaviour.py`, run by `make test`. It uses
only the public API: no injected clock and no private seam. It advances
REQ-MATRIX and REQ-SPEC in EP-M5.

```gherkin
Feature: Event parity across hook types and output modes
  Observe hooks receive the same line events whatever shape the hook has and
  whatever else consumes the output.

  Scenario Outline: A hook sees the same line events in every output mode
    Given a command that writes known lines to both streams from a known directory and environment
    And a <hook> observe hook that retains every event
    When the command runs with <mode>
    Then the hook retains each stream's lines in print order
    And every line event names the command, its directory, its environment, and its process
    And every line event carries a wall-clock time within the run

    Examples:
      | hook         | mode                             |
      | synchronous  | capture only                     |
      | synchronous  | echo only                        |
      | synchronous  | a line callback only             |
      | synchronous  | capture, echo, and line callback |
      | asynchronous | capture only                     |
      | asynchronous | echo only                        |
      | asynchronous | a line callback only             |
      | asynchronous | capture, echo, and line callback |

  Scenario Outline: A run that reads no output reports its lifecycle and no lines
    Given a command that writes known lines to both streams from a known directory and environment
    And a <hook> observe hook that retains every event
    When the command runs with no capture, echo, or line callback
    Then the hook retains no line events
    And the hook retains one plan, start, exit, and settled event

    Examples:
      | hook         |
      | synchronous  |
      | asynchronous |

  Scenario: Synchronous and asynchronous hooks receive the same events
    Given a two stage pipeline that writes known lines to both streams
    And a synchronous observe hook that retains every event
    And an asynchronous observe hook that retains every event
    When the pipeline runs with capture, echo, and line callback
    Then both hooks retain the same event objects for each stage and phase
```

## Plan of work

### `EP-M1` Line-event parity stops comparing `None` with `None`

Assigned: GAP-NONE, REQ-LIVE (in process). Edit
`cuprum/unittests/test_line_event_emission_support.py` (`_make_observation`) and
`cuprum/unittests/test_line_event_emission_support_props.py` (`_observation`,
`_EmissionCase`, `_emission_case`) so the observations carry a real `Path`, a
non-empty `EnvOverlay`, `EnvMode.REPLACE`, a resolved path, and
`CaptureMode.BYTES`, each distinguishable from every other value. Where a
caller relies on the absent form, keep it as an explicit named case. Add
`TestEveryInvariantFieldIsPopulated` to
`cuprum/unittests/test_line_event_emission_parity.py`: it classifies every
`ExecEvent` field, requires the classes to partition `dc.fields(ExecEvent)`,
and requires the fixture to give every invariant field a distinct non-`None`
value.

Red: the guard test fails on the unmodified fixture, listing the five fields.
Then, in a throwaway edit of `cuprum/_line_event_emitter.py`, apply each of the
five mutants; before the fixture change each passes, after it each fails.
Revert the throwaway edit with `git checkout -- cuprum/_line_event_emitter.py`
and confirm `git diff --quiet -- cuprum/_line_event_emitter.py`.

The classification tuple lives in `tests/helpers/event_parity/fields.py`
because EP-M3 and EP-M4 use it too.

### `EP-M2` The pre-optimization baseline is captured and every cell meets it

Assigned: REQ-MATRIX, REQ-EQUIV, GAP-MODE, GAP-HIST. Create the package
`tests/helpers/event_parity/`:

- `matrix.py` — `HookKind`, `Subject`, `OutputMode`, `Cell`, `MATRIX`.
- `workload.py` — the child scripts and their literal lines, and the builders.
  The command prints its pid, a word, an empty line, a non-ASCII word, and an
  unterminated tail to stdout; its pid, a word, and the value of `PARITY` to
  stderr. The pipeline's first stage prints its pid and a word to stderr and
  one line to stdout; its second reads stdin to the end and then behaves like
  the command. Every script fixes UTF-8 output through the environment overlay.
- `runner.py` — `run_cell`. It installs an injected wall clock, gives every hook
  its own recorder, uses `io.StringIO` sinks, and returns a `CellRun`.
- `trace.py` — `project(run)` and `differences(expected, actual)`, both pure.
- `capture.py` — `python -P -m tests.helpers.event_parity.capture`.

Write `cuprum/unittests/test_event_parity_baseline.py` first. Red: it fails
because `tests/fixtures/event_parity/pre-5-2-1-traces.json` is missing. Capture
the file from `7b86b904` as shown in `Concrete steps`, twice, and require equal
bytes; capture `4419c7c6` and require the same traces. Pin the SHA-256 in the
test. Green: 64 cells pass with no production change.

The JSON holds a provenance object (source commits, capture command, Python
version), the distinct traces by name, and the cell-to-trace index. The probe
suggests four distinct traces: command and pipeline, silent and not.

`runner.py`, `workload.py`, `matrix.py`, `trace.py`, and `capture.py` must
import under `7b86b904`, so they use only names present in both trees. The one
difference, which modules own the clock seam, is an argument: tests pass
`cuprum._command_internals` and `cuprum._pipeline_observation`; `capture.py`
passes `cuprum._command_internals` and `cuprum._pipeline_internals`. This is a
call-site difference, not a compatibility layer, and `capture.py` is the only
module that knows about it.

### `EP-M3` Every cell meets the specification and every fault is rejected

Assigned: REQ-SPEC, REQ-LIVE, GAP-TIME. Add
`tests/helpers/event_parity/checks.py` with `violations(run)`, one small
function per invariant, each returning `Violation` records, and
`render(violations)`. Add `cuprum/unittests/_event_parity_faults.py`, then
`test_event_parity_seeded_faults.py` and `test_event_parity_matrix.py`.

Red: write the seeded-fault tests first against a `violations` that returns an
empty tuple, marked `xfail(strict=True)`; every fault is accepted, so every
test fails as expected. Green: implement one invariant at a time and remove
each marker as its faults are rejected. No marker may remain.

Add one syrupy snapshot of `render` over constructed violations: one field
difference, one missing line, one timestamp, and one silent-cell surprise.

### `EP-M4` Generated workloads keep the same payloads

Assigned: REQ-SPEC (`INV-GEN`). Add
`cuprum/unittests/_event_parity_generation.py` (strategies and the literal
oracle) and `test_event_parity_properties.py`. Drive
`_compose_observation_line_callback` through `_consume_stream` with the
`_ChunkedReader` double from `cuprum/unittests/_stream_drain_support.py`. Draw
the callback as absent, synchronous, or awaitable-returning, which is the shape
`SafeCmd.lines()` installs. Lines exclude the boundary characters listed in
`_LINE_BOUNDARY_CHARACTERS`; line splitting has its own properties.

Red: run the property once with the `cwd` fault installed and record the
shrunken counterexample, then keep that run as the negative-control test.

### `EP-M5` The behavioural feature and backend slice pass

Assigned: REQ-MATRIX and REQ-SPEC at the public boundary. Add the feature file
above and `tests/behaviour/test_event_parity_behaviour.py`, with steps in
`tests/behaviour/_event_parity_steps.py` if the module nears 300 lines. Reuse
`workload.py` for the commands and nothing else from the harness, so the
scenario stays an independent witness. Red: bind the scenarios before writing
the steps; pytest-bdd reports missing step definitions.

Add `test_pipeline_parity_holds_on_both_backends` to
`test_event_parity_baseline.py` using the root `stream_backend` fixture for the
pipeline with `mixed` hooks and everything on. All other cells pin
`CUPRUM_STREAM_BACKEND=python`, as the baseline tree had no extension.

### `EP-M6` Documentation and delivery closure

- `docs/developers-guide.md`: a section "Proving event parity across hooks and
  output modes" after "Verifying dispatch against a specification oracle": the
  matrix, the three oracles, how to read a failure, how to add a hook kind or
  mode, the capture command, and the retirement rule.
- `docs/cuprum-design.md` §8.1.3: a short paragraph naming the suite, the
  baseline commit, the clock invariant including the per-child read, and D2's
  retirement rule.
- `docs/repository-layout.md`: a table of the harness modules, beside the
  dispatch harness.
- `docs/contents.md`: list this plan. (Added with the plan itself.)
- `docs/users-guide.md`: no behaviour changes. Add at most one sentence to
  "Structured execution events" stating that line-event payloads do not depend
  on hook type or output mode, if the text does not already imply it.
- `docs/roadmap.md`: repair the broken line wrap in item 5.2.3, mark it done,
  and record the outcome.
- `CHANGELOG.md`: no entry; nothing a user installs changes.

## Milestones and plateaus

For each milestone the conformance check is the same list unless noted: the
assigned requirements are met; §8.1.3 is still followed; no upstream assumption
was falsified; no public interface, dependency, trust boundary, or persisted
format changed; trace links are current. Compatibility decision for every
milestone: none — everything added is test-only.

- `EP-M1`: existing parity tests discriminate on every field. Acceptance: five
  mutants flip from passing to failing. Recovery: revert the commit. Remaining:
  modes, history.
- `EP-M2`: 64 cells equal the pre-5.2.1 record. Acceptance:
  `test_event_parity_baseline.py` passes and the capture reproduces
  byte-for-byte. Recovery: delete the new files. Remaining: absolute payload
  checks, fault evidence. Extra check: `AXIOM-HISTORY` re-verified.
- `EP-M3`: 64 cells meet the specification, all faults rejected. Acceptance: no
  `xfail`, snapshot stored, 20 consecutive green runs. Remaining: generated
  inputs.
- `EP-M4`: the property passes and its control fails as intended.
- `EP-M5`: the feature passes through the public API; both backends agree.
- `EP-M6`: documents agree with the tree; roadmap ticked; all gates pass.

## Concrete steps

Working directory: the repository root of this branch's worktree. Use
`make build` once. Log names follow
`/tmp/$ACTION-cuprum-5-2-3-combinatorial-event-parity-suite.out`.

Capture (EP-M2), using a scratch directory outside the repository:

```bash
set -o pipefail
REPO=$PWD
SCRATCH=$(mktemp -d)
mkdir "$SCRATCH/pre" "$SCRATCH/mid" "$SCRATCH/out"
test "$(git rev-parse 71aaf3ebe^)" = 7b86b904b99140c041c28a6258efd4fb4c8d6186
test "$(git rev-parse c0e871fc5^)" = 4419c7c65ca514d52f10a034dd6756173500542a
git archive 7b86b904 cuprum | tar -x -C "$SCRATCH/pre"
git archive 4419c7c6 cuprum | tar -x -C "$SCRATCH/mid"
for run in a b; do
  PYTHONPATH="$SCRATCH/pre:$REPO" PYTHONDONTWRITEBYTECODE=1 \
    uv run --no-project --python 3.14 python -P \
    -m tests.helpers.event_parity.capture \
    --tree "$SCRATCH/pre" --commit 7b86b904 --output "$SCRATCH/out/pre-$run.json"
done
cmp "$SCRATCH/out/pre-a.json" "$SCRATCH/out/pre-b.json"
```

Expected: no output from `cmp`. Repeat for `mid` with `--tree "$SCRATCH/mid"`
and compare the `traces` and `cells` objects. Copy `pre-a.json` into
`tests/fixtures/event_parity/pre-5-2-1-traces.json`.

Focused loop for any milestone:

```bash
uv run pytest -q cuprum/unittests/test_event_parity_baseline.py
uv run pytest -q cuprum/unittests/test_event_parity_matrix.py \
  cuprum/unittests/test_event_parity_seeded_faults.py
uv run pytest -q --hypothesis-show-statistics \
  cuprum/unittests/test_event_parity_properties.py
uv run pytest -q tests/behaviour/test_event_parity_behaviour.py
```

Stability (EP-M3), run on one processor to provoke scheduling differences:

```bash
for n in $(seq 20); do
  taskset -c 0 uv run pytest -q -p no:randomly \
    cuprum/unittests/test_event_parity_baseline.py \
    cuprum/unittests/test_event_parity_matrix.py || break
done
```

Full gates before every commit, in this order, through `scrutineer`:

```bash
make check-fmt
make typecheck
make lint
make test
make markdownlint   # when Markdown changed; also runs spelling
make nixie          # when Markdown changed
```

## Validation and acceptance

Acceptance, as behaviour:

1. `uv run pytest -q cuprum/unittests/test_event_parity_baseline.py` reports 64
   cell items passing, plus its guards.
2. Editing `cuprum/_line_event_emitter.py` so `emit_line` passes `cwd=None`
   fails `make test-python` with a report naming `cwd`; so do the other four
   mutants, and none did before this work.
3. Editing `_line_event_emitter` to return `None` when `context.on_line` is set
   fails exactly the cells whose callback is on and which are not silent.
4. The baseline regenerates byte-for-byte from `7b86b904`.
5. `make test` passes; the new modules add less than 60 s serially.

Quality criteria: every invariant has at least one seeded fault rejected for
the intended reason; no `xfail`, `skip` (other than Windows and a missing
extension), or lint suppression without a stated reason; all gates pass.

## Idempotence and recovery

Every step can be repeated. The capture writes only to the path it is given and
refuses a tree that contains `_line_event_emitter.py`, except under an explicit
`--allow-optimized` flag used for the `4419c7c6` cross-check, which also
refuses to write into `tests/fixtures/`. Throwaway mutants are made one at a
time and reverted with `git checkout -- <file>`; confirm a clean
`git status --short` before committing. Never use `git stash` in this worktree;
the stash is shared.

## Artefacts and notes

Planning-time evidence, all from scratch copies outside the repository.

Historical equivalence probe at `c966dbf14`; each line is one capture of 64
cells. "Pinned" runs used `taskset -c 3`.

```plaintext
pre-5.2.1 7b86b904, Python 3.14          1 capture
pre-5.2.2 4419c7c6, Python 3.14          1 capture
HEAD c966dbf14, Python 3.12, pinned      5 captures
HEAD c966dbf14, Python 3.14, pinned      5 captures
md5sum of all 12 outputs: 1 distinct value
elapsed per capture: 1.1 s to 1.9 s
```

Mutants of `cuprum/_line_event_emitter.py` against the 161 focused tests
(`test_line_event_emission_*`, `test_observe*`, `test_settled_hook_dispatch`,
`test_line_observe*`, `test_line_events`, and the two behaviour modules):

```plaintext
cwd=None             161 passed   SURVIVED
env=None             161 passed   SURVIVED
env_mode=None        161 passed   SURVIVED
resolved_path=None   161 passed   SURVIVED
capture_mode=None    161 passed   SURVIVED
project=None           2 failed   killed
tags={}                6 failed   killed
exec_id=None           7 failed   killed
pid=None               9 failed   killed
argv without program   7 failed   killed
monotonic clock       12 failed   killed
frozen clock           5 failed   killed
```

Wider-suite result for the survivors: being measured; recorded before approval.

Fault seam probe: replacing `_line_callbacks._line_event_emitter` with a
wrapper that returns `dataclasses.replace(emitter, cwd=None)` was applied twice
per command (stdout and stderr) and changed both the projected trace and the
comparison with `start`. `capture_mode=None` and `env_mode=None` changed only
the comparison with `start`, as D3 intends.

Prior art consulted: NIST's combinatorial-testing guidance (testing all t-way
combinations is pseudo-exhaustive when faults involve t or fewer variables;
with so few parameters the full product is affordable, so no covering array is
needed); Python's event-loop documentation for callback order; syrupy's
documentation for what `--snapshot-update` rewrites and deletes.

## Interfaces and dependencies

No new dependency. New test-only interfaces, owned by
`tests/helpers/event_parity/` and used only by the modules named here:

```python
# matrix.py
class HookKind(enum.StrEnum): ...      # SYNC, ASYNC, SUSPENDING, MIXED
class Subject(enum.StrEnum): ...       # COMMAND, PIPELINE

@dc.dataclass(frozen=True, slots=True)
class OutputMode:
    capture: bool
    echo: bool
    line_callback: bool

    @property
    def is_silent(self) -> bool: ...

@dc.dataclass(frozen=True, slots=True)
class Cell:
    subject: Subject
    hook: HookKind
    mode: OutputMode

    @property
    def cell_id(self) -> str: ...      # "command-mixed-capture-echo-callback"

MATRIX: tuple[Cell, ...]

# runner.py
@dc.dataclass(frozen=True, slots=True)
class CellRun:
    cell: Cell
    workdir: Path
    events_by_hook: cabc.Mapping[str, tuple[ExecEvent, ...]]
    journal: tuple[tuple[str, str], ...]
    clock_readings: tuple[float, ...]
    callback_lines: tuple[LineEvent, ...]
    echoed: tuple[str, str]
    result: CommandResult | PipelineResult

def run_cell(
    cell: Cell, workdir: Path, clock_modules: tuple[types.ModuleType, ...]
) -> CellRun: ...

# trace.py
def project(run: CellRun) -> Trace: ...
def differences(expected: Trace, actual: Trace) -> tuple[str, ...]: ...

# fields.py
INVARIANT_FIELDS: frozenset[str]
PER_LINE_FIELDS: frozenset[str]        # phase, line, timestamp
ABSENT_FIELDS: frozenset[str]

# checks.py
@dc.dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    location: str                      # stage, phase, event index
    expected: str
    actual: str

def violations(run: CellRun) -> tuple[Violation, ...]: ...
def render(cell: Cell, found: cabc.Sequence[Violation]) -> str: ...
```

Reused as they are: `tests.helpers.catalogue.python_catalogue`; `_fields` and
`_make_observation`; `_ChunkedReader`, `_reader`, and `_config` from
`_stream_drain_support.py`; the `stream_backend` fixture. Not reused, with
reasons: `normalize_event` in `tests/behaviour/_structured_events_support.py`
masks pid and timestamp, which are under test; `mismatch` in
`_observe_hook_dispatch_compare.py` compares dispatch for a fixed event and
takes no payload; `_run_observed` in `test_line_observe_support.py` is built
for bounded-delivery floods.

Reuse policy: `tests/helpers/event_parity` is for event-payload equivalence
across execution modes. Tests of the dispatcher alone extend the 5.2.2
specification; tests of retention extend `test_line_observe_support.py`.

## Revision note

- 2026-11-25: initial draft from reconnaissance and feasibility probes.
