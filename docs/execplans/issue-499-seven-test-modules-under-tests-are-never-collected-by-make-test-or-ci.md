# Bring seven uncollected test modules into the default suite and guard the selector

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT

## Purpose / big picture

Seven test modules under `tests/` never run. `PYTEST_TARGETS` in the repository
`Makefile` collects only `tests/test_ci_*.py` from that directory — plus the
explicitly named `tests/test_native_sdist.py` — and the seven modules we are
concerned with match neither. `CI`'s `lint-test` job runs `make test-python`,
which reads the same variable, so these contract suites can regress without
anything noticing.

After this change, running `make test-python` executes all seven modules, and a
new guard test fails the suite whenever a root-level `tests/test_*.py` module
has no route into a suite that CI actually runs. A contributor who adds a
contract module under `tests/` and forgets to name it will be told so by
`make test-python` rather than discovering the gap months later.

## Constraints

- Do not change what `PYTEST_TARGETS` collects by widening it with a bare
  `tests/test_*.py` glob. The rename route is chosen instead: it reuses the
  existing `test_ci_` selector, so `PYTEST_TARGETS` itself is unchanged. This
  preserves the deliberate separation recorded in the `Makefile` comments around
  `ACT_SCENARIO_TARGETS`, `EXTENSION_TEST_TARGETS`, and the `test-extension`
  target, and it keeps the container-bound scenario suite and the
  extension-gated modules out of the default run.
- `ACT_SCENARIO_TARGETS` must stay out of `PYTEST_TARGETS`. CI's `lint-test`
  job has no container runtime;
  `tests/test_ci_act_harness_contract.py::test_scenario_targets_stay_out_of_the_default_suite`
  pins this, and the guard test added here must not contradict it.
- Every rename is a `git mv`, so the history of each module survives as a
  rename rather than a delete-and-add.
- English prose uses en-GB-oxendict spelling ("-ize"/"-yse"/"-our"). Code
  identifiers, comments, and docstrings follow the same rule except where an
  external contract requires otherwise.
- No production code under `cuprum/` or `rust/` changes. This is a test-tree,
  Makefile, and documentation change only.
- `.github/` workflow files must not be edited unless a contract drift is
  found that the configuration, rather than the test, is wrong about.

## Tolerances (exception triggers)

- Scope: stop and escalate if a module in `tests/` other than the seven named
  in this plan must be renamed, moved, or otherwise modified. Seven renames
  plus one new guard module and one new helper is the expected shape.
- Interface: stop and escalate if `PYTEST_TARGETS`, `ACT_SCENARIO_TARGETS`,
  `ACT_PARSER_TARGETS`, or `EXTENSION_TEST_TARGETS` must change their contents.
  The rename route exists precisely so they need not.
- Dependencies: stop and escalate if a new external dependency is required. The
  Makefile reader is written against the standard library and the existing
  `makeutil` binary already used by
  `cuprum/unittests/test_skylos_lint_contract.py`, so none is expected.
- Iterations: stop and escalate if a renamed module still fails after three
  distinct fix attempts. A module that passed triage and fails only after the
  rename is a rename defect, not a contract defect, and needs a different
  diagnosis than a third guess.
- Ambiguity: stop and present options if a module's intended contract is
  genuinely unclear after reading its originating pull request and the
  documentation. The instruction is to correct the test where its assertion is
  wrong and correct the configuration where it regressed; where neither is
  demonstrable, escalate rather than guess.
- Environment: stop and escalate if disk space in the workspace or `/tmp`
  becomes tight, rather than deleting anything to make room.

## Risks

- Risk: a renamed module resolves a path relative to its own file, so moving it
  changes what it reads. Severity: high Likelihood: low Mitigation: triage each
  module by running it *after* the rename as well as before, not only before.
  The seven modules read through `tests.helpers` accessors that resolve from
  `__file__` in the helper, and the helper's depth is unchanged by renaming a
  file inside the same directory, but this is asserted rather than assumed —
  `make test-python` collecting and passing all seven is the acceptance
  evidence.

- Risk: the syrupy snapshot for `tests/test_dev_fast_action.py` is keyed by
  module name. Renaming the module orphans the snapshot, which then fails as a
  missing snapshot rather than as an unpicked-up rename. Severity: medium
  Likelihood: high (this is certain, not speculative) Mitigation: rename
  `tests/__snapshots__/test_dev_fast_action.ambr` to match in the same commit
  as the module, then run the module to confirm the snapshots are found and
  pass. `test_dev_fast_action.py` reported "2 snapshots passed" during triage,
  so a missed rename shows up immediately.

- Risk: the guard test is vacuous. If the enumeration finds no modules, or the
  selector resolves to no files, a "nothing is uncovered" result is satisfied
  by an empty set and proves nothing. Severity: high Likelihood: medium
  Mitigation: the guard requires both the enumerated set and the covered set to
  be non-empty, and asserts specific known members — `tests/test_ci_*.py` must
  contain the seven renamed modules. Non-vacuity is discharged explicitly in the
  `Verification plan` below, including a negative control that must fail.

- Risk: reading the Makefile with the wrong mechanism produces a false clean
  result. A regex that misses an assignment, or one that reads a comment,
  silently under-reports the selector and the guard passes for the wrong
  reason. Severity: high Likelihood: medium Mitigation: use the pinned
  `makeutil parse Makefile` tool, which emits structured JSON with `variables`
  and `rules` already parsed and already used by
  `cuprum/unittests/test_skylos_lint_contract.py`. Where a variable is read by
  expansion, the helper expands `$(VAR)` recursively and fails on an undefined
  reference rather than returning empty. A negative control mutates the
  selector and must be rejected.

- Risk: the new helper crosses the 400-line file limit or exceeds the
  Pydantic-era module budget that `pylint` and CodeScene enforce on production
  modules. Severity: low Likelihood: low Mitigation: `tests/` is a test tree;
  `pylint` is nevertheless invoked over `tests` by `PYLINT_TARGETS`, and
  `pylint` ran only on packages it can parse. Keep both new files well under
  the limit and check `make lint` at the milestone.

## Progress

- [x] (2026-09-26 15:00Z) Confirmed branch
  `issue-499-seven-test-modules-under-tests-are-never-collected-by-make-test-or-ci`
  and set the Lody session title.
- [x] (2026-09-26 15:00Z) Reconnaissance: read `Makefile` selector variables,
  `AGENTS.md`, `tests/helpers/` inventory, `docs/developers-guide.md` testing
  sections, and the guard precedent in
  `tests/test_ci_test_coverage_overlap.py::test_make_keeps_both_suites_available_locally`.
- [x] (2026-09-26 15:00Z) Task 1 triage: ran all seven modules individually with
  `uv run pytest <module> -v`. All seven pass. No contract drift, no failing
  assertion, no missing prerequisite, and no skip.
- [x] (2026-09-26 15:00Z) Established that the `#488` platform gate referenced
  by the task brief does not exist; the four `#488` step-execution modules
  carry no `skipif`, no `sys.platform` check, and no platform guard of any
  kind. The conditional instruction therefore does not apply.
- [ ] Task 2: rename the seven modules to `tests/test_ci_*.py`.
- [ ] Task 3: add `tests/helpers/makefile.py` and
  `tests/test_ci_test_selection_contract.py`, and document the rule.
- [ ] Milestone gates and CodeRabbit review per milestone.
- [ ] Push and open a draft pull request.

## Surprises & discoveries

- Observation: all seven modules pass on this Linux host, at the tip of
  `main` (`991dee64`), with no triage fix required. The issue anticipated that
  "some of these modules may already be failing, because nothing has run them";
  none is. Evidence: `uv run pytest <module> -v` per module, counts recorded
  under `Artefacts and notes` below. Impact: Task 1 reduces to a rename. No
  contract drift, no `xfail` marker, and no follow-up issue is needed. The
  triage counts still belong in the pull request, because they are the evidence
  that the rename did not change behaviour.

- Observation: the `#488` step-execution platform gate the task brief refers to
  does not exist in this tree. The four `#488` modules
  (`tests/test_ci_build_wheels_maturin_step.py`,
  `tests/test_ci_pure_python_wheel_build_step.py`,
  `tests/test_ci_build_wheels_sdist_cardinality.py`,
  `tests/test_ci_rust_boundaries_extended_step.py`) contain no `skipif`, no
  `sys.platform` reference, and no platform gate; they pass on Linux. The
  task's own wording is conditional — "Apply the `#488` step-execution platform
  gate to the three specified shell-executing modules only if that gate
  exists". Evidence:
  `grep -rn "skipif\|sys.platform\|platform.system" tests/test_ci_*.py` returns
  nothing; `git grep` over the `#488` commit `2673770a` shows no such gate
  added. Impact: the conditional is not triggered. The three shell-executing
  modules that would have received it (`test_resource_sampler_action.py`,
  `test_setup_sccache_action.py`, `test_dev_fast_action.py`) keep their
  existing assertions, unweakened.

- Observation: `tests/test_resource_sampler_action.py` completes in 16.07 s
  against a suite-wide `timeout = 30` in `pyproject.toml`, and it waits up to
  40 s for the sampler to produce its first row. The 40-second wait therefore
  exceeds the 30-second per-test timeout, so the wait loop can never reach its
  own deadline on a slow host: `pytest-timeout` fires first and the test fails
  as a timeout rather than with the module's own "the sampler produced no rows
  within 40 s" message. Evidence: `tests/test_resource_sampler_action.py:129`
  sets `deadline = time.monotonic() + 40` inside
  `test_the_sampler_writes_three_numbers_per_interval`, while
  `pyproject.toml:370` sets the suite-wide `timeout = 30`. The module passed on
  triage because the sampler's first row arrived well inside 30 s. Impact: this
  is a latent defect the rename would carry into the default suite. It must be
  resolved in Task 1 as the brief requires — the 40-second bound and the
  30-second timeout conflict is named explicitly. See the decision logged below
  for the resolution.

- Observation: `pytest` resolves `python_files` by default, and `tests/` has no
  `testpaths` or `norecursedirs` override, so the guard test's definition of
  "root-level module" must be the explicit `tests/test_*.py` glob rather than
  an assumption about collection. Evidence:
  `grep "python_files\|testpaths\|norecursedirs" pyproject.toml` returns
  nothing; `[tool.pytest.ini_options]` at `pyproject.toml:368` sets only
  `timeout`. Impact: the guard enumerates `tests/test_*.py` positively, which
  is the same set the issue names.

## Decision log

- Decision: rename the seven modules into the existing `tests/test_ci_*.py`
  selector rather than widening `PYTEST_TARGETS`. Rationale: the issue offers
  both routes. Renaming reuses a selector the repository already treats as "the
  CI contract suites", which is exactly what these seven are — six are workflow
  or composite-action contracts, and the seventh is the dev-fast action
  contract. Widening `PYTEST_TARGETS` with seven more literal lines leaves the
  next uncollected module just as invisible and adds a second naming convention
  for the same kind of test. The `#488` precedent named in the issue took the
  rename route for the same reason. Additionally, `PYTEST_TARGETS` contains
  deliberately excluded neighbours (`ACT_SCENARIO_TARGETS`,
  `EXTENSION_TEST_TARGETS`); leaving the variable's contents untouched keeps
  those exclusions auditable at a glance. Date/Author: 2026-09-26, agent.

- Decision: resolve the `tests/test_resource_sampler_action.py` 40 s bound /
  30 s timeout conflict by keeping the assertion meaningful and making the wait
  bounded by the suite timeout rather than exceeding it. Rationale: the
  40-second bound was presumably chosen to tolerate a slow sampler start, but
  it is unreachable — pytest-timeout kills the test at 30 s first, so on a slow
  host the test fails with a bare timeout and loses the module's own
  diagnostic. Raising the suite timeout for the whole repository to accommodate
  one module would relax every other test's protection, and the module does not
  need 40 s: triage shows the first row arriving in well under a second. The
  resolution keeps a real bound, keeps the diagnostic, and adds the
  prerequisite skip the brief asks for. Date/Author: 2026-09-26, agent.

- Decision: read the Makefile through `makeutil parse Makefile`, the pinned
  parser the repository already depends on, rather than through a local regex.
  Rationale: `cuprum/unittests/test_skylos_lint_contract.py:24` already runs
  `("makeutil", "parse", "Makefile")` and reads its JSON, and
  `.github/actions/install-makeutil/action.yml` pins and installs the binary
  for CI, so the dependency exists and is provisioned. A structured parse is
  authoritative about continuations, comments, and expansions in a way a regex
  is not, and the risk of a false clean result from a misparsed selector is the
  most serious risk this plan carries. `tests/test_ci_act_harness_contract.py`
  uses an ad-hoc regex reader for its own narrower purpose; the new helper is
  the general one, and the guard test uses the general one. Date/Author:
  2026-09-26, agent.

## Outcomes & retrospective

Not yet complete. To be written at each milestone boundary.

## Context and orientation

A novice reading this plan needs three things: where the selector lives, how
the suites are split, and what the seven modules are.

The selector is `PYTEST_TARGETS` in the repository `Makefile`, defined around
line 154 as a `?=` assignment containing nine whitespace-separated shell glob
patterns. The `test-python` target around line 420 iterates over those patterns
and runs `pytest` once per pattern, skipping any pattern whose first expanded
word does not exist on disk. `make test` runs `test-python` and `test-rust`
together. CI's `lint-test` job in `.github/workflows/ci.yml` runs
`make test-python`.

Three sibling variables matter and must not change:

- `ACT_PARSER_TARGETS` is *inside* `PYTEST_TARGETS` and runs the recorded-stream
  parser tests on every machine.
- `ACT_SCENARIO_TARGETS` is *outside* it, because the scenarios need a container
  runtime. `make test-act` runs them, and
  `.github/workflows/benchmark-gate-harness.yml` calls that target.
- `EXTENSION_TEST_TARGETS` is outside it too, because the extension must be
  built first. `make test-extension` runs them.

The seven modules, all directly under `tests/`:

| Module today                                   | Contract                                                  |
| ---------------------------------------------- | --------------------------------------------------------- |
| `tests/test_codescene_environment_contract.py` | the `codescene` environment sits on the uploading job     |
| `tests/test_coverage_scratch_discard.py`       | the coverage discard step reclaims instrumented trees     |
| `tests/test_loom_workflow_contract.py`         | the scheduled Loom workflow shape and single cache writer |
| `tests/test_resource_sampler_action.py`        | the resource-sampler composite action's shell behaviour   |
| `tests/test_setup_sccache_action.py`           | the sccache setup action's backend selection              |
| `tests/test_workflow_contract.py`              | the mutation-mutmut caller's pinned shape                 |
| `tests/test_dev_fast_action.py`                | the dev-fast composite action's install step              |

All seven rename by prefacing `ci_`:
`tests/test_ci_codescene_environment_contract.py` and so on, with
`tests/test_workflow_contract.py` becoming
`tests/test_ci_mutation_workflow_contract.py` because its subject is the
mutation-mutmut workflow caller and the shorter name would read as if it owned
every workflow contract.

`tests/dev_fast_action.ambr` is the one syrupy snapshot file affected; it lives
at `tests/__snapshots__/test_dev_fast_action.ambr` and is keyed by module name.

The guard test belongs in the `test_ci_` family because it is itself a CI
contract. It will be named `tests/test_ci_test_selection_contract.py`, which
lets the guard police its own inclusion: if someone removes the
`tests/test_ci_*.py` pattern, the guard is no longer collected, and the module
that would have complained is gone. That is acceptable and is noted as a
residual gap — the alternative, a guard outside the selector, is not collected
either.

## Conformance basis

No Terms of Reference or technical design document governs this change. The
upstream artefacts are:

- GitHub issue `#499`, "Seven test modules under `tests/` are never collected by
  `make test` or CI", which supplies the module list, the evidence, and the two
  candidate fixes.
- Pull request `#497`, which added
  `tests/test_codescene_environment_contract.py` and did not rename it into the
  selector.
- Pull request `#488` (commit `2673770a`), the named precedent: it added four
  `tests/` step-execution modules and renamed them into `tests/test_ci_*.py`
  when the same problem was found.
- `AGENTS.md`, which sets the 400-line file limit, the en-GB-oxendict spelling
  rule, the quality gates, and the commit conventions.

Trace links:

```plaintext
ISSUE-499/module-list      -> EP-M1 -> make test-python collects all seven
ISSUE-499/rename-route     -> EP-M2 -> tests/test_ci_*.py expands to the seven
ISSUE-499/guard-test       -> EP-M3 -> tests/test_ci_test_selection_contract.py
AGENTS/400-line-limit      -> EP-M3 -> tests/helpers/makefile.py line count
```

## Verification plan

The change introduces one non-trivial invariant and one helper contract. There
are no numerical lemmas, no concurrency, and no memory obligations; the work is
test selection, so the obligations are about set membership and resolution
fidelity.

Obligation `SELECT-1`: every root-level `tests/test_*.py` module is a member of
the set `PYTEST_TARGETS` expands to, or of `ACT_SCENARIO_TARGETS`, or appears
in a documented exception table.

- Method: parameterized/assertion test over the real file set and the real
  Makefile, plus a negative control.
- Rationale: the domain is a finite, small, concrete set of files and
  patterns. There is no input space to generate; enumerating what is actually
  in the tree is both exhaustive and cheap, and a property test would add
  machinery without adding coverage.
- Domain: every `tests/test_*.py` on disk at the time of the run, minus the
  documented exceptions (initially none).
- Artefact: `tests/test_ci_test_selection_contract.py`.
- Evidence: `uv run pytest tests/test_ci_test_selection_contract.py -v` passes
  with the seven modules renamed, and fails with a message naming each
  uncovered module when one is renamed back out of the selector.
- Non-vacuity: the guard asserts both the enumerated set and the covered set are
  non-empty and asserts the seven renamed modules are in the expansion. The
  negative control is the red stage of Red-Green-Refactor: the guard is written
  and run *before* the rename is applied, so it must fail listing all seven
  modules. That failure is the proof the guard detects exactly the defect
  reported in `#499`. A second control — temporarily removing
  `tests/test_ci_*.py` from the selector — must also be rejected, proving the
  covered set is not accidentally satisfied by another pattern.

Obligation `RESOLVE-1`: the helper's pattern resolution agrees with the shell's
own glob expansion, and its variable expansion agrees with `make`'s.

- Method: assertion tests over known members and known non-members, with the
  `make` command itself as the oracle where an oracle is cheap.
- Rationale: a resolver that disagrees with the shell is the one way the guard
  can be confidently wrong. Comparing against `make -n test-python`'s printed
  patterns and against `bash`-style globbing of the same patterns grounds the
  helper in an executable oracle rather than in a restatement of its own logic.
- Domain: the nine patterns in `PYTEST_TARGETS`, the three in
  `ACT_SCENARIO_TARGETS`, and a synthetic undefined variable.
- Artefact: `tests/helpers/makefile.py` and its use in the guard test.
- Evidence: the guard passes; a pattern with no matches contributes nothing and
  does not raise; `$(UNDEFINED)` raises rather than silently expanding to empty.
- Non-vacuity: the expansion test cites at least one pattern that expands to
  many files (`cuprum/unittests/test_*.py`) and one that expands to exactly one
  (`tests/test_native_sdist.py`), so neither "resolve everything" nor "resolve
  nothing" can pass. The undefined-variable case is the negative control.

Axioms relied upon, all third-party interfaces treated as given:

- `makeutil parse Makefile` reports every `?=` and `:=` assignment in
  `variables` with a `raw_value` preserving continuation lines, and every
  target recipe in `rules`. Verified by inspection during reconnaissance: the
  tool reported `PYTEST_TARGETS` and `ACT_SCENARIO_TARGETS` with their exact
  source text. If a future `makeutil` changed this shape, the helper fails
  loudly on a missing key rather than returning empty.
- `pathlib.Path.glob` semantics for the pattern syntax used in the selector,
  which is ordinary shell-style globbing over a single directory level.
- `pytest` collects a module named on the command line regardless of
  `python_files`.

No formal proof is warranted: the property under test is set membership over a
finite file list, and the bound is the whole domain rather than a sample of it.

## Plan of work

Stage A — triage (complete). Run each of the seven modules individually and
record the counts.

Stage B — red guard. Write `tests/helpers/makefile.py` and
`tests/test_ci_test_selection_contract.py` *before* the rename, run the guard,
and observe it fail listing all seven modules. Commit the failing test as the
Red stage, because the failure is the evidence the guard works.

Stage C — rename (Green). Rename the seven modules with `git mv`, rename the
snapshot, update the `Makefile`'s `test-dev-fast-contract` recipe, the one
in-module reference in `tests/test_loom_workflow_contract.py`, the two
references in `docs/developers-guide.md`, and the one in
`tests/helpers/ci_runners.py`. Re-run the guard: it must now pass.

Stage D — resolve the sampler timing defect, update documentation
(`docs/developers-guide.md`), commit, and run the full gates.

Each stage ends with validation. Do not proceed to the next stage if the
current stage's validation fails.

## Milestones and plateaus

`EP-M1 — triage recorded`. Requirements and gaps: `ISSUE-499/module-list`
advanced. End state: seven modules confirmed passing, with counts recorded.
Acceptance evidence: the per-module counts in `Artefacts and notes`.
Conformance check: no source file was modified by triage. Recovery: nothing to
revert; triage is read-only. Remaining gaps: nothing is yet collected by the
suite. Compatibility decision: none required; this is a test-only surface.

`EP-M2 — the seven modules enter the selector`. Requirements and gaps:
`ISSUE-499/rename-route` discharged. End state: `tests/test_ci_*.py` expands to
the seven renamed modules plus the existing ones; `PYTEST_TARGETS` is
byte-identical to its previous value; all seven pass under `make test-python`.
Acceptance evidence: `make test-python` collects and passes the seven; the
`test-dev-fast-contract` target still collects its module. Conformance check:
the three sibling variables are unchanged; no `skipif` was added to weaken an
assertion; the snapshot rename is in the same commit as the module rename.
Recovery: `git revert` the rename commit; the modules return to their
uncollected state and no other behaviour changes. Remaining gaps: the guard
test does not yet exist, so the next uncollected module still goes unnoticed.
Compatibility decision: none required.

`EP-M3 — the guard prevents recurrence`. Requirements and gaps:
`ISSUE-499/guard-test` discharged. End state:
`tests/test_ci_test_selection_contract.py` passes, and fails when any
root-level module leaves the selector. Acceptance evidence: the guard's Red run
listing seven modules, and its Green run after the rename. Conformance check:
the exception table is empty and documented as such; the helper fails on an
undefined variable; the guard asserts non-emptiness of both sets. Recovery:
revert the guard commit; the suite returns to `EP-M2`. Remaining gaps: the
guard is itself inside the selector it polices, noted above.

## Concrete steps

All commands run from the repository root,
`/home/leynos/.lody/repos/github---leynos---cuprum/worktrees/623954fa-05d1-4aeb-953d-0ccfe3805275`.

Triage, already performed:

```bash
uv run pytest tests/test_codescene_environment_contract.py -v
uv run pytest tests/test_coverage_scratch_discard.py -v
uv run pytest tests/test_loom_workflow_contract.py -v
uv run pytest tests/test_resource_sampler_action.py -v
uv run pytest tests/test_setup_sccache_action.py -v
uv run pytest tests/test_workflow_contract.py -v
uv run pytest tests/test_dev_fast_action.py -v
```

Renames:

```bash
git mv tests/test_codescene_environment_contract.py tests/test_ci_codescene_environment_contract.py
git mv tests/test_coverage_scratch_discard.py       tests/test_ci_coverage_scratch_discard.py
git mv tests/test_loom_workflow_contract.py         tests/test_ci_loom_workflow_contract.py
git mv tests/test_resource_sampler_action.py        tests/test_ci_resource_sampler_action.py
git mv tests/test_setup_sccache_action.py           tests/test_ci_setup_sccache_action.py
git mv tests/test_workflow_contract.py              tests/test_ci_mutation_workflow_contract.py
git mv tests/test_dev_fast_action.py                tests/test_ci_dev_fast_action.py
git mv tests/__snapshots__/test_dev_fast_action.ambr \
       tests/__snapshots__/test_ci_dev_fast_action.ambr
```

Reference updates after the renames:

- `Makefile:471` in the `test-dev-fast-contract` recipe.
- `tests/test_ci_loom_workflow_contract.py:3` in its module docstring.
- `tests/helpers/ci_runners.py:140` in a comment naming the mutation-workflow
  contract.
- `docs/developers-guide.md:396` and `docs/developers-guide.md:5363,5372`.

Acceptance:

```bash
make test-python
make test-dev-fast-contract
uv run pytest tests/test_ci_test_selection_contract.py -v
```

## Validation and acceptance

Red-Green-Refactor evidence:

- Red: with the guard and helper written but the renames not yet applied,

  ```bash
  uv run pytest tests/test_ci_test_selection_contract.py -v
  ```

  must fail, and the failure message must name all seven uncollected modules.
  Expected shape:

  ```plaintext
  AssertionError: these root-level modules no target in PYTEST_TARGETS or
  ACT_SCENARIO_TARGETS collects: tests/test_codescene_environment_contract.py,
  tests/test_coverage_scratch_discard.py, tests/test_loom_workflow_contract.py,
  tests/test_resource_sampler_action.py, tests/test_setup_sccache_action.py,
  tests/test_workflow_contract.py, tests/test_dev_fast_action.py. Fix by
  renaming to tests/test_ci_*.py or adding the module to PYTEST_TARGETS.
  ```

- Green: after the renames, the same command passes, and
  `make test-python` collects and passes all seven.

- Refactor: the helper is tidied for line count and docstring form; the guard
  and the full `make test-python` are re-run.

Quality criteria:

- Tests: `make test-python` passes, collecting the seven renamed modules. The
  guard passes. `make test-dev-fast-contract` passes and collects
  `tests/test_ci_dev_fast_action.py`.
- Verification: `SELECT-1` and `RESOLVE-1` discharged with the non-vacuity
  evidence above.
- Lint/typecheck: `make lint`, `make check-fmt`, and `make typecheck` pass,
  run sequentially by `scrutineer`.
- Markdown: `make markdownlint` passes after the `docs/developers-guide.md`
  edits.
- Performance: not applicable; no production path changes.

Quality method: the full commit gate set, run sequentially via `scrutineer`,
with output captured to `/tmp` per the repository convention.

## Idempotence and recovery

Every step is idempotent or trivially revertible. `git mv` on an
already-renamed file fails harmlessly. The guard test reads the tree and
mutates nothing. The `make test-python` run is read-only apart from its Cargo
and pytest caches.

The renames are the only step with a non-trivial failure mode: if the snapshot
rename is missed, `make test-python` fails on a missing snapshot. The recovery
is to apply the missed `git mv` — the snapshot file is still in the tree and
nothing was lost.

If a renamed module fails for a reason triage did not reveal, revert that one
rename with `git mv` back and escalate rather than adding a `skipif`.

## Artefacts and notes

Triage results, at `main` tip `991dee64`, on Linux, `uv run pytest <module> -v`:

```plaintext
tests/test_codescene_environment_contract.py   9 passed in 1.03s
tests/test_coverage_scratch_discard.py         6 passed in 0.28s
tests/test_loom_workflow_contract.py           5 passed in 0.11s
tests/test_resource_sampler_action.py          8 passed in 16.07s
tests/test_setup_sccache_action.py             7 passed in 0.12s
tests/test_workflow_contract.py                6 passed in 0.04s
tests/test_dev_fast_action.py                  9 passed in 0.17s (2 snapshots)
```

Total: 50 tests, 0 failures, 0 skips, 0 errors.

The selector as it stands, from `makeutil parse Makefile`:

```plaintext
PYTEST_TARGETS ?= cuprum/unittests/test_*.py \
  tests/test_ci_*.py \
  tests/test_native_sdist.py \
  scripts/tests/test_boundary_*.py \
  scripts/tests/test_rust_lint_baseline_contract.py \
  tests/behaviour/test_[a-h]*.py \
  tests/behaviour/test_[i-r]*.py \
  tests/behaviour/test_[s-z]*.py \
  $(ACT_PARSER_TARGETS)
```

## Interfaces and dependencies

In `tests/helpers/makefile.py`, define a small reader over the `makeutil` JSON:

```python
def makefile_variables(path: Path) -> dict[str, str]: ...
def variable_expansion(name: str) -> str: ...
def selected_paths(patterns: str) -> tuple[Path, ...]: ...
def target_recipe(name: str) -> str: ...
```

`variable_expansion` returns the whitespace-split patterns of a named Makefile
variable with `$(VAR)` references expanded recursively, raising
`AssertionError` on an undefined reference. `selected_paths` resolves one
pattern against the repository root and returns the files it names, returning
an empty tuple for a non-path pattern that is not a selector entry — the rule
documented in the module docstring, and the reason a bare `pytest`-level marker
cannot silently appear in the selector.

In `tests/test_ci_test_selection_contract.py`, define the guard:

```python
EXCEPTIONS: dict[str, str] = {}  # module path -> target and reason

def test_every_root_level_module_has_a_ci_route() -> None: ...
def test_the_selector_resolves_to_the_renamed_contract_modules() -> None: ...
def test_ci_runs_the_selector() -> None: ...
```

`test_ci_runs_the_selector` uses
`tests.helpers.workflow_shell.script_runs_command` and
`tests.helpers.ci_workflows.workflow_sources` to prove a workflow `run:` step
invokes `make test-python`, so the guard connects the selector to the workflow
that consumes it rather than stopping at the Makefile.

## Revision note

2026-09-26, initial draft. Records the completed triage, the rename route
decision, the discovery that the `#488` platform gate does not exist, and the
latent sampler timeout conflict that Task 1 must resolve.
