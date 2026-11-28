# Measure and reduce pytest orchestration overhead, and qualify selective xdist

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS. Stage 1 (baseline and classification) recorded; the runner
itself is unchanged.

## Purpose / big picture

Issue #607 asks whether Cuprum's `make test-python` pays for process boundaries
it does not need. The selector currently starts 31 separate pytest processes
for one CI test step. Each process re-imports the interpreter, `pytest`, its
plugins, the root `conftest.py`, and every `conftest.py` on the path to the
selected files, and each re-collects what it will run. The hypothesis is that
this repeated start-up, plugin initialization and collection costs more than
some of the boundaries are worth.

The hypothesis is unmeasured. A serial Cargo constraint - several batches
compile or reuse one `rust/target` directory, so they must not run concurrently

- does not prove that 31 *sequential* boundaries are necessary, and neither
does it prove they are dispensable. This plan therefore does not assume a
saving. It establishes one, or records that it does not exist.

Two changes are evaluated, separately and in order:

1. **Serial consolidation (Stage 2A).** Merge a small number of compatible
   batches into one pytest process, keeping every run serial
   (`PYTEST_WORKERS=0`).
2. **Selective two-worker execution (Stage 2B).** Run a proven-independent
   subset under `pytest-xdist` with exactly two workers, in a separate phase of
   the same job. Stage 2B is attempted only if Stage 2A establishes that such a
   subset exists.

After the change, a contributor or CI job either runs the same tests through
fewer processes with evidence that fidelity is preserved, or runs them exactly
as today with a committed record saying why consolidation was rejected or
deferred. Retaining the current layout is a valid, expected outcome.

## Constraints

- **No new paid capacity.** The experiment is measured inside existing required
  CI executions. No shadow or control jobs, no extra matrix legs, no larger
  runners, no synthetic push or `workflow_dispatch` loops, and no new
  schedules. When ordinary activity cannot supply adequate evidence, adoption
  is deferred rather than the evidence manufactured.
- **Coverage and isolation are not negotiable.** No test is skipped, weakened,
  retried, or made to share an artefact whose purpose requires a fresh build.
  Assertions, fixture isolation guarantees, Hypothesis profiles and example
  budgets, warning filters and timeout allowances are preserved unchanged.
- **Environment separation is preserved.** The pure-Python environment, the
  native-extension environment (`CUPRUM_REQUIRE_RUST_EXTENSION`), the opt-in
  `act` scenario suite (`ACT_SCENARIO_TARGETS`) and the parser half
  (`ACT_PARSER_TARGETS`, which is in the default suite) keep their existing
  boundaries. No selection is replaced by an indiscriminate `pytest .`.
- **One variable at a time.** Batching changes are never mixed with worker
  count, cache backend, toolchain, dependency, fixture-scope, or
  production-code changes in the same comparison.
- **Collection fidelity.** Before and after collected node-ID multisets,
  parametrizations, and required executed outcomes must match for each lane. A
  pre-existing duplicate collection or selection gap is a separately reviewed
  correctness change, not an unannounced performance saving.
- **No `-n auto`.** Stage 2B uses exactly two workers, chosen by measurement,
  never `auto`.
- **ExecPlan files are excluded** from any source-line or change-size budget.
  They are documentation, not code.
- English prose uses en-GB-oxendict spelling (`-ize`, `-yse`, `-our`). Code
  identifiers, comments and docstrings follow the same rule except where an
  external contract requires otherwise.

## Tolerances (exception triggers)

- **Scope.** Stop and escalate if a batch cannot be classified as merge
  candidate or fresh-process-required from code and observation. "Unknown" is
  not a licence to merge: it is recorded as fresh-process-required, and the
  plan proceeds without that batch.
- **Interface.** Stop and escalate if `PYTEST_TARGETS`, `ACT_PARSER_TARGETS`,
  `ACT_SCENARIO_TARGETS`, or `EXTENSION_TEST_TARGETS` must change their
  *contents*. The driver must consume the existing selectors, not replace them.
- **Dependencies.** Stop and escalate if a new runtime dependency is required.
  `pytest-xdist` is already a development dependency; the driver, the evidence
  plugin and the analysis tool are written against the standard library and
  already-pinned tools.
- **Cost.** Stop and escalate if any candidate implies incremental paid
  runner-seconds, a new artefact charge, or a new cache family. A shorter
  pytest log line is not by itself a saving.
- **Evidence.** If fewer than five comparable warm pairs per interpreter can be
  accumulated from ordinary activity, the arm is reported `inconclusive` and is
  not enabled. No further runs are created to reach the target.
- **Budget.** Code changes (production, tests, executable automation) are
  expected to stay within 1,200 net lines; configuration within 150 net lines;
  documentation within 600 net lines. These are surveyed estimates, not
  measurements: the repository has no comparable prior experiment. Exceeding a
  budget is a decision-log entry, not a silent overrun.

## Risks

- **Merged collection hides an import collision.** Pytest imports test modules
  by basename in its default `prepend` import mode, and `tests/`,
  `tests/behaviour/`, `tests/integration/`, `scripts/tests/` and
  `cuprum/unittests/` carry no `__init__.py`. Two modules with the same
  basename in different directories collide only when one process imports both.
  Commit `24c3965` records exactly this: a new
  `cuprum/unittests/test_context_hooks.py` collided with
  `tests/behaviour/test_context_hooks.py`, and only the whole-repository
  `coverage` job noticed. Mitigation: a fidelity check that collects the whole
  repository in one process and requires zero `import file mismatch` errors,
  plus a basename-uniqueness contract over the selected population.
- **Session fixture lifetimes change.** A session fixture lives for one pytest
  process. Merging batches makes one process serve several batches, so a
  fixture that was recreated per batch is now created once. If any fixture is
  not idempotent, or leaks state through a module-level cache, a merged run can
  pass where separate runs failed - or fail where separate runs passed.
  Mitigation: the per-batch audit in this plan, plus running each candidate
  group alone, in the proposed order, and in reverse order.
- **Consolidation buys nothing.** Thirty-one interpreter starts may cost only a
  few seconds against a multi-minute job. Mitigation: the predeclared minimum
  saving, and the explicit right to reject.
- **The measurement itself distorts the result.** An evidence plugin that
  records per-node durations adds overhead per session, and a wrapper adds a
  process per invocation. Mitigation: measure the instrumentation's own cost
  with alternating runs and record it; keep every record bounded.
- **CI evidence is confounded.** Warm and cold caches, different interpreter
  legs, and cancelled or retried runs make pairs incomparable. Mitigation:
  predeclared pairing rules that refuse to pool interpreters or cache states,
  and an explicit `inconclusive` state.
- **xdist duplicates session fixtures.** Each worker collects and may execute
  session fixtures, so a session-scoped build is not one-per-job. Mitigation:
  Stage 2B excludes every compiler-invoking module and selects `load`,
  `loadscope` or `loadfile` from measured fixture locality.

## Progress

- [x] (2026-11-22) Recorded the implementation baseline at
  `87de64c2cad4e970984760096faf278bd70439f3` (2026-11-22) and resolved the
  selector to batches and files.
- [x] (2026-11-22) Recorded the lanes, runner shape and time budgets.
- [x] (2026-11-22) Recorded the git-history reason for the three alphabetical
  behaviour batches.
- [x] (2026-11-22) Predeclared the minimum useful saving and the pairing
  rules.
- [x] (2026-11-22) Built the per-batch audit table and classified every batch.
- [x] (2026-11-22) Recorded the code-based compiler builds and the mocked
  seams.
- [x] (2026-11-22) Added `COMPILER_INVOKING_MODULES` and
  `FRESH_PROCESS_MODULES` with a validating contract. They are defined in
  `tests/helpers/suite_classification.py` and re-exported from
  `tests/helpers/suite_selection.py`, because that module is at the 400-line
  ceiling the Pylint gate enforces on `tests/`.
- [ ] Stage 1: profile every pytest invocation end to end.
- [x] (2026-11-23) Implemented the opt-in evidence plugin, the `wrap`
  subcommand and the layout driver.
- [x] (2026-11-23) Proved the `baseline` layout is selection-identical to the
  legacy loop: 31 groups, 681 files across groups (679 distinct; the deselected
  module appears in its glob group and again in the grouped one), identical
  order, verified against `make -n test-python`.
- [ ] Stage 2A: qualify the consolidated group locally.
- [ ] Stage 2A: accumulate matched CI pairs and decide.
- [ ] Stage 2B: attempt only if Stage 2A names an independent subset; otherwise
  record "not attempted".
- [ ] Commit the decision record (ADR) and remove unused experimental wiring.

## Surprises & discoveries

- The issue text cites "eleven default `PYTEST_TARGETS` entries" and this plan's
  brief cites 21 batches at `f868a6f`. Both were true when written. At
  `24c3965` the variable holds 11 entries; at `f868a6f` it holds 21; at the
  implementation baseline `87de64c2c` it holds 32. The growth is new singleton
  entries (`tests/test_distribution_*.py`, `scripts/tests/test_cargo_*.py`,
  `scripts/tests/test_prepare_cargo_state.py`,
  `tests/test_rust_pump_writer_handoff_terminal_paths.py`), each of which adds
  another process. The case for consolidation strengthened while the issue sat
  open.
- The repository's root `conftest.py` carries seven duplicated copies of its own
  module docstring and `TYPE_CHECKING` block, injected by commit `0b026a1a3`
  (2026-09-05). They are inert string expressions, so they change no behaviour,
  but they are parsed and compiled in every one of the 31 processes. They are
  recorded here and removed in a separate announced commit, not folded into the
  performance change.
- No two test modules in the repository share a basename (689 modules checked),
  so merged collection is currently collision-free. That is a fact to guard,
  not a property to assume.
- No test module writes `os.environ` directly; all 105 environment mutations in
  the selected trees go through `monkeypatch`, which restores on teardown.
  `tests/behaviour/` modules install no signal handlers and the only socket in
  the selected trees binds an ephemeral port (`127.0.0.1:0`).
- Nearly every pytest process pays for the same Cargo-registry and tool
  imports, but the expensive session fixtures are lazy: `prepared_cargo_run` and
  `distribution_artefact_factory` are requested only by the packaging modules.
  This is the main mechanism by which merging helps, and it helps only where
  several processes each pay that set-up.
- The Make recipe's `--deselect` is per invocation, not global. The three
  `PYTEST_GROUPED_TARGETS` modules are removed from the individual loop *and*
  run together in a final command, so a driver that applied the deselection to
  every process would lose them entirely. The baseline layout therefore carries
  the deselections on the individual groups and not on the grouped one. The
  first version of the layout got this wrong and over-counted by one file; the
  comparison against `make -n` caught it.
- The fixed cost of one pytest process is larger than a bare interpreter start
  by two orders of magnitude: 20 ms for `python -c pass`, 253 ms for
  `pytest --version`, and about 1.7 s to collect a single test module. The cost
  is plugin and `conftest.py` initialization plus collection, not interpreter
  start-up, which is why merging batches is worth measuring at all.
- Local execution of the compiler-invoking batches is not a usable timing
  signal. On a host shared with other agents, compile admission queues the
  build, so `tests/test_py_typed_marker.py` measured 724.5 seconds of wall time
  against 21.3 seconds of child CPU (97 per cent waiting) while other agents
  held every compile slot. Local screening therefore measures collection, and
  execution evidence comes from CI.

## Decision log

- **2026-11-28 - The code budget is exceeded, deliberately.** The Tolerances
  section budgeted 1,200 net code lines. The change adds roughly 5,000, because
  review required the measurement instrument to fail closed: complete-evidence
  preconditions, multiset and outcome fidelity, a loader separated from the
  rule, and executable contracts. A cheap instrument that can report `adopted`
  on absent evidence is worse than none, so the budget yields to correctness.
  The overspend is recorded here rather than absorbed silently.
- **2026-11-28 - Retained wiring after a deferral, on review advice.** This
  plan's Stage 5 says to remove unadopted experimental wiring after a deferral.
  ADR-0030 retains it instead, and CodeRabbit's assessment endorsed retaining
  it only if the verdict fails closed and the scope is recorded as
  deferral-only. Both now hold: `decide` refuses a verdict unless every
  precondition is met, and the ADR states the deferral explicitly. The rollback
  in ADR-0030 lists every file to delete if the work is abandoned.

- **2026-11-22 - Baseline is the current `main`, not the issue's citation.**
  Issue #607 quotes `24c3965` (11 entries). The implementation baseline is
  `87de64c2c` (32 entries, 31 processes, 679 files). Measuring the citation
  would describe a selector that no longer exists. Recorded here and in the
  implementation pull request.
- **2026-11-22 - Keep `PYTEST_TARGETS` as the single selector.** The driver
  reads the existing variable; it does not introduce a second source of truth
  for what the suite collects. This preserves the #499 guard, which asserts
  that the selector agrees with the module population.
- **2026-11-22 - Stage 2B is gated, not scheduled.** It is attempted only if
  Stage 2A's decision record names a proven-independent subset. Otherwise the
  plan records "not attempted" and stops.
- **2026-11-22 - `conftest.py` duplication is a separate commit.** It is a
  pre-existing defect unrelated to process boundaries. Folding it into the
  performance change would confound the comparison and hide a correctness fix
  inside a speed-up.

## Context and orientation

The repository is Cuprum, a Python command-execution library with a Rust
extension. Tests live in five trees: `cuprum/unittests/` (unit tests), `tests/`
(root-level contract modules), `tests/behaviour/` (pytest-bdd behavioural
tests), `tests/integration/` (the `act` harness), and `scripts/tests/` (tests
for the repository's own tooling).

`Makefile` target `test-python` runs the Python suite. It depends on
`test-selection` (which checks the selector against the module population),
`build`, `uv`, `$(VENV_TOOLS)` and `makeutil`, then executes a shell loop: for
each entry in `PYTEST_INDIVIDUAL_TARGETS`, it skips entries whose first glob
expansion does not exist and otherwise runs one `pytest` process for that
entry. It then runs one further process for `PYTEST_GROUPED_TARGETS`.

`.github/workflows/ci.yml` job `typecheck-test` is the only job that runs
`make test-python`. It has three interpreter legs (3.12, 3.14 and an
experimental 3.15.2), runs on `ubicloud-standard-2` with
`LINUX_RUNNER_VCPUS: '2'`, has `timeout-minutes: 45`, and passes
`CARGO_BUILD_JOBS="${LINUX_RUNNER_VCPUS}"` so Cargo cannot fan out past the
billed vCPU count. `pyproject.toml` sets `timeout = 30` for every test through
`pytest-timeout`. `PYTEST_WORKERS ?= 0` keeps pytest itself serial.

`coverage` runs a bare pytest for coverage collection; `extension-tests` and
`extension-tests-windows` run `make test-extension`; `verus` runs
`pytest scripts/tests/test_boundary_*.py`. Those lanes are observed, not
retargeted.

`tests/helpers/suite_selection.py` resolves the selector against the tree and
owns the claims about what the suite collects. `tests/helpers/makefile.py`
reads the Makefile through `makeutil`. `tests/helpers/recipe_flow.py` asserts
that a target's recipe consumes its selector. The new classification data
belongs in `suite_selection.py` beside the existing selector policy.

## Conformance basis

There is no Terms of Reference or technical-design identifier for this work.
The governing artefacts are:

- Issue #607, "Experiment: reduce pytest session churn and qualify selective
  xdist with CI evidence", including its Stage 1, Stage 2A, Stage 2B,
  CI-evidence, and fidelity checklists.
- Repository `AGENTS.md`: change quality, atomic commits, spelling, Markdown,
  Python testing, and observability rules.
- `docs/scripting-standards.md`: the `Cyclopts`, `cuprum`, `pathlib` and
  `cmd-mox` conventions the driver and tooling must follow.
- `docs/developers-guide.md`: the "One execution per suite" and test-selection
  sections, which the decision record updates if a layout is adopted.
- Issue #499 (default-suite collection gaps), issue #452 (a compiler-heavy test
  and its local timeout), issue #258 (native-boundary execution requirements),
  and issue #606 (evidence format). These are related, not replaced: this work
  must not reintroduce their defects or claim to solve them by rebatching.

## Verification plan

The change introduces no new production invariant over untrusted input. Its
obligations are about *orchestration fidelity*: the suite that runs after the
change must be the same suite that ran before, with the same isolation.

- **Invariant F1 - selection identity.** The node-ID multiset collected by the
  adopted layout equals the multiset collected by the `baseline` layout, for
  each interpreter lane. Method: a `fidelity` subcommand that collects both
  layouts, compares sorted node IDs and parametrizations, and compares the
  required executed outcomes. Non-vacuity: the comparison is run against a
  deliberately altered layout and must report the difference; an empty
  collection is an error, not a pass.
- **Invariant F2 - disjoint and complete routing (Stage 2B only).** The serial
  selection and the parallel selection are disjoint and their union is the
  required suite. Method: the same `fidelity` subcommand, plus a contract over
  the committed manifest. Non-vacuity: an unlisted module must fall into the
  serial set; a module listed in both must fail.
- **Invariant F3 - no import collision.** One process collecting the whole
  repository reports zero `import file mismatch` errors, and no two selected
  modules share a basename. Method: whole-tree `--collect-only` plus a basename
  contract. Non-vacuity: the check is exercised against a deliberately
  duplicated basename in a temporary tree, which must fail.
- **Invariant F4 - failure propagation.** A nonzero child status reaches
  `make` and CI under every adopted mode; a deliberately failing assertion and
  a deliberately broken collection each fail the run; a worker crash fails the
  run. Method: executable driver contracts using `cmd-mox` doubles and real
  pytest runs on tiny suites. Non-vacuity: the doubles are asserted to have
  been invoked, and the failing case is asserted to produce the expected
  nonzero exit and diagnostic, not merely a nonzero exit from something else.
- **Invariant F5 - coverage preserved.** Native-required modules still execute
  rather than silently skipping; `act` scenarios remain opt-in; the parser
  module remains in the default suite. Method: the existing `tests/test_ci_*`
  contracts extended, plus an executed-versus-skipped count comparison per lane
  in the evidence records. Non-vacuity: the comparison uses an environment
  where the extension is required (`CUPRUM_REQUIRE_RUST_EXTENSION=1`), where a
  missing extension is fatal.
- **Cost obligation C1.** Incremental paid runner-seconds are non-positive and
  no new cache or artefact charge is introduced. Method: offline whole-job cost
  accounting from GitHub job and step timestamps, with runner-seconds labelled
  a cost proxy unless Ubicloud billing and rounding are confirmed. Non-vacuity:
  failed, cancelled and retried work is counted in the cost, so a candidate
  cannot look cheaper by failing faster.
- **Predeclared adoption rule.** Before any CI exposure, this plan fixes the
  primary metric (the `make test-python` step's wall time), the pairing rules,
  and the minimum useful saving. See "Predeclaration" below.

### Predeclaration

Committed before the first CI run of the candidate layout, and not revised
afterwards. If a later measurement suggests a different threshold, that is a
new experiment, not an amendment to this one.

- Primary metric: wall time of the `Run tests` step (`make test-python`) in
  `typecheck-test`, taken from the job's step timestamps.
- Secondary metric: whole-job wall time, and total paid runner-seconds.
- Pairing: same interpreter leg, same cache-state class (warm, cold, or
  scratch-cold), same runner label and vCPU count, same lockfile and toolchain
  fingerprints; prefer an identical tested tree hash. Incomparable runs are
  reported as unpaired observations and never pooled. Baseline and candidate
  legs are assigned by `github.run_id` parity, so both arms accumulate from
  ordinary activity on the same workflow.
- Sample: at least five warm pairs per interpreter. Fewer is `inconclusive`.
- **Minimum useful saving: a median paired saving of at least 20 seconds *and*
  at least 1.5 per cent of the paired control `Run tests` step.** Local
  screening predicts 35-45 seconds for the consolidated layout, so 20 seconds
  is roughly half the expected effect: large enough that CI noise cannot
  produce it, small enough that a genuinely cheaper runner still qualifies.
- Per-pair regression tolerance: no pair may regress by more than 15 seconds.
  A single larger regression is reported and investigated rather than averaged
  away.
- Rejection: a median saving below the minimum, any pair regressing beyond the
  tolerance, any unexplained change in collected or executed results, or any
  incremental paid cost, rejects the arm.
- Cost: incremental paid runner-seconds must be non-positive, and no new cache
  or artefact charge may be introduced. Runner-seconds are labelled a cost
  proxy unless Ubicloud billing and rounding are confirmed.

### Stage 1 local screening results

Measured 2026-11-22 on the development host with `hyperfine`, warm, in the
project virtual environment. Collection only, because execution of the
compiler-invoking batches is dominated by compile-admission queueing on a host
shared with other agents and is therefore not comparable to CI.

| command                                                         | mean   | σ      |
| --------------------------------------------------------------- | ------ | ------ |
| `python -c pass`                                                | 20 ms  | 3 ms   |
| `python -m pytest --version`                                    | 253 ms | 69 ms  |
| collect `tests/integration/test_act_stream_parsing.py` (1 file) | 1.70 s | 0.28 s |
| collect `tests/behaviour/test_[a-h]*.py` (26 files)             | 3.55 s | 0.57 s |
| collect `tests/behaviour/test_[i-r]*.py` (12 files)             | 2.21 s | 0.31 s |
| collect `tests/behaviour/test_[s-z]*.py` (12 files)             | 2.86 s | 0.50 s |
| collect all three behaviour batches in one process              | 3.72 s | 0.45 s |

Three separate behaviour collections cost 8.62 s; one merged collection costs
3.72 s. The fixed per-process cost is therefore about 1.6-1.9 s, which is
consistent with a one-file module taking 1.70 s to collect at all. Across 31
processes that is roughly 50-59 s of fixed cost, of which a consolidation that
reaches eight processes can recover most.

The measurement also shows why the saving must be measured on the whole job
rather than on collection: collection is a small part of a step whose execution
phase runs the tests.

### Per-batch audit

Audited 2026-11-22 against the working tree. "Fixtures" records session- or
module-scoped fixtures, autouse fixtures, and hooks defined for the batch;
"Global state" records environment writes, working-directory changes, signal
handlers, and event-loop ownership.

| batch                                                                                                      | files | fixtures and hooks                                                                                      | global state                                                                         | subprocess and descriptors                                                    | classification                                                       |
| ---------------------------------------------------------------------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `cuprum/unittests/test_*.py`                                                                               | 524   | root `conftest.py` autouse cache clear; module-scoped fixtures in 7 modules; session-scoped probes in 3 | no direct `os.environ` writes; 25 modules use `monkeypatch`; one `monkeypatch.chdir` | 236 modules use `asyncio.run`, each owning its loop; children reaped per test | merge candidate, kept whole; contains 4 compiler modules             |
| `tests/test_ci_*.py`                                                                                       | 63    | one module-scoped fixture                                                                               | `monkeypatch` only                                                                   | reads workflow and Makefile text; no builds                                   | merge candidate                                                      |
| `tests/test_distribution_artefacts.py`, `tests/test_distribution_build.py`                                 | 2     | injected recording frontend and runner                                                                  | `monkeypatch` only                                                                   | build seams are doubles                                                       | merge candidate                                                      |
| `tests/test_native_pipeline_*.py` (6)                                                                      | 6     | one autouse fixture in `test_native_pipeline_liveness_runner.py`                                        | `monkeypatch` only                                                                   | drives Python children, no build                                              | merge candidate after the autouse fixture is shown not to leak       |
| `tests/test_process_state_*.py` (2)                                                                        | 2     | none                                                                                                    | `monkeypatch` only                                                                   | Python children                                                               | merge candidate                                                      |
| `tests/test_rust_pump_writer_handoff_terminal_paths.py`, `tests/test_running_command_children_contract.py` | 2     | none                                                                                                    | `monkeypatch` only                                                                   | Python children                                                               | merge candidate                                                      |
| `tests/test_native_sdist.py`, `tests/test_py_typed_marker.py`                                              | 2     | session artefact factory over `prepared_cargo_run`                                                      | Cargo home snapshot                                                                  | `uv build` and `maturin`                                                      | fresh process required                                               |
| `scripts/tests/test_boundary_*.py` (16)                                                                    | 16    | module helpers; one autouse fixture in the HTTPS module                                                 | `monkeypatch` only                                                                   | ephemeral listener on `127.0.0.1:0`; one module runs `cargo check`            | split: `test_boundary_workspace.py` fresh, the rest merge candidates |
| `scripts/tests/test_cargo_*.py` (7) and 4 further `scripts/tests` entries                                  | 11    | injected command runners and stub tools                                                                 | `monkeypatch` only                                                                   | `ruff` and `make --dry-run` only; no compiler                                 | merge candidate                                                      |
| `tests/behaviour/test_[a-h]*.py`, `[i-r]*.py`, `[s-z]*.py`                                                 | 50    | two session payload fixtures (pure strings); module-level platform skips                                | `monkeypatch` only; 5 modules use it                                                 | `asyncio.run` per test; Python children                                       | merge candidate                                                      |
| `tests/integration/test_act_stream_parsing.py`                                                             | 1     | none                                                                                                    | none                                                                                 | parses recorded `act` output                                                  | merge candidate                                                      |

Repository-wide facts the audit established:

- Zero duplicate module basenames across 689 `test_*.py` files, so merged
  collection is currently free of the collision recorded in `24c3965`. That is
  a fact to guard, not a property to assume.
- No test module writes `os.environ` directly; all 105 environment mutations in
  the selected trees go through `monkeypatch`, which restores on teardown.
- No test module installs a signal handler. Every signal use targets a child
  process (`os.kill`, `handle.signal`).
- One socket exists in the selected trees, bound to an ephemeral port.
- The only working-directory change is `monkeypatch.chdir` in
  `cuprum/unittests/test_executable_binding_relative_paths.py`, which restores.

### Compiler-invoking modules

Confirmed by tracing each call path to a real process spawn. The mocked seams
were excluded deliberately: `test_cargo_warning_fixture_probe.py`,
`test_dev_fast_prerequisites.py`, `test_nextest_route_floor.py`,
`test_workflow_lint.py`, `test_cargo_state.py`, `test_prepare_cargo_state.py`,
`tests/test_ci_build_wheels_maturin_step.py` and
`tests/test_ci_pure_python_wheel_build_step.py` all substitute the executable
or the runner, so they exercise a contract about the command, not a build.

| module                                                   | what it runs                        | spawn site                               |
| -------------------------------------------------------- | ----------------------------------- | ---------------------------------------- |
| `cuprum/unittests/test_maturin_build.py`                 | `python -m maturin build --locked`  | `tests/helpers/maturin.py`               |
| `cuprum/unittests/test_rust_pump_debug_abort.py`         | `maturin build --profile debug`     | `tests/helpers/maturin.py`               |
| `cuprum/unittests/test_doctest_warning_contract.py`      | `cargo test --doc`, pinned nightly  | the module itself                        |
| `cuprum/unittests/test_cargo_build_warnings_contract.py` | `cargo build`, `clippy`, `doc`      | `tests/helpers/cargo_warning_fixture.py` |
| `tests/test_native_sdist.py`                             | `uv build --sdist`, `maturin sdist` | `tests/helpers/distribution_build.py`    |
| `tests/test_py_typed_marker.py`                          | `uv build`, `maturin`               | `tests/helpers/distribution_build.py`    |
| `scripts/tests/test_boundary_workspace.py`               | `cargo check --frozen`              | `scripts/boundary_compile.py`            |

Three further modules read shared Cargo state through a real process without
compiling - `scripts/tests/test_boundary_contract.py`,
`scripts/tests/test_boundary_target_discovery.py` and
`cuprum/unittests/test_nextest_config_discovery.py` (`cargo metadata`,
`cargo nextest show-config`). They are recorded as requiring their own process
so a merged group cannot queue behind a build lock, but they are not
compiler-invoking and are not counted as such.

## Plan of work

### Stage 1 - profile the unchanged runner

Measure before changing anything. For every one of the 31 actual pytest
invocations, record the resolved argv, exit status, end-to-end wall time, and
`RUSAGE_CHILDREN` CPU and peak RSS. Separately capture collection duration and
the per-phase durations pytest reports. Record collected node IDs and
parametrizations per batch, duplicates across batches, executed outcomes,
deselections, skip and xfail reasons, and collection errors. Compare by
interpreter, never across interpreters.

The durable instrumentation arrives with the `CUPRUM_PYTEST_EVIDENCE_DIR`
plugin; the first profile uses a scratch harness so that the committed
instrumentation can be validated against it.

### Stage 2A - consolidate compatible batches, still serial

Select a small first group from the profile. Keep explicit fresh-process
boundaries for the compiler and isolation-sensitive modules. Retain a
`baseline` layout so local and CI comparison needs no branch switch. Keep
`PYTEST_WORKERS=0` throughout.

### Stage 2B - qualify selective two-worker execution

Only if Stage 2A names an independent subset. Run serial groups with `-n 0`,
then one invocation with `-n 2 --dist <strategy>`, in a separate phase of the
same job. Never overlap the phases. Never use `-n auto`.

### Decision

Analyse offline, write the ADR, adopt or reject each arm separately, and remove
every piece of experimental wiring the decision does not adopt.

## Concrete steps

Stage 1 is executed from the repository root. The scratch harness lives outside
the repository so its files cannot enter the format gates.

1. Time every invocation of the real Make loop by overriding `PYTEST` with an
   outer wrapper, which leaves the loop, its argv and its environment untouched:

   ```sh
   make test-python PYTEST='uv run /tmp/pytest607/wrapper.py'
   ```

2. Collect per-batch node IDs with the same selection and no execution:

   ```sh
   uv run pytest --collect-only -q -n 0 <batch files>
   ```

3. Record the whole-repository collection and require zero errors:

   ```sh
   uv run pytest --collect-only -q -p no:cacheprovider
   ```

4. Compare the collected node-ID multisets per interpreter and record any
   duplicate selection as a separate correctness finding.

## Validation and acceptance

- `make test-selection` passes, and the selector still resolves to the same
  module population.
- The `fidelity` tool reports identity between `baseline` and the adopted
  layout for every interpreter lane.
- Whole-tree collection reports zero `import file mismatch` errors.
- A deliberately failing assertion, a deliberately broken collection, and a
  simulated worker crash each produce a nonzero status under every adopted mode.
- `make check-fmt`, `make lint`, `make test`, `make markdownlint` and
  `make nixie` pass.
- The implementation pull request carries versioned JSON/CSV summaries, raw
  results, immutable CI run and job links, and a decision record with an owner,
  review date and rollback instructions.

## Idempotence and recovery

Every step is re-runnable. The driver writes its evidence with
write-to-temporary-then-replace, so an interrupted run leaves either the old
record or a complete new one, never a truncated file. A failed qualification
run is re-run from the same commit; no step mutates the working tree.

Rollback for an adopted layout is a single variable: setting
`PYTEST_LAYOUT=baseline` restores the legacy loop's grouping without reverting
the commit.

## Artefacts and notes

- Baseline selector resolution at `87de64c2c`: 32 entries; 30 loop
  invocations, all finding files; 1 grouped invocation; 31 pytest processes;
  679 files. That figure counts each file once across the whole run. An earlier
  note in this plan said 681; that number came from summing per-group
  expansions, which counts the deselected module once per individual group and
  again in the grouped invocation. 679 is the deduplicated count and is the one
  this plan treats as authoritative; neither figure has been re-derived at a
  later head.
- Batch provenance: the three `tests/behaviour/test_[a-h]*`, `[i-r]*` and
  `[s-z]*` batches were introduced by commit `02b8a3461` (2026-07-16, PR #153)
  in the "Stabilize local validation gates" section of that change, whose
  stated reason was that "pytest runs in smaller in-process batches" to stop
  validation tools exhausting process and thread resources in constrained
  worktrees. The split is a historical resource workaround, not an isolation
  requirement.

## Interfaces and dependencies

- `tests/helpers/suite_selection.py` gains `COMPILER_INVOKING_MODULES` and
  `FRESH_PROCESS_MODULES`, both mapping repository-relative module paths to a
  reason string, so classification is data that contracts can consume rather
  than a comment.
- `scripts/pytest_batches.py` gains `wrap` and `run` subcommands, following
  `docs/scripting-standards.md`.
- `tests/helpers/pytest_session_evidence.py` is the opt-in evidence plugin.
- `scripts/pytest_layout_evidence.py` gains `fidelity` and `compare`.

## Revision note

- 2026-11-23: Rebasing onto `origin/main` (`f62a510a1`) replayed the branch
  commit identically (`range-diff` reports `=`). Main's change moves the Rust
  consume path into `rust/cuprum-streams/src/consume.rs` and updates two design
  documents; it touches neither `PYTEST_TARGETS`, `conftest.py`, the Makefile,
  nor any module the process-boundary classification names. Nothing in it is
  pertinent to this work, so no branch update follows from it. The
  `test_ci_pytest_layout_contract.py` guard already fails if a classified
  module stops existing or stops being selected, so drift of that kind cannot
  pass silently.

- 2026-11-23: Implementation of Stage 2A's instrument. Adds the evidence
  plugin, the `wrap` subcommand, the layout resolver and the phase planner, and
  records the per-group deselection discovery. No layout other than `baseline`
  is enabled, and `test-python` is unchanged.
- 2026-11-22: Initial draft. Records the refreshed baseline (`87de64c2c`), the
  four batch counts across revisions, the lanes and budgets, the git-history
  reason for the alphabetical behaviour batches, and the plan structure.
- 2026-11-22: Stage 1 screening recorded. Adds the `hyperfine` collection
  measurements, the per-batch audit table, the compiler-invoking classification
  with its excluded seams, and the frozen predeclaration. Records that the
  classification data lives in `tests/helpers/suite_classification.py` and is
  re-exported from `tests/helpers/suite_selection.py`, because the latter is at
  the 400-line ceiling the Pylint gate enforces on `tests/`.
