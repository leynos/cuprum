# Bring seven uncollected test modules into the default suite and guard the selector

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IMPLEMENTED, awaiting gates and pull request

## Purpose / big picture

Seven test modules under `tests/` never run in the default suite.
`PYTEST_TARGETS` in the repository `Makefile` collects only
`tests/test_ci_*.py` from that directory — plus the explicitly named
`tests/test_native_sdist.py` — and the seven modules in question match neither.
`CI`'s `typecheck-test` job runs `make test-python`, which reads the same
variable, so these contract suites can regress without anything noticing. (The
`coverage` job's bare rootdir `pytest` does collect them, so they are not
unexecuted everywhere; the loss is the fast local loop.)

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
- `ACT_SCENARIO_TARGETS` must stay out of `PYTEST_TARGETS`. CI's
  `typecheck-test` job has no container runtime;
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
- [x] (2026-09-26 16:30Z) Task 1 completed: resolved the sampler's 40 s bound /
  30 s suite timeout conflict with a per-test `@pytest.mark.timeout(120)`
  marker rather than by shortening the wait or loosening the suite default, and
  added skip reasons for non-Linux hosts and for a missing `free`/`df`/`du`.
  The toolbox assertion deliberately does not skip on a missing tool.
- [x] (2026-09-26 16:45Z) Task 2: renamed the seven modules and the syrupy
  snapshot with `git mv`; updated the `test-dev-fast-contract` recipe, the loom
  module docstring, the `ci_runners.py` comment, and two developers'-guide
  references. `make test-python` collects and passes all seven.
- [x] (2026-09-26 17:15Z) Task 3: added `tests/helpers/makefile.py` (makeutil
  JSON reader with recursive expansion) and
  `tests/test_ci_test_selection_contract.py`, and documented the rule under
  "Test selection" in the developers' guide.
- [x] (2026-09-26 17:30Z) Guard non-vacuity discharged: both negative controls
  are rejected. Renaming one module back out of the selector fails four guard
  tests and names the module; removing `tests/test_ci_*.py` from
  `PYTEST_TARGETS` fails 52.
- [x] (2026-09-26 17:35Z) Pulled the branch forward through two gate failures.
  `make check-fmt` reformatted three files (two modules and the ExecPlan's own
  `python`-tagged sketches); `make lint` reported twelve errors across the two
  new modules. Both fixed and committed.
- [x] (2026-09-26 17:53Z) Gate run at `a4053bd3` reported all five green with
  every lint leaf observed. Superseded: the fix that followed landed after
  gates 1–4, so those logs certify `a4053bd3`, not the branch head.
- [x] (2026-09-26 18:05Z) CodeScene delta review failed the pull request on a
  nested-complexity finding against the guard's workflow walk. Fixed by moving
  the sweep into `tests/helpers/ci_workflows.run_scripts`;
  `cs delta origin/main` now reports no issues. (That function later moved to
  `tests/helpers/ci_run_scripts.py` to clear C0302; see the entry below.)
- [x] (2026-09-26 18:20Z) `make lint` at `dd8dc2df` reported the sweep had
  pushed `tests/helpers/ci_workflows.py` to 420 lines, over pylint's 400-line
  C0302 cap. Split into `tests/helpers/ci_run_scripts.py` (97 lines) on the
  boundary `workflow_shell`/`workflow_recipe` already document: reading a named
  thing stays in `ci_workflows` (358 lines), sweeping for unnamed things moves
  out. The guard imports from the new module; nothing else referenced it.
- [x] (2026-09-26 19:10Z) CodeRabbit's first `--agent` pass raised 14 findings,
  all triaged as real. Addressing them grew both `ci_workflows.py` and the
  guard past the 400-line C0302 cap again, so two more splits are part of the
  fix rather than a follow-up:
  - `tests/helpers/ci_documents.py` (181 lines) takes the parse-and-narrow
    layer — `parse_document`, `document_jobs`, `narrow_steps`, `mapping`,
    `step_inputs`, `cache_paths` — leaving `ci_workflows.py` (339 lines) with
    the resolve-a-named-file layer. `run_scripts` now parses each workflow once
    in its own sweep, so it narrows from the document it read rather than
    resolving the file name again through a fixed directory.
  - `tests/helpers/suite_selection.py` (226 lines) takes the exception table
    and everything that validates it, leaving the guard
    (`tests/test_ci_test_selection_contract.py`, 319 lines) with only its
    assertions. The table now sits beside the code that checks its claims.
  Three further fixes landed with them: the Makefile reader resolves `?=` by
  operator rather than position (a later `?=` must not override an earlier
  assignment, or the guard polices a selector `make` never uses);
  `narrow_steps` separates a job that declares no `steps:` from one whose
  `steps:` is malformed, which `run_scripts` had been reading as "no steps";
  and the guard's original seeded-fault control was replaced by two whose
  second halves are falsifiable — the first version asserted a module name
  absent from disk reached `uncovered()`, which it never could.
- [x] (2026-09-26 19:15Z) Corrected three inherited misattributions, all
  verified against the tree rather than the issue text: `make test-python` runs
  in CI's `typecheck-test` job, not `lint-test` (this ExecPlan, the guard's
  docstring, the developers' guide, and the draft PR body all said the wrong
  job); the seven modules *are* collected by the `coverage` job's bare rootdir
  `pytest`, so the loss is the fast local loop rather than all execution; and
  `pytest` exits 5 on empty collection rather than zero, so the guide's "exits
  zero for having collected nothing" was wrong about the mechanism — the
  recipe's `[ -e "$1" ] || continue` guard is what skips a pattern silently.
  The guide's Test selection section now states all three.
- [ ] Milestone gates at the resulting head.
- [ ] CodeRabbit review.
- [ ] Push and open a draft pull request.

- Observation: the guard as first written passed every local gate and CI's
  `typecheck-test`, and still failed CodeScene's delta review. The check-run is
  not in the branch-protection ruleset, so the pull request is `MERGEABLE` with
  it red — but it is a real finding, not noise: `cs delta origin/main`
  reproduces it, naming `test_ci_invokes_the_target_that_consumes_the_selector`
  with a nested complexity depth of 4 against a threshold of 4. Evidence:
  `cs delta origin/main` before the fix reports "New issue: Deep, Nested
  Complexity"; after the fix it reports "No issues found!". Impact: the
  three-deep workflow/job/step walk moved into
  `tests/helpers/ci_workflows.run_scripts`, where it is reusable and where the
  guard asks its question in one comprehension. A second finding then appeared
  against the new helper — proving the fix was measured rather than assumed —
  and the per-job walk became its own function to clear it. The helper has
  since moved again, to `tests/helpers/ci_run_scripts.py`, and both functions
  went with it; the clean `cs delta` verdict was re-established after that
  split. The general lesson: a green local gate set and a green
  `typecheck-test` do not cover CodeScene's complexity rules, so `cs delta`
  must be run after adding a nested walk.

- Observation: running a pinned gate run while editing the tree invalidates the
  run, and it is not enough to wait for the *last* gate to finish. In the
  `a4053bd3` run, editing began once `make test` had finished, while
  `make markdownlint` — the fifth gate — was still executing. The run reported
  `head_before == head_after` and green throughout, but its cleanliness
  precondition failed mid-run, so the logs cannot certify the following commit.
  Evidence: the gate report's timeline places the first external edit at
  17:56:12 and gate 5's completion at 17:57:51, with the working-diff
  fingerprint changing during gate 5. Impact: gates 1–4 certify `a4053bd3`; the
  fix landed afterwards and needs its own run. The rule to carry forward is to
  wait for the run's own completion report, not for a marker in one log.

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

- Observation: `makeutil`'s `raw_value` preserves the source's
  backslash-newline continuations verbatim, so a naive `.split()` on the
  expanded value yields a literal `\` word between every pair of patterns.
  Worse, it is a *silent* defect: `selected_paths` skips a pattern that does
  not end in `.py`, so the stray backslashes were discarded and the resolved
  file set looked correct. The helper now collapses continuations the way
  `make` does before splitting. Evidence: the first smoke test printed
  `PYTEST_TARGETS` as nine alternating path/`\` tokens; the resolved set was
  nonetheless 344 files and included all seven. Impact: only the returned tuple
  was wrong, but any future caller comparing words would have been misled. This
  is why `variable_expansion` joins continuations rather than splitting the raw
  text.

- Observation: `Path("tests") == "tests"` is `False`. The guard's first version
  filtered selector results with `path.parent == "tests"`, which excluded every
  module; 54 of 56 guard tests failed reporting modules as uncovered that the
  selector plainly names. Evidence: the first Green run of
  `tests/test_ci_test_selection_contract.py` reported 54 failures naming all 49
  root modules. Impact: the comparison now uses a module-level
  `_TESTS = Path ("tests")`, and the constant's comment records why a string
  comparison cannot be used here.

- Observation: the guard's own name puts it inside the selector it polices.
  `tests/test_ci_test_selection_contract.py` matches `tests/test_ci_*.py`, so
  removing that pattern also removes the guard. Evidence: negative control B,
  which deleted the pattern, failed 52 of the 56 tests rather than all of them
  — the four that survived include the seeded-fault control, which needs no
  selector. Impact: accepted and recorded in `Context and orientation` as a
  residual gap. The alternative — putting the guard outside the selector — is
  not collected either, so it would guard nothing. The `test_ci_` family is the
  right home; a repository-wide selector rewrite is a bigger change than issue
  #499 asks for.

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
  30 s timeout conflict by keeping the 40 s wait and raising *that one test's*
  ceiling with `@pytest.mark.timeout(120)`. The constants are named
  `SAMPLE_DEADLINE_SECONDS = 40` and `SAMPLE_TEST_TIMEOUT_SECONDS = 120`, and
  the marker is sized above the wait (40 s) plus the start step's own
  subprocess limit (60 s). Rationale: the wait is meaningful — the sampler's
  loop is `while sleep 15`, so its first row lands near 15 s and its second
  near 30 s, and 40 s is what tolerates one missed interval on a loaded runner.
  Shortening it to fit under 30 s would remove exactly that tolerance, and
  truncating the wait at the suite ceiling would make the test's own "produced
  no rows within N s" message unreachable, so a slow host would see an
  unexplained timeout instead of the diagnostic naming the sampler. Raising the
  suite-wide default in `pyproject.toml` to accommodate one module would relax
  every other test's protection. Narrowing it to one test costs nothing and
  keeps both the real bound and the diagnostic. The brief also asked for a skip
  on non-Linux hosts and on a missing `free`/`df`/`du`: both are implemented,
  and the toolbox assertion itself deliberately does *not* skip on a missing
  tool, because that is the defect it exists to catch and a skip there would
  make it a tautology. Date/Author: 2026-09-26, agent.

- Decision: name the mutation-workflow contract
  `tests/test_ci_mutation_workflow_contract.py` rather than
  `tests/test_ci_workflow_contract.py`. Rationale: its subject is the
  mutation-mutmut caller workflow specifically, and the shorter name would read
  as if it owned every workflow contract, which
  `tests/test_ci_test_selection_contract.py` and the other workflow contracts
  would then appear to contradict. The brief named this module explicitly, so
  the choice is recorded rather than assumed. Date/Author: 2026-09-26, agent.

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

- Decision: leave `tests/test_ci_act_harness_contract.py`'s private
  `_directives`/`_variable`/`_target` readers in place rather than re-pointing
  that module at `tests/helpers/makefile.py`. Rationale: `AGENTS.md` requires
  sweeping for an existing equivalent before adding an abstraction, and that
  module does contain one — so the choice is deliberate, not an oversight. Its
  reader answers a different question: it asserts *textual* properties of a
  recipe (that the literal `$(ACT_SCENARIO_TARGETS)` appears in `test-act`'s
  argv, that `CUPRUM_REQUIRE_ACT` appears in exactly one directive across the
  whole file). Neither is an expansion, and the second is a census over the
  source that a parsed variable table cannot answer. Re-pointing it would lose
  the cross-file directive count and the literal-token assertions. The two
  readers coexist with documented scopes: the private one reads this Makefile's
  text for byte-level contracts, the shared one reads the parsed selector for
  set-membership contracts. Date/Author: 2026-09-26, agent.

## Outcomes & retrospective

`EP-M1` through `EP-M3` are complete; the gates and the pull request remain.

What was achieved. All seven modules issue #499 named now run under
`make test-python`: `env -u BASH_ENV make test-python` passes with the
`tests /test_ci_*.py` pattern collecting 688 tests, and each of the seven
appears in the collected set by name. `make test-dev-fast-contract` collects
the renamed dev-fast module and its two snapshots. The guard,
`tests/test_ci_test_selection_contract.py`, passes 56 tests and rejects both
negative controls: renaming one module back out of the selector fails four of
its tests naming that module, and deleting `tests/test_ci_*.py` from
`PYTEST_TARGETS` fails 52. The rule is documented under "Test selection" in the
developers' guide.

What went differently from the plan. Task 1 was expected to need repairs;
triage found none, so the only Task 1 work was the sampler's latent timeout
conflict. The plan anticipated that the makefile helper would be a fresh
abstraction over a tree with no equivalent; in fact
`tests/test_ci_act_harness_contract.py` already held a private reader, and the
decision log now records why the two coexist rather than one replacing the
other. The plan expected the guard could be written and observed Red *before*
the rename; because the rename was already applied by the time the helper was
finished, the Red evidence is the two negative controls instead, which is the
stronger artefact — they are reproducible on the final tree rather than being a
transcript of a state that no longer exists.

Lessons. Three defects surfaced during Task 3 that examples alone would not
have caught, and each is recorded in `Surprises & discoveries`: `makeutil`'s
raw values carry continuation backslashes that `.split()` turns into spurious
words; `Path("tests") == "tests"` is `False`, so a string comparison silently
excluded every module; and the guard's own name places it inside the selector
it polices. The first two are invisible in a passing run — the resolved set was
correct in both cases despite the bugs — which is the argument for reading the
selector structurally and asserting non-vacuity rather than trusting a green
result.

## Context and orientation

A novice reading this plan needs three things: where the selector lives, how
the suites are split, and what the seven modules are.

The selector is `PYTEST_TARGETS` in the repository `Makefile`, defined around
line 154 as a `?=` assignment containing nine whitespace-separated shell glob
patterns. The `test-python` target around line 420 iterates over those patterns
and runs `pytest` once per pattern, skipping any pattern whose first expanded
word does not exist on disk. `make test` runs `test-python` and `test-rust`
together. CI's `typecheck-test` job in `.github/workflows/ci.yml` runs
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

Red-Green-Refactor evidence. The order the work actually ran in differs from
the plan: the rename landed before the guard was finished, so the Red stage is
reproduced as two negative controls applied to the finished tree rather than as
a transcript of a state that no longer exists. Reproducing them on the final
tree is the stronger artefact, because a reader can re-run them.

- Red, control A: rename one module back out of the selector, then

  ```bash
  git mv tests/test_ci_setup_sccache_action.py tests/test_setup_sccache_action.py
  uv run pytest tests/test_ci_test_selection_contract.py -q
  git mv tests/test_setup_sccache_action.py tests/test_ci_setup_sccache_action.py
  ```

  fails four tests. The named check and the per-module check both report, and
  the message names the module and both fixes:

  ```plaintext
  AssertionError: these root-level modules are collected by no target the suite
  runs, so nothing executes them:
    - tests/test_setup_sccache_action.py
  Fix by either renaming each module to `tests/test_ci_*.py`, which
  PYTEST_TARGETS already collects, or adding it to PYTEST_TARGETS in the
  Makefile. A module that is deliberately collected elsewhere can be listed in
  test_ci_test_selection_contract.EXCEPTIONS with its target and reason instead.
  ```

- Red, control B: delete the `tests/test_ci_*.py` pattern from
  `PYTEST_TARGETS` and run the same command. It fails 52 of 56 tests. This is
  the control that proves the covered set is not satisfied by some other
  pattern, and it also demonstrates the residual gap recorded above: the guard
  lives inside the selector it polices, so removing the pattern removes it too.
  The four survivors are the controls that need no selector.

- Green: on the final tree,

  ```bash
  uv run pytest tests/test_ci_test_selection_contract.py -q
  env -u BASH_ENV make test-python
  make test-dev-fast-contract
  ```

  report 56 passed; a full pass collecting 688 tests under `tests/test_ci_*.py`
  with all seven modules present by name; and 47 passed with 2 snapshots.

- Refactor: the helper was tidied after the first smoke test — continuation
  joining added, the `@` marker stripped from recipes, and the `Path`-versus-
  `str` comparison fixed. The guard and `make test-python` were re-run after
  each change.

Quality criteria:

- Tests: `make test-python` passes, collecting the seven renamed modules. The
  guard passes. `make test-dev-fast-contract` passes and collects
  `tests/test_ci_dev_fast_action.py`. `make markdownlint` passes after the
  developers'-guide edits.
- Verification: `SELECT-1` and `RESOLVE-1` discharged with the non-vacuity
  evidence above.
- Lint/typecheck: `make lint`, `make check-fmt`, and `make typecheck` pass,
  run sequentially by `scrutineer`.
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

`tests/helpers/makefile.py` exports four names; these are the signatures as
built, which differ from the plan's first sketch and are recorded here so a
reader is not misled by the earlier version:

```python
def makeutil_document(*, makefile: str = "Makefile") -> dict[str, Any]: ...
def variable_expansion(name: str, *, makefile: str = "Makefile") -> tuple[str, ...]: ...
def selected_paths(
    patterns: Iterable[str], *, root: Path | None = None
) -> tuple[Path, ...]: ...
def recipe_of(name: str, *, makefile: str = "Makefile") -> str: ...
```

`variable_expansion` returns the *words* of a named Makefile variable — a
tuple, not one string — after collapsing `\`-newline continuations the way
`make` does and expanding `$(VAR)` references recursively. It raises
`AssertionError` on an undefined reference or a cycle rather than substituting
an empty string. The `makefile` keyword exists so the helper's own tests can
parse a temporary Makefile; the guard never passes it.

`selected_paths` resolves patterns against the repository root and returns the
files they name, sorted and deduplicated. A pattern without a `.py` suffix
contributes nothing, and a pattern matching nothing contributes nothing — the
same behaviour as `make test-python`, whose loop skips a pattern whose first
expansion does not exist. A bare word in the selector is therefore a mistake
rather than a file, and the guard's
`test_the_selector_resolves_the_whole_root_module_population` is what makes
that visible.

`recipe_of` returns one target's recipe with `make`'s leading `@` stripped and
continuations collapsed, preserving line structure so a caller can still tell
one command from the next.

In `tests/test_ci_test_selection_contract.py`, the guard as built:

```python
EXCEPTIONS: Final[dict[str, tuple[str, str]]] = {}  # module -> (target, reason)


def test_every_root_level_module_has_a_ci_route() -> None: ...
def test_the_exception_mechanism_reports_an_uncovered_module() -> None: ...
def test_the_seven_reported_modules_are_now_collected() -> None: ...
def test_the_selector_resolves_the_whole_root_module_population() -> None: ...
def test_each_root_module_matches_a_selector_pattern(module: str) -> None: ...
def test_ci_invokes_the_target_that_consumes_the_selector() -> None: ...
def test_the_suite_target_recipe_consumes_the_selector() -> None: ...
```

Two tests beyond the plan's sketch are worth naming.
`test_the_exception_mechanism_reports_an_uncovered_module` is the seeded-fault
control: it drives `_remedy` with a module that cannot exist so the empty
exception table is exercised rather than assumed.
`test_the_suite_target_recipe_consumes_the_selector` closes the gap between the
Makefile and the workflow from the other side: a target named `test-python`
that ran a bare directory would satisfy the workflow check while collecting the
container-bound scenarios the repository deliberately keeps out.

`test_ci_invokes_the_target_that_consumes_the_selector` uses
`tests.helpers.workflow_shell.script_runs_command` and
`tests.helpers.ci_run_scripts.run_scripts` to prove a workflow `run:` step
invokes `make test-python`. Matching the command by its leading shell tokens
rather than by substring is what keeps a mention in a comment from satisfying
it.

`tests/helpers/ci_run_scripts.py` is a split of `tests/helpers/ci_workflows.py`
forced by the 400-line cap, not a new capability: `run_scripts` and its private
`_job_run_scripts` moved verbatim. `ci_workflows` keeps the named lookups, as
`workflow_shell`/`workflow_recipe` split on the same boundary.

Dependencies: `makeutil` 0.1.0, already pinned by
`.github/actions/install-makeutil` and already a prerequisite of `test` and
`test-python`; no new dependency is introduced.

## Revision note

2026-09-26, second revision. `EP-M1` through `EP-M3` are complete. This
revision records the Task 2 and Task 3 outcomes, the three defects found during
implementation (`makeutil` continuation backslashes, the `Path`/`str`
comparison, and the guard's placement inside its own selector), the corrected
`Interfaces` signatures, and the replaced Red-stage evidence.

2026-09-26, first revision. Records the completed triage, the rename route
decision, the discovery that the `#488` platform gate does not exist, and the
latent sampler timeout conflict that Task 1 must resolve.
