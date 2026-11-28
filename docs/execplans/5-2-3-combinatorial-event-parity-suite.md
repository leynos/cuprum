# 5.2.3 Add a combinatorial event-parity suite

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT (expert review applied; awaiting user approval)

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
may each be on or off. Today, a defect that set `env=None` on every line event
passes 4,407 tests, and one that set `cwd=None` fails exactly one, in a module
about executable binding (see `Surprises & discoveries`). `cwd` and `env` are
both fields the roadmap item names.

After this change:

1. `make test` runs a matrix of 64 cells, one real command or two-stage pipeline
   per cell: four hook kinds, by eight output modes, by two subjects.
2. Each cell is checked against a specification of the payload — programme,
   argv, cwd, env, pid, line, and timestamp semantics — so a failure names the
   field and the event that broke.
3. Each cell is also compared with a small committed record captured from the
   real source tree _before_ item 5.2.1, so "equivalent before and after" is
   measured rather than argued.
4. Deliberate faults prove that each check can fail, including faults that are
   visible only in some combinations. Those are the reason for a matrix.

No production behaviour, public API, or user-visible output changes. A failing
cell prints a short report naming the cell, rule, field, event, expected value,
and actual value. That report is the only new "interface".

## Constraints

Violating one of these requires escalation, not a workaround.

- C1. No production source under `cuprum/` outside `cuprum/unittests/` may
  change. If a cell exposes a real defect, stop and follow `Tolerances`.
- C2. No Rust, dependency, packaging, CI workflow, or benchmark change. Phase 5
  is Rust-free by design (`docs/roadmap.md`, phase 5 "Idea").
- C3. The committed baseline is historical evidence. It may be captured only
  from commit `7b86b904b99140c041c28a6258efd4fb4c8d6186` (the parent of
  `71aaf3ebe`, which landed 5.2.1), never from a tree that contains either
  optimization, and no `--snapshot-update` run may rewrite it (Decision D2).
- C4. Existing assertions stay as strong as they are. Existing tests may be
  strengthened (EP-M1), never weakened or deleted.
- C5. Reuse before adding; `Interfaces and dependencies` says what is reused and
  why anything is not. The 5.2.2 dispatch specification
  (`_observe_hook_dispatch_oracle.py`, called V1 in that plan) stays the single
  authority for _dispatcher_ behaviour over hook sequences. This suite extends
  it with payloads and modes and must not restate it.
- C6. Modules under `tests/helpers/` stay at or below 400 lines: that directory
  is inside the Pylint line cap. Target 300 lines per new module everywhere.
- C7. Assertions live in `test_*.py` modules. Helpers return values describing
  violations and never use the `assert` statement.
- C8. New test modules must match an existing `PYTEST_TARGETS` glob in the
  `Makefile`: `cuprum/unittests/test_*.py` and `tests/behaviour/test_*.py` do;
  a new directory directly under `tests/` would not, and no guard would notice.
- C9. Tests must not depend on real-time sleeps, real-time thresholds, a
  non-decreasing system clock, or the relative order of stdout and stderr,
  which Cuprum does not guarantee.
- C10. Python 3.12 is the oldest supported interpreter. CI runs the suite on
  3.12 and 3.14, coverage on its own interpreter, and an experimental 3.15 leg
  after merge; nothing may depend on one version.
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

Restrict the pathspec for each category. Every changed file belongs to one:

- Code (`'*.py' '*.feature'`): at most 2,450 net lines.
- Configuration (`pyproject.toml`, `typos.local.toml`, `typos.toml`, and the
  `Makefile`): at most 20 net lines.
- Captured data (`tests/fixtures/event_parity` and `'*.ambr'`): at most 350 net
  lines. This is generated evidence, budgeted apart so it cannot hide code
  growth.
- Documentation (`'*.md'`, excluding `docs/execplans`): at most 220 net lines.
- Files: at most 26 non-ExecPlan files.

Each category is enforced alone; spare capacity in one does not offset another.
The survey behind these figures is in `Context and orientation`.

Other triggers:

- Divergence: if any cell differs from the baseline or the specification on
  unmodified production code, stop. Record the cell, the field, both values,
  and whether `4419c7c6` (after 5.2.1, before 5.2.2) agrees with either side.
  Do not edit the baseline, narrow the projection, or add an exemption.
- Baseline reproduction: if two captures of `7b86b904` differ, stop.
- Flakiness: one failure in the stability runs (`Concrete steps`) on an
  otherwise passing tree stops the work until its cause is known. Do not add a
  retry.
- Run time: stop if the new modules add more than 60 seconds to a serial run
  confined to two processors (`taskset -c 0,1`, the shape of the CI runner), or
  if any single test item exceeds 10 seconds.
- Vacuity: stop if any seeded fault is accepted, is rejected without naming its
  primary field, or is applied a number of times other than the one predicted.
- Prediction: the rule sets in Table 2 are predictions. If a fault trips a
  different set, explain the difference in `Decision log` before changing
  either the table or the rule.
- Interface or dependency: any need for a new one outside
  `Interfaces and dependencies` stops the work.
- Iterations: stop after three failed attempts to make one red test green.
- Ambiguity: stop if two readings of the matrix would change which cells exist.

## Risks

- R1. The baseline tree differs from HEAD by far more than the two
  optimizations, so an unrelated, deliberate payload change could look like a
  regression. Severity: medium. Likelihood: low today; the probe found all 64
  cells equal. Mitigation: the baseline projection holds only the fields the
  roadmap names (Decision D3). `settled` events, which did not exist then, are
  outside it and are covered by the specification.
- R2. The capture could silently import HEAD's `cuprum`, making the baseline a
  copy of the code under test, or could inject its clock into the wrong module.
  Severity: high. Likelihood: medium, because `python -m` puts the working
  directory ahead of `PYTHONPATH`, and the pipeline clock moved module between
  `7b86b904` and `4419c7c6`. Mitigation: the command uses `python -P`; the tool
  refuses to write unless `cuprum.__file__` lies under the tree it was given,
  unless each named clock module owns a `time` attribute, and unless the
  injected clock was read.
- R3. Scheduling nondeterminism makes cells flaky. Severity: high. Likelihood:
  low. Mitigation: events are compared per stage and per stream only, each hook
  has its own recorder, clocks are injected, and no cell sleeps. Independent
  review runs (30 × 256 concurrent unpinned, 8 × 256 on two processors, 5 × 64
  on each of 3.12, 3.14, and 3.15.0b2) saw no violation of the clock, identity,
  or ordering invariants.
- R4. A normalization masks a defect, for example by replacing every pid with
  one marker. Severity: high. Likelihood: medium. Mitigation: normalization is
  _relational_. Each child writes its own pid, working directory, and `PARITY`
  value to `stage-N.json` in its working directory; a pid becomes `<child>`
  only when it equals that file's pid. The file works in silent cells too,
  where the child's output goes to `/dev/null`, and does not travel through the
  line events under test.
- R5. A cell passes because its mode never took effect. Severity: medium.
  Likelihood: medium. Mitigation: every cell asserts a _mode witness_ —
  captured text present exactly when capture is on, sink text present exactly
  when echo is on, and callback deliveries exactly when the callback is on.
- R6. A future `ExecEvent` field is populated by `_StageObservation.emit` but
  forgotten in `_LineEventEmitter`. This is the likeliest real regression.
  Mitigation: every field of a line event except `phase`, `line`, and
  `timestamp` must equal the same field of that stage's `start` event. No list
  needs editing. Residual: a field that is `None` in this workload is not
  exercised; EP-M1 covers those the emitter already carries.
- R7. The environment leaks into children. Severity: medium. Likelihood: medium;
  review showed `PYTHONVERBOSE=1` adds 145 stderr lines. Mitigation: children
  run as `python -I -c …`, which ignores `PYTHON*` variables, and write bytes
  through `sys.stdout.buffer`, so no output encoding is configured.
- R8. An awaitable hook that merely yields once finishes by accident, so the
  suite would not notice a run that never waits for its hooks. Severity: high.
  Likelihood: certain with a naive hook — review replaced the drain with a
  no-op and every record still arrived. Mitigation: the awaitable hook parks
  until all `exit` events have been seen and then yields 200 loop turns. With
  that shape the no-op drain loses every line record on all three trees
  (`Artefacts`).
- R9. Child-process cost. One pass over the matrix is 96 children (32 commands
  and 32 two-stage pipelines). Each cell runs once and both oracles examine
  that one run. Faults add about 120 children and the feature about 12, about
  230 in all. Measured cost was 17–30 ms per cell idle and 65–125 ms loaded.
- R10. Hosts whose interpreter is a launcher stub report a different pid
  (Windows virtual environments). Likelihood: low; the Python suite runs on
  Linux in CI. Mitigation: the matrix modules skip on Windows with that reason.

## Progress

- [x] (2026-11-25 14:30Z) Branch created; governing documents, production code,
  existing tests, and 5.2.1/5.2.2 history read with four read-only
  reconnaissance agents.
- [x] (2026-11-25 15:25Z) Feasibility probes run; results recorded below.
- [x] (2026-11-25 16:00Z) Initial draft committed and pushed (`8d042c6b2`).
- [x] (2026-11-25 16:30Z) Expert review reconciled: structure and contracts,
  failure modes and cost, alternatives and viability, and a separate fact check.
- [ ] Plan approved by the user.
- [ ] `EP-M1` Line-event parity stops comparing `None` with `None`.
- [ ] `EP-M2` Every cell meets the specification and every fault is rejected.
- [ ] `EP-M3` Every cell equals the record from before the optimizations.
- [ ] `EP-M4` Generated workloads keep the same payloads.
- [ ] `EP-M5` The behavioural feature passes through the public API.
- [ ] `EP-M6` Documentation and roadmap are reconciled; all gates pass.

## Surprises & discoveries

- Observation: the existing in-process parity fixtures compare five invariant
  fields only as `None == None`. Evidence: `_make_observation` and
  `_observation` (`cuprum/unittests/test_line_event_emission_support.py`,
  `…_support_props.py`) set `cwd=None` and `env_overlay=None` and leave
  `env_mode`, `resolved_path`, and `capture_mode` at their `None` defaults. In
  a scratch copy of `c966dbf14`, editing `_LineEventEmitter.emit_line` to pass
  `None` for any of the five left all 161 focused tests green. In a wider run
  of 518 modules, `env=None` still passed everything, `cwd=None` and
  `resolved_path=None` failed one test each, `capture_mode=None` two, and
  `env_mode=None` four. Impact: EP-M1 exists, and `env=None` is this plan's
  strongest red evidence.
- Observation: the true "before" code is runnable. Evidence:
  `git archive 7b86b904 cuprum` runs under the current interpreter with no
  native extension, and its public API already accepts `capture`, `echo`,
  `on_line`, `cwd`, `env`, and `tags`. Impact: the baseline is captured, not
  reconstructed (D2).
- Observation: `7b86b904`, `4419c7c6`, and HEAD produce identical traces for all
  64 cells. Evidence: 12 captures with one digest (`Artefacts and notes`).
  Impact: no production defect is expected.
- Observation: two hooks sharing one recorder made traces nondeterministic.
  Impact: every hook has its own recorder.
- Observation: the wall clock is read once per event _plus_ once per spawned
  child. Evidence: a counting clock saw 12 reads for 11 events in a command and
  19 for 17 in a pipeline; the unowned readings equal the result's `started_at`
  values exactly. The reads are `wall_clock_started_at` in
  `cuprum/_subprocess_execution.py` and `cuprum/_pipeline_spawn.py`, and exist
  in `7b86b904`. Impact: `INV-TIME` carries that term.
- Observation: with capture, echo, and `on_line` all off, streams are bound to
  `/dev/null` and no line event exists, but lifecycle events still carry pids.
  Impact: the silent mode is a real cell, and pids need a witness outside
  stdout.
- Observation: a hook that waits only for `exit` does not detect a skipped final
  drain, because the woken tasks finish in the loop turn that stops the loop.
  Adding 200 yields does detect it. Impact: R8.
- Observation: the profiling harness's `--line-callbacks` flag installs an
  _observe hook_, not `on_line`. Impact: see D4.

## Decision log

All decisions: 2026-11-25, planning agent, revised after expert review. They
take effect when the user approves the plan.

- D1. Decision: the matrix is the full Cartesian product of hook kind (`sync`,
  `async`, `awaitable`, `mixed`), `capture`, `echo`, and `on_line` (each on or
  off), and subject (`command`, `pipeline`): 4 × 8 × 2 = 64 cells. Rationale:
  the roadmap says "every combination", and the product is cheap, so no
  covering-array argument about which combinations matter is needed. The kinds
  follow the three branches of `_emit_observe_hook`: `sync` returns `None`;
  `async` is an `async def`, a native coroutine; `awaitable` is a plain
  callable returning an object with `__await__`, which reaches
  `inspect.isawaitable`, and it parks as R8 describes; `mixed` registers `sync`
  and `awaitable` together. `pipeline` is a subject because pipelines build
  their observations elsewhere. Rejected: pairwise sampling; `run_concurrent`,
  byte mode, per-stream overrides, and the stream backend as axes.
  `SafeCmd.lines()` gets one specification-only item per hook kind, outside the
  product, because it has its own spawn path and many users would call it
  line-callback mode.
- D2. Decision: "before" is a small committed JSON record captured from the real
  pre-5.2.1 tree, compared by plain equality in the same test item as the
  specification. Rationale: literal before/after equality is the roadmap's
  criterion, and an equality that shares no rule code with the checker guards
  against a checker that is too weak. A frozen copy of the old functions would
  drift; 5.2.2's D4 rejected that shape for the dispatcher. A syrupy snapshot
  would be rewritten by any `--snapshot-update` run. Alternative considered:
  run the specification against the old tree once, record the result as
  evidence, and commit no trace. That is less to maintain, and review rated it
  stronger on that ground; it was declined because the trace needs no index and
  only four entries (`command`, `command-silent`, `pipeline`,
  `pipeline-silent`), so hook kinds and modes can be added without touching it,
  and because the equality would otherwise never run again. Lifecycle: capture
  again only from `7b86b904`, and only when the workload itself changes. If a
  payload field in the projection is _deliberately_ changed, or the capture no
  longer imports under that tree, delete the record, its comparison, and the
  capture tool together, and say so in the commit; the specification remains.
  The failure message states these rules.
- D3. Decision: the baseline projection holds `phase`, `program`, `argv`, `cwd`,
  `env`, `pid`, `line`, and a timestamp verdict, for phases `plan`, `start`,
  `stdout`, `stderr`, and `exit`, grouped by stage. The verdict is `fresh` (a
  clock reading no other event carries), `reused`, or `foreign` (not a reading
  of the injected clock). Rationale: these are the roadmap's named fields.
  `tags` and later fields belong to the specification, so a deliberate new tag
  does not falsify history.
- D4. Decision: "line-callback mode" means `RunOutputOptions.on_line`.
  Rationale: the observe hook is already the first axis, so reading the phrase
  as "observe hook present" would make the axis redundant.
- D5. Decision: the in-run oracle for a line event's invariant fields is its own
  stage's `start` event, plus literals. Rationale: `start` is built by the
  generic `_StageObservation.emit`, which neither optimization changed, so no
  patched reference path is needed. Because `emit` and the emitter could drift
  together, every field the workload knows is also compared with a literal:
  `program`, `argv`, `cwd`, `env`, `pid`, `line`, `project`, `env_mode`,
  `capture_mode`, and `tags`, whose `capture` and `echo` entries are the only
  payload that varies by mode.
- D6. Decision: no Rust, `rstest`, `rstest-bdd`, `proptest`, `insta`, Kani,
  Verus, or CrossHair work. Rationale: the change adds no production logic,
  axiom, or Rust code. The obligations concern CPython object identity, asyncio
  scheduling, and child processes, which a solver cannot explore; the finite
  part is enumerated exhaustively and the open-ended part uses Hypothesis.
  Items 5.2.1 and 5.2.2 reached the same conclusion.
- D7. Decision: syrupy is used once, for the rendered violation report.
  Rationale: it is what a person reads when a cell fails, it has several
  variants, and its inputs are constructed, so it is deterministic. Payload
  equality is asserted semantically, not by snapshot (D2).
- D8. Decision: no ADR. Rationale: the suite changes no architecture, interface,
  or policy; D2's lifecycle is recorded in `docs/cuprum-design.md` §8.1.3 and
  the developers' guide.
- D9. Decision: the specification and its faults (EP-M2) come before the
  baseline (EP-M3). Rationale: a baseline comparison whose ability to fail is
  unproven is not a coherent plateau.
- D10. Decision: events are attributed to a stage by what cannot be under test
  at that moment — lifecycle events by argv, line events by line text, which
  the workload makes unique per stage and stream. `exec_id` and the stage tags
  are then checked against that attribution. Rationale: grouping by `exec_id`
  or tags would make those checks circular. An event that cannot be attributed
  is itself a violation.
- D11. Decision: no backend axis or Rust-backend slice. Cells pin
  `CUPRUM_STREAM_BACKEND=python`, as the baseline tree had no extension.
  Rationale: the backend affects only the pump between stages and never a
  line-observed stream. CI's ordinary jobs skip extension tests, so a slice
  would report success for not running unless added to `EXTENSION_TEST_TARGETS`.

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
composes a stderr callback for every stage and a stdout callback only for the
last, and starts a consumer only when that stream is consumed. Two names are
resolved as module globals when a callback is composed, and are the seams the
seeded faults replace: `_line_event_emitter`, the factory that returns a
`_LineEventEmitter` or `None` with no observe hook, and
`_compose_line_callbacks`, which returns the per-line closure. The order "hook,
then `on_line`" is decided inside that closure.

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

Not covered end to end today: a non-`None` `env` on any line event, and `cwd`
only by one executable-binding test; echo with an async hook; payloads under an
async hook on a pipeline; synchronous and asynchronous hooks registered
together in a real run; wall-clock readings beyond `timestamp >= 0.0`; and any
comparison with the code before 5.2.1. "Not covered" means reconnaissance and
the mutant runs found none.

### Frameworks and gates

pytest (below 9.1), pytest-bdd, pytest-timeout (30 s per test), Hypothesis
(profile `cuprum-test-suite`, no deadline), and syrupy are already development
dependencies. `cuprum/unittests/` ships in no wheel. Nose and Skylos scan only
`cuprum/` outside `unittests/`. Ruff (complexity 8, four arguments, ten
locals), classic Pylint, df12-python-lints, interrogate (100% docstrings),
ambrleaks, and `ty` cover the new files, and CodeScene reviews test files in
pull requests. Feature files live in `tests/features/`; behaviour modules bind
scenarios with `@scenario`. There is no rule requiring a changelog entry for a
test-only change, and recent test-only changes added none.

### Documentation and skills to read

- `docs/cuprum-design.md` §8.1.3 (the event contract); `docs/users-guide.md`
  "Structured execution events" and "Awaitable hooks and line backpressure".
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

Comparable work, from the diffs of `71aaf3ebe` and `c0e871fc5`: 5.2.1 added
1,723 lines of Python under `cuprum/unittests/` for line emission and changed
about 1,000 lines of behaviour tests; 5.2.2 added 1,439 lines of harness, 692
of tests, and 277 of behaviour tests. Neither plan budgeted test size. The
limits they did revise were unrelated to size: 5.2.1's construction-share
threshold moved twice, and 5.2.2 raised its capture attempts from three to six.
Both hit the 400-line module cap and split modules late, so this plan starts
with small modules.

Estimate: shared helpers 960 lines; unit tests and their support 850; behaviour
test and feature 260; total about 2,070, plus 18% for docstrings and splits.
Review asked for a smaller suite than the first draft's 2,800; merging the two
matrix modules, dropping the generation support module, the field lists, and
the backend slice produced this figure. Data: four traces, about 150 lines, and
one small snapshot. Documentation: developers' guide 75, design 30, repository
layout 25, roadmap 12, plus contingency.

## Conformance basis

There is no Terms of Reference. Upstream artefacts, all at `c966dbf14`:

- `docs/roadmap.md` item 5.2.3, with its prerequisites 5.2.1 and 5.2.2, both
  marked done.
- `docs/cuprum-design.md` §8.1.3.
- `docs/execplans/5-2-2-remove-the-per-hook-inspect-isawaitable-call.md`: D4,
  which rejects a frozen dispatcher copy because it "rots and duplicates item
  5.2.3's parity suite" and asks this plan to name V1 as the property it
  extends; and "Related prior work", which assigns the hook-type and
  stream-mode matrix to this item.
- `AGENTS.md` testing rules.

Requirements:

- `REQ-MATRIX`: sync and async hooks crossed with capture, echo, and
  line-callback modes, every combination.
- `REQ-EQUIV`: programme, argv, cwd, env, pid, line, and timestamp semantics are
  equivalent before and after the optimizations in each.
- `REQ-SPEC`: the payload contract in the design document holds in each.
- `REQ-LIVE`: each check demonstrably fails on a broken implementation.

Gaps: `GAP-NONE` (five fields compared only as `None` in process), `GAP-MODE`
(no payload check across modes), `GAP-HIST` (no evidence from before 5.2.1),
`GAP-TIME` (no wall-clock semantics check on a real run), `GAP-WAIT` (no check
across modes that a run waits for its hooks).

```plaintext
REQ-LIVE, GAP-NONE            -> EP-M1 -> TestEveryHoistedFieldIsPopulated
REQ-MATRIX, REQ-SPEC,
GAP-MODE, GAP-TIME, GAP-WAIT  -> EP-M2 -> test_event_parity_matrix.py
REQ-LIVE                      -> EP-M2 -> test_event_parity_seeded_faults.py
REQ-EQUIV, GAP-HIST           -> EP-M3 -> baseline assertion in the same items
REQ-SPEC                      -> EP-M4 -> test_event_parity_properties.py
REQ-MATRIX, REQ-SPEC          -> EP-M5 -> tests/features/event_parity.feature
roadmap closure               -> EP-M6 -> docs/roadmap.md 5.2.3 ticked
```

## Verification plan

### Abstract model

A _cell_ is (subject, hook kind, capture, echo, callback). Running a cell
yields a _run_: for each hook, the event objects it recorded in order; a
journal of synchronous hook and callback calls; the injected clock's readings;
the callback deliveries; the result; the text each sink received; and one
witness file per stage. Let _L(stage, stream)_ be the literal lines the
workload writes there; every line text occurs in exactly one (stage, stream). A
cell is _silent_ when capture, echo, and callback are all off. The projection π
maps one hook's events to a _trace_: the fields in D3, with machine-specific
values replaced by the relation they satisfy.

### Invariants and lemmas

Each invariant is one `Rule` in the checker. `applies(rule, cell)` states where
it has content, and the matrix test requires that rule to have examined at
least one item exactly there.

- `INV-COVER`: the parametrized cells equal the full product, with unique
  identifiers. (REQ-MATRIX)
- `INV-FIELD` (all non-silent cells): every `ExecEvent` field of a line event
  except `phase`, `line`, and `timestamp` equals that field of the same stage's
  `start` event. (REQ-SPEC)
- `INV-LITERAL` (all cells): the fields listed in D5 equal the workload's
  literals on every event that carries them; `pid` is `None` on `plan` and the
  witness pid elsewhere; the witness proves the child really ran in `cwd` with
  `env`. (REQ-SPEC)
- `INV-LINES` (all cells): for each hook, the lines recorded for (stage, stream)
  equal _L_, in order, once each, or are none in a silent cell; and each stage
  has exactly one `plan`, `start`, `exit`, and `settled`. Because the awaitable
  hook parks past `exit`, this is also the waiting check (`GAP-WAIT`).
  (REQ-SPEC)
- `INV-TIME` (all cells): each distinct event's timestamp is a distinct reading
  of the run's wall clock; the readings no event carries number exactly one per
  stage and equal the result's `started_at` values; and timestamps strictly
  increase within each (stage, stream). (REQ-SPEC)
- `INV-HOOK` (`mixed` cells): both hooks record the same event _objects_ for
  each (stage, phase), in the same order. (REQ-SPEC)
- `INV-ORDER` (`sync` and `mixed` cells with a callback): in the journal, each
  line's synchronous hook call immediately precedes its callback call. Both
  calls happen in one uninterrupted step, so stream interleaving cannot
  separate them. (REQ-SPEC)
- `INV-MODE` (all cells): the mode witness of R5. (REQ-MATRIX)
- `INV-EQUIV`: for every cell and every hook, π at HEAD equals the committed π
  at `7b86b904`. (REQ-EQUIV)
- `INV-GEN`: for generated lines, chunk boundaries, hook sequences, callback
  shapes, and metadata, the composed callback driven through `_consume_stream`
  yields, per hook, exactly the events a literal constructor builds from the
  drawn inputs. This extends V1, whose event is fixed. (REQ-SPEC)
- `L-RELATE`: π distinguishes any two runs that differ in a projected field.

### Axioms (trusted, not verified here)

- `AXIOM-HISTORY`: `7b86b904` is the tree immediately before 5.2.1 landed
  (`git rev-parse 71aaf3ebe^`), and `4419c7c6` the tree immediately before 5.2.2
  (`git rev-parse c0e871fc5^`). Both were checked during planning.
- `AXIOM-FIFO`: asyncio runs ready callbacks in registration order ("Callbacks
  are called in the order in which they are registered", Python event-loop
  documentation), under the default, non-eager task factory.
- `AXIOM-PIPE`: one pipe delivers one writer's bytes in order.
- `AXIOM-PID`: the interpreter at `sys.executable` runs in the spawned process.
- `AXIOM-START`: neither 5.2.1 nor 5.2.2 changed `_StageObservation.emit`.
  Checked by reading both diffs: 5.2.1 touched only
  `cuprum/_line_callbacks.py`, 5.2.2 only `cuprum/_observability.py`.

### Methods per obligation

Finite obligations use exhaustive parametrization; `INV-GEN` uses Hypothesis.

1. Matrix: `cuprum/unittests/test_event_parity_matrix.py`, one item per cell
   plus four `lines()` items, plus a guard for `INV-COVER`. Each item calls
   `run_cell` once, asserts that `check(run)` reports no violation, asserts the
   examined counts, and (from EP-M3) asserts that every hook's trace equals the
   baseline. Non-vacuity: the examined counts, the mode witness, and the faults
   below.
2. Faults: `cuprum/unittests/test_event_parity_seeded_faults.py`. Each row of
   Table 2 runs on the `pipeline-mixed` cell with capture and echo on and no
   callback, so ORDER does not apply; the last row adds the callback. Each row
   asserts the primary field, the exact rule set, and the number of times the
   fault was applied. From EP-M3, faults on projected fields must also make the
   trace differ. The mode-coupled faults run over the 16 `async` cells each,
   and must be rejected in exactly the affected cells and accepted in the rest.
3. Report: one syrupy snapshot of `render` over constructed violations, paired
   with semantic assertions on the `Violation` values.
4. Property: `cuprum/unittests/test_event_parity_properties.py`, at least 100
   examples, at most 20 lines by four hooks so the 256-task bound is never
   reached. The oracle builds expected field dictionaries from the drawn inputs
   and never calls `_StageObservation.emit`. `hypothesis.event` labels: hook
   mix, callback shape, a chunk split inside a line, an empty line, a split
   multi-byte character, and no trailing newline; a test uses `hypothesis.find`
   to show each class is reachable. Negative control: the same comparison with
   the `env` fault fails.
5. EP-M1 guard: the fields shared by `_LineEventEmitter` and `ExecEvent`,
   derived with `dataclasses.fields`, must each be non-`None` and distinct in
   the in-process fixtures. Deriving the set means no list to maintain.

| Fault                                              | Seam                                      | Primary field | Expected rules     |
| -------------------------------------------------- | ----------------------------------------- | ------------- | ------------------ |
| `program` replaced                                 | emitter field                             | `program`     | FIELD, LITERAL     |
| `argv` without programme                           | emitter field                             | `argv`        | FIELD, LITERAL     |
| `cwd=None`                                         | emitter field                             | `cwd`         | FIELD, LITERAL     |
| `env=None`                                         | emitter field                             | `env`         | FIELD, LITERAL     |
| `pid` of another process                           | emitter field                             | `pid`         | FIELD, LITERAL     |
| `tags` with `capture` inverted                     | emitter field                             | `tags`        | FIELD, LITERAL     |
| `project`, `env_mode`, `capture_mode` cleared      | emitter field, one fault each             | that field    | FIELD, LITERAL     |
| `exec_id` replaced                                 | emitter field                             | `exec_id`     | FIELD              |
| line altered                                       | `emit_event` wrapper                      | `line`        | LINES              |
| line dropped; duplicated; two lines swapped        | `emit_event` wrapper, one fault each      | `line`        | LINES, TIME        |
| clock read once at preparation; another clock used | emitter `wall_clock`, one fault each      | `timestamp`   | TIME               |
| first event object replayed for every line         | `emit_event` wrapper                      | `line`        | LINES, TIME        |
| `tags` with `capture` inverted on every phase      | both `_base_stage_tags` bindings          | `tags`        | LITERAL            |
| no emission when `on_line` is set                  | `_line_event_emitter`, `async` cells      | `line`        | LINES, FIELD count |
| no emission when capture is off; when echo is on   | `_line_event_emitter`, `async` cells      | `line`        | LINES, FIELD count |
| awaitable result dropped and closed                | `_observability._emit_observe_hook`       | `line`        | LINES, HOOK        |
| final drain skipped                                | both `_wait_for_exec_hook_tasks` imports  | `line`        | LINES, HOOK        |
| callback before hook                               | `_line_callbacks._compose_line_callbacks` | `order`       | ORDER              |

_Table 2: Seeded faults, the seam each uses, and the predicted rejection._

"FIELD count" means the rule reports zero examined items where `applies` says
it should have some. "Both imports" means the two names production calls:
`cuprum._command_finalization` and `cuprum._pipeline_finalize`. The "every
phase" tag fault is the one the `start` comparison cannot see, which is why the
literals exist. Where a fault wraps a coroutine it must close what it drops, so
no "never awaited" warning masks the result.

### Formal verification decision

None; see D6. Residual gaps, stated so they are not rediscovered as omissions:
`resolved_path` is `None` in the process-level matrix because the baseline tree
has no executable binding (EP-M1 and EP-M4 populate it in process);
`run_concurrent`, `run_bytes`, per-stream overrides, the stream backend, and
the deferred path at 256 tasks keep their existing tests; failing hooks are
covered by V1 and `test_line_event_emission_hook_contract.py`; early-closing
and broken-pipe pipelines keep theirs; Windows is skipped.

### Suggested BDD features

Behaviour-driven development (BDD) scenarios are proposed in
`tests/features/event_parity.feature`, bound by
`tests/behaviour/test_event_parity_behaviour.py`, and run by `make test`. They
use only the public API: no injected clock and no private seam. They advance
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
    And every line event carries a wall-clock timestamp in seconds since the epoch

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

  Scenario: A run that reads no output reports its lifecycle and no lines
    Given a command that writes known lines to both streams from a known directory and environment
    And an asynchronous observe hook that retains every event
    When the command runs with no capture, echo, or line callback
    Then the hook retains no line events
    And the hook retains one plan, start, exit, and settled event

  Scenario: Synchronous and asynchronous hooks receive the same events
    Given a two stage pipeline that writes known lines to both streams
    And a synchronous observe hook that retains every event
    And an asynchronous observe hook that retains every event
    When the pipeline runs with capture, echo, and line callback
    Then both hooks retain the same event objects for each stage and phase
```

The timestamp step asserts a `float` greater than 1e9, which separates epoch
seconds from monotonic readings without depending on the order of two clock
reads.

## Plan of work

### `EP-M1` Line-event parity stops comparing `None` with `None`

Assigned: GAP-NONE, REQ-LIVE (in process). Edit
`cuprum/unittests/test_line_event_emission_support.py` (`_make_observation`) and
`cuprum/unittests/test_line_event_emission_support_props.py` (`_observation`,
`_EmissionCase`, `_emission_case`) so the observations carry a real `Path`, a
non-empty `EnvOverlay`, `EnvMode.REPLACE`, a resolved path, and
`CaptureMode.BYTES`, and so the property draws them. Where a caller relies on
an absent value, keep that as an explicit named case. Add
`TestEveryHoistedFieldIsPopulated` to
`cuprum/unittests/test_line_event_emission_parity.py` as described under
"Methods per obligation", item 5.

Red: the guard fails on the unmodified fixtures, listing five fields. Then, in
a throwaway edit of `cuprum/_line_event_emitter.py`, apply each of the five
`None` mutants: before the fixture change each passes the focused modules,
after it each fails. Revert with
`git checkout -- cuprum/_line_event_emitter.py` and confirm
`git diff --quiet -- cuprum/_line_event_emitter.py`.

### `EP-M2` Every cell meets the specification and every fault is rejected

Assigned: REQ-MATRIX, REQ-SPEC, REQ-LIVE, GAP-MODE, GAP-TIME, GAP-WAIT. Create
the package `tests/helpers/event_parity/`:

- `matrix.py` — `HookKind`, `Subject`, `OutputMode`, `Cell`, `MATRIX`.
- `workload.py` — child scripts, literal lines, and builders. Every script
  starts by writing `stage-N.json` (pid, `os.getcwd()`, `PARITY`). The command
  then writes a word, an empty line, a non-ASCII word, and an unterminated tail
  to stdout and two words to stderr. The pipeline's first stage writes two
  words to stderr and one line to stdout; its second reads stdin to the end and
  then behaves like the command with its own texts. Children run as
  `sys.executable -I -c SCRIPT` and write bytes.
- `runner.py` — `run_cell` and the hook factories. It installs an injected wall
  clock that increases by one per read, gives every hook its own recorder, uses
  `io.StringIO` sinks, and returns a `CellRun`. The awaitable hook returns an
  object with `__await__`; for line events it waits on an `asyncio.Event` set
  when the last stage's `exit` has been delivered, then yields 200 loop turns,
  then records. The final `settled` cannot be the trigger: the baseline tree
  has none. The wait is wrapped in a ten-second `asyncio.timeout`, purely so a
  run that never emits `exit` fails with a message rather than hanging until
  pytest-timeout; no assertion depends on its value.
- `checks.py` — `check(run)`, `applies`, and `render`, with one small function
  per rule.

Add `cuprum/unittests/_event_parity_faults.py`,
`test_event_parity_seeded_faults.py`, and `test_event_parity_matrix.py`.

Red: write the fault tests first against a `check` that reports nothing. Mark
the rejection rows `xfail(strict=True)`; leave the "accepted in unaffected
cells" half of the mode-coupled faults unmarked, because it already passes.
Every marked row fails as expected. Green: implement one rule at a time,
removing markers as faults are rejected. No marker may remain. Then the matrix
passes on unmodified production code.

### `EP-M3` Every cell equals the record from before the optimizations

Assigned: REQ-EQUIV, GAP-HIST. Add `tests/helpers/event_parity/trace.py`
(`project`, `differences`, `trace_name`) and `capture.py`, and unit tests of
`project` and `differences` over constructed events inside
`test_event_parity_seeded_faults.py`.

Red: add the baseline assertion to the matrix items and the trace assertions to
the projected-field faults; both fail because
`tests/fixtures/event_parity/pre-5-2-1-traces.json` is missing. Capture it as
shown in `Concrete steps`. Green: 64 cells pass with no production change, and
every projected-field fault makes a trace differ.

`capture.py` runs all 64 cells, refuses to write unless every hook of every
cell with one trace name projects identically, and stores four traces with a
provenance object (source commit, `git archive` digest, interpreter
`major.minor`, and a literal command template, not `sys.argv`). In traces each
script appears as `<script:NAME>`, so reformatting a script's builder does not
falsify history; changing what a script does is a workload change (D2).

`matrix.py`, `workload.py`, `runner.py`, `trace.py`, and `capture.py` must
import under `7b86b904`. They use only names present there; `checks.py` need
not. The one difference, which modules own the clock, is an argument: tests pass
`cuprum._command_internals` and `cuprum._pipeline_observation`; the capture
command names `cuprum._pipeline_internals` for `7b86b904` and
`cuprum._pipeline_observation` for `4419c7c6`. This is a call-site argument,
not a compatibility layer.

### `EP-M4` Generated workloads keep the same payloads

Assigned: REQ-SPEC (`INV-GEN`). Add
`cuprum/unittests/test_event_parity_properties.py`, one module of at most 250
lines, strategies included. Drive `_compose_observation_line_callback` through
`_consume_stream` with `_reader` and `_config` from
`cuprum/unittests/_stream_drain_support.py`. Draw one to four hooks from the
three single kinds; the callback as absent, synchronous, or
awaitable-returning, the shape `SafeCmd.lines()` installs; capture and echo;
and the metadata EP-M1 populates. Lines exclude that module's
`_LINE_BOUNDARY_CHARACTERS`; line splitting has its own properties. The
awaitable kind here does not park, since no `exit` event occurs; the test awaits
`_wait_for_exec_hook_tasks` itself.

Red: run the property once with the `env` fault installed, record the shrunken
counterexample here, and keep that run as the negative-control test.

### `EP-M5` The behavioural feature passes through the public API

Assigned: REQ-MATRIX and REQ-SPEC at the public boundary. Add the feature file
and `tests/behaviour/test_event_parity_behaviour.py`; keep every `assert` in
that module, since Ruff does not exempt `_*_steps.py` there. Reuse
`workload.py` for the commands and nothing else from the harness, so the
scenario stays an independent witness. Red: bind the scenarios before writing
steps; pytest-bdd reports the missing step definitions.

### `EP-M6` Documentation and delivery closure

- `docs/developers-guide.md`: "Proving event parity across hooks and output
  modes" after "Verifying dispatch against a specification oracle", about 70
  lines: what the matrix is, the two oracles, how to read a failure, how to add
  a hook kind or mode, and the baseline lifecycle (D2).
- `docs/cuprum-design.md` §8.1.3: one paragraph naming the suite, the baseline
  commit, the clock invariant with its per-child term, and D2's lifecycle.
- `docs/repository-layout.md`: a module table beside the dispatch harness.
- `docs/users-guide.md`: no change. No behaviour or interface changes, and a
  sentence claiming payloads do not depend on mode would be false: `tags`
  records the mode. Record this conclusion in `Outcomes & retrospective`.
- `docs/roadmap.md`: repair the broken line wrap in item 5.2.3, mark it done,
  and record the outcome.
- `CHANGELOG.md`: no entry; nothing a user installs changes.
- Reconcile this plan's discoveries with §8.1.3, then set the status to
  `COMPLETE`.

## Milestones and plateaus

For each milestone the conformance check is the same: the assigned requirements
are met; §8.1.3 is still followed; no upstream assumption was falsified; no
public interface, dependency, trust boundary, or persisted format changed;
trace links are current. Compatibility decision for every milestone: none —
everything added is test-only.

- `EP-M1`: existing parity tests discriminate on every hoisted field.
  Acceptance: five mutants flip from passing to failing. Recovery: revert the
  commit. Remaining: modes, real runs, history.
- `EP-M2`: 64 cells and four `lines()` items meet the specification; every fault
  is rejected as predicted. Acceptance: no `xfail` remains, the snapshot is
  stored, and the stability runs are clean. Recovery: delete the new files; no
  existing file depends on them. Remaining: history, generated inputs.
- `EP-M3`: 64 cells equal the pre-5.2.1 record. Acceptance: two captures compare
  equal, `4419c7c6` gives the same traces, and the matrix passes. Extra check:
  `AXIOM-HISTORY` re-verified. Recovery: delete the fixture, `trace.py`,
  `capture.py`, and the baseline assertions; EP-M2 stands alone. Remaining:
  generated inputs.
- `EP-M4`: the property passes and its control fails as intended. Recovery:
  delete one module. Remaining: the public-boundary feature.
- `EP-M5`: the feature passes. Recovery: delete two files. Remaining:
  documentation.
- `EP-M6`: documents agree with the tree; roadmap ticked; all gates pass.
  Recovery: revert the documentation commit. Remaining: none.

## Concrete steps

Working directory: the repository root of this branch's worktree. Run
`make build` once. Log names follow
`/tmp/$ACTION-cuprum-5-2-3-combinatorial-event-parity-suite.out`.

Capture (EP-M3), using a scratch directory outside the repository:

```bash
set -o pipefail
REPO=$PWD
SCRATCH=$(mktemp -d)
mkdir "$SCRATCH/pre" "$SCRATCH/mid" "$SCRATCH/out"
test "$(git rev-parse 71aaf3ebe^)" = 7b86b904b99140c041c28a6258efd4fb4c8d6186
test "$(git rev-parse c0e871fc5^)" = 4419c7c65ca514d52f10a034dd6756173500542a
git archive 7b86b904 cuprum | tar -x -C "$SCRATCH/pre"
git archive 4419c7c6 cuprum | tar -x -C "$SCRATCH/mid"
capture() {  # tree commit pipeline-clock-module output
  PYTHONPATH="$1:$REPO" PYTHONDONTWRITEBYTECODE=1 \
    uv run --no-project --python 3.14 python -P \
    -m tests.helpers.event_parity.capture --tree "$1" --commit "$2" \
    --clock-module cuprum._command_internals --clock-module "$3" --output "$4"
}
capture "$SCRATCH/pre" 7b86b904 cuprum._pipeline_internals "$SCRATCH/out/a.json"
capture "$SCRATCH/pre" 7b86b904 cuprum._pipeline_internals "$SCRATCH/out/b.json"
capture "$SCRATCH/mid" 4419c7c6 cuprum._pipeline_observation "$SCRATCH/out/m.json"
cmp "$SCRATCH/out/a.json" "$SCRATCH/out/b.json"
diff <(jq -S .traces "$SCRATCH/out/a.json") <(jq -S .traces "$SCRATCH/out/m.json")
```

Expected: no output from `cmp` or `diff`. Copy `a.json` to
`tests/fixtures/event_parity/pre-5-2-1-traces.json`, then remove `$SCRATCH`.

Focused loops:

```bash
uv run pytest -q cuprum/unittests/test_line_event_emission_*.py
uv run pytest -q cuprum/unittests/test_event_parity_matrix.py \
  cuprum/unittests/test_event_parity_seeded_faults.py
uv run pytest -q --hypothesis-show-statistics \
  cuprum/unittests/test_event_parity_properties.py
uv run pytest -q tests/behaviour/test_event_parity_behaviour.py
```

Stability (EP-M2, repeated after EP-M3). A failure stops the loop with a
non-zero status. Zero failures in 150 runs bounds the per-run failure rate at
about 2% (95% confidence), so it is evidence and not proof:

```bash
for n in $(seq 100); do
  uv run pytest -q cuprum/unittests/test_event_parity_matrix.py || exit 1
done
for n in $(seq 50); do
  taskset -c 0,1 uv run pytest -q cuprum/unittests/test_event_parity_matrix.py ||
    exit 1
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

1. `uv run pytest -q cuprum/unittests/test_event_parity_matrix.py` reports 64
   cell items and four `lines()` items passing, plus its guards.
2. Editing `_LineEventEmitter.emit_line` to pass `env=None` fails
   `make test-python` with a report naming `env`; before this work nothing
   failed.
3. Editing `_line_event_emitter` to return `None` when `context.on_line` is set
   fails exactly the cells whose callback is on.
4. Replacing the final hook drain with a no-op fails every non-silent cell with
   an `awaitable` or `mixed` hook.
5. Two captures of `7b86b904` compare equal and equal the committed traces.
6. `make test` passes; the new modules add less than 60 s serially on two
   processors.

Quality criteria: every rule has at least one seeded fault rejected for the
predicted reason; no `xfail`; no `skip` other than Windows; no lint suppression
without a stated reason; all gates pass; the stability runs are clean.

## Idempotence and recovery

Every step can be repeated. The capture writes only to the path it is given.
Throwaway mutants are made one at a time and reverted with
`git checkout -- <file>`; confirm a clean `git status --short` before
committing. Never use `git stash` in this worktree; the stash is shared. If a
stability run fails, keep its log, stop, and record the cell and rule in
`Surprises & discoveries` before changing anything.

## Artefacts and notes

Planning-time evidence, all from scratch copies outside the repository, at
`c966dbf14`.

Historical equivalence probe; each capture covers 64 cells. "Pinned" runs used
`taskset -c 3`.

```plaintext
pre-5.2.1 7b86b904, Python 3.14          1 capture
pre-5.2.2 4419c7c6, Python 3.14          1 capture
HEAD c966dbf14, Python 3.12, pinned      5 captures
HEAD c966dbf14, Python 3.14, pinned      5 captures
md5sum of all 12 outputs: 1 distinct value
elapsed per capture: 1.3 s to 1.9 s
```

Mutants of `cuprum/_line_event_emitter.py`. "Focused" is 161 tests
(`test_line_event_emission_*`, `test_observe*`, `test_settled_hook_dispatch`,
`test_line_observe*`, `test_line_events`, and two behaviour modules). "Wider"
is the 518 modules under `cuprum/unittests/` and `tests/behaviour/` that do not
mention a build tool; its unmutated baseline in the scratch copy was 4,407
passed, 25 environment-caused failures, and two collection errors, and only
_new_ failures are counted.

```plaintext
mutant               focused           wider, new failures
env=None             161 passed        0
cwd=None             161 passed        1 (executable-binding paths)
resolved_path=None   161 passed        1 (executable binding)
capture_mode=None    161 passed        2 (capture-mode logging)
env_mode=None        161 passed        4 (environment policies)
project=None           2 failed        not run
tags={}                6 failed        not run
exec_id=None           7 failed        not run
pid=None               9 failed        not run
argv without program   7 failed        not run
monotonic clock       12 failed        not run
frozen clock           5 failed        not run
```

Waiting probe: with `_wait_for_exec_hook_tasks` replaced by a no-op, line
records delivered by a hook that parks until `exit` and then yields 200 turns:

```plaintext
tree       Python       command, non-silent   pipeline, non-silent
7b86b904   3.12, 3.14   0 of 3 recorded       0 of 4 recorded
4419c7c6   3.12, 3.14   0 of 3 recorded       0 of 4 recorded
c966dbf14  3.12, 3.14   0 of 3 recorded       0 of 4 recorded
unmutated: every record present on all three trees
```

A hook that yields once, or that parks without yielding afterwards, recorded
everything under the same mutant.

Fault seam probe: replacing `_line_callbacks._line_event_emitter` with a
wrapper returning `dataclasses.replace(emitter, cwd=None)` was applied twice
per command (stdout and stderr) and changed both the projected trace and the
comparison with `start`. `capture_mode=None` and `env_mode=None` changed only
the latter, as D3 intends.

Prior art consulted: NIST's combinatorial-testing guidance (covering arrays
matter when the full product is unaffordable; here it is not); Python's
event-loop documentation for callback order; syrupy's documentation, which says
`--snapshot-update` rewrites snapshots to match assertions and deletes unused
ones.

## Interfaces and dependencies

No new dependency. New test-only interfaces, owned by
`tests/helpers/event_parity/` and used only by the modules named in this plan:

```python
# matrix.py
class HookKind(enum.StrEnum): ...      # SYNC, ASYNC, AWAITABLE, MIXED
class Subject(enum.StrEnum): ...       # COMMAND, PIPELINE, LINES

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

MATRIX: tuple[Cell, ...]               # 64 cells; COMMAND and PIPELINE only
LINES_ITEMS: tuple[Cell, ...]          # 4 specification-only items

# runner.py
@dc.dataclass(frozen=True, slots=True)
class StageWitness:
    pid: int
    cwd: str
    parity: str | None

@dc.dataclass(frozen=True, slots=True)
class JournalEntry:
    source: typ.Literal["hook", "callback"]
    stream: str
    text: str

@dc.dataclass(frozen=True, slots=True)
class CellRun:
    cell: Cell
    workdir: Path
    events_by_hook: cabc.Mapping[str, tuple[ExecEvent, ...]]
    journal: tuple[JournalEntry, ...]
    clock_readings: tuple[float, ...]
    callback_lines: tuple[LineEvent, ...]
    echoed: tuple[str, str]
    started_at: tuple[float, ...]      # one per stage, from the result
    captured: tuple[str | None, str | None]
    witnesses: tuple[StageWitness, ...]

def run_cell(
    cell: Cell, workdir: Path, clock_modules: tuple[types.ModuleType, ...]
) -> CellRun: ...

# checks.py
class Rule(enum.StrEnum): ...          # FIELD, LITERAL, LINES, TIME, HOOK, ORDER, MODE

@dc.dataclass(frozen=True, slots=True)
class Violation:
    rule: Rule
    field: str
    location: str                      # hook, stage, phase, event index
    expected: str
    actual: str

@dc.dataclass(frozen=True, slots=True)
class Report:
    violations: tuple[Violation, ...]
    examined: cabc.Mapping[Rule, int]

def applies(rule: Rule, cell: Cell) -> bool: ...
def check(run: CellRun) -> Report: ...
def render(cell: Cell, found: cabc.Sequence[Violation]) -> str: ...

# trace.py
class TimestampVerdict(enum.StrEnum): ...   # FRESH, REUSED, FOREIGN

@dc.dataclass(frozen=True, slots=True)
class TraceEvent:
    phase: str
    program: str
    argv: tuple[str, ...]
    cwd: str | None
    env: tuple[tuple[str, str], ...]
    pid: str | None
    line: str | None
    timestamp: TimestampVerdict

type Trace = tuple[tuple[TraceEvent, ...], ...]   # one tuple per stage

@dc.dataclass(frozen=True, slots=True)
class Difference:
    stage: int
    index: int
    field: str
    expected: str
    actual: str

def trace_name(cell: Cell) -> str: ...            # subject, plus "-silent"
def project(run: CellRun) -> cabc.Mapping[str, Trace]: ...   # by hook
def differences(expected: Trace, actual: Trace) -> tuple[Difference, ...]: ...
```

Within a stage, a trace lists `plan`, `start`, stdout lines, stderr lines, then
`exit`, so the unordered interleaving of two streams cannot affect it. Where
Ruff's argument or local limits bind, group values in the dataclasses above
rather than suppressing the rule.

Reused as they are: `tests.helpers.catalogue.python_catalogue`; `_fields` and
`_make_observation`; `_reader`, `_config`, and the test copy of
`_LINE_BOUNDARY_CHARACTERS` in `cuprum/unittests/_stream_drain_support.py`. Not
reused, with reasons: `normalize_event` in
`tests/behaviour/_structured_events_support.py` masks pid, timestamp, and the
programme, which are under test; `mismatch` in
`_observe_hook_dispatch_compare.py` takes an event from its caller and judges
only its dispatch; `_run_observed` in `test_line_observe_support.py` is built
for bounded-delivery floods.

Reuse policy: `tests/helpers/event_parity` is for event-payload equivalence
across execution modes. Tests of the dispatcher alone extend the 5.2.2
specification; tests of retention extend `test_line_observe_support.py`.
`tests/helpers/` is outside `PYLINT_TEST_TARGETS`, so its modules keep the
400-line cap.

## Revision note

- 2026-11-25: initial draft from reconnaissance and feasibility probes.
- 2026-11-25: expert review applied. The panel was structure and contracts,
  failure modes and cost, alternatives and viability, and a separate fact
  check. Changed: pids are witnessed by files rather than printed lines,
  because silent cells print nothing; the awaitable hook reaches the general
  dispatch branch and parks, because a one-yield hook proved nothing about
  waiting; the baseline lost its cell index and digest pin and gained a
  lifecycle that lets the matrix grow; the specification and faults now precede
  the baseline; fault expectations are exact rule sets; events are attributed
  non-circularly; `tags` and modes gained literals; children run isolated from
  `PYTHON*` variables; the matrix modules were merged, the field lists,
  generation module, and backend slice dropped, and the child budget corrected;
  the wall-clock scenario no longer brackets the system clock; citations to
  5.2.2 and the design document were corrected; the claim about surviving
  mutants was narrowed to what the wider run supports. Remaining work:
  unchanged in scope; milestone order changed as D9 records.
