# Bring seven uncollected test modules into the default suite and guard the selector

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IMPLEMENTED; pull request #505 open for review at `3d1408c0`, with the
post-rebase review findings reconciled

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
  `PYTEST_TARGETS` fails the rest. The denominators in this entry were
  re-measured at `df3c59ad`; see `Validation and acceptance` for the current
  pair and for why the numbers drift.
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
- [x] (2026-09-26 19:25Z) Gate run at `6dbee0cd`: `check-fmt`, `typecheck`,
  `test`, `markdownlint`, and `nixie` green, but `make lint` red at
  `python-lint` → `df12-pylint`, which reported R9109 against the guard. That
  leaf aborts the recipe, so `ambrleaks`, `skylos`, `lint-clippy`,
  `lint-whitaker`, `yamllint`, and `actionlint` never ran and remain unobserved.
  `make test` itself was green: 2467 passed / 63 skipped in the Python suite,
  125 passed in Rust nextest, doctests ok.
- [x] (2026-09-26 19:40Z) Cleared R9109 by folding four
      `assert "<literal>" in recipe` probes into one `_RECIPE_ENDPOINTS` table.
      The rule's own suggestion — a syrupy snapshot of the recipe — was
      declined on evidence, not on preference:
      `tests/test_ci_act_harness_contract.py` documents this repository's
      recipe assertions as "structural rather than byte-exact, so reordering
      prerequisites or adding a flag does not fail the test", `AGENTS.md` warns
      against blob comparisons that legitimate formatting turns brittle, and
      `recipe_of` returns one joined line whose layout `make fmt` can reflow.
      The rewrite is strictly stronger than the probes it replaces: a recipe
      missing two endpoints now reports both rather than failing on the first
      and hiding the second. Verified non-vacuous by mutating each endpoint in
      turn — each mutation is reported by name, and the unmutated recipe
      reports nothing. Recorded here rather than silently: a future reader will
      see R9109 and wonder why the recipe is not snapshot-pinned.
- [x] (2026-09-26 20:15Z) Second gate run at `87e2dde7`: red again, at a
  *different* leaf — `ruff check` ISC004
  (`implicit-string-concatenation-in-collection-literal`) on the two
  concatenated prose strings the R9109 fix had just introduced into
  `_RECIPE_ENDPOINTS`. Fixing one finding created a blocking finding beside it
  in the same file; the two should have been pre-checked together rather than
  discovered one gate run at a time. Leaves two through eleven were unobserved
  again.
- [x] (2026-09-26 20:20Z) Cleared ISC004 by parenthesizing each concatenated
  pair, the shape `tests/helpers/parity.py` and
  `tests/behaviour/test_documentation_examples_behaviour.py` already use for
  the same construct. Before requesting the next gate run, both rule families
  were run directly against every changed Python file: `ruff check` over all
  eighteen changed paths exits 0, and df12-pylint with all thirteen rules
  enabled over the same set reports 10.00/10. The gate run should not be the
  discovery mechanism for a finding a one-file pre-check would have caught.
- [x] (2026-09-26 20:30Z) Third gate run at `5ccb4701`: **all six gates
  green**, and all eleven `make lint` leaves observed — ruff check,
  interrogate, pylint, df12-pylint, ambrleaks, skylos, lint-clippy,
  lint-whitaker, spelling, yamllint, actionlint. Logs are under
  `/tmp/R3-*-issue-499.out`, deliberately distinct from the two earlier runs'
  log paths so those findings' logs were not overwritten. `make test` green in
  all three suites: Python 2467 passed / 63 skipped, of which the
  `tests/test_ci_*.py` batch includes all seven renamed modules and this
  branch's own guard, with 2 syrupy snapshots passing; Rust nextest 125 passed;
  Rust doctests ok. The per-module counts quoted here — `test_ci_*.py` at 747
  and the guard at 59 — were true at `5ccb4701` and are superseded; at
  `df3c59ad` the same two are 750 and 58. Both revisions are green; only the
  sizes moved.
- [x] (2026-09-26 20:45Z) Gate evidence from here on is recorded on the pull
  request rather than in this document. The loop — edit the plan, run the
  gates, edit the plan again — invalidates each run as a citation, because a
  commit after a gate run moves the head the run certified. The plan is frozen
  at `704a4b03`; anything later belongs on the PR body.
- [x] (2026-09-28 03:00Z) Draft pull request #505 opened at `df3c59ad`, with
  the full seven-gate set green there. Two further commits followed (`ca5fb1fb`,
  `1e968b86`), both spanning the same single file under `docs/`.
- [x] (2026-09-28 07:20Z) The three lock-dependent gates were measured by CI
  instead, at `0220baf6`: run `36380609989` is `success` across all 17 jobs,
  including `lint-test` (which runs `/usr/bin/make ACTIONLINT=… lint` at
  `ci.yml:326` — the same target whose local run died at `lint-clippy`),
  `Typecheck and test` on 3.12/3.14/3.15a (which run `make test-python`), and
  `coverage` (which runs the Rust suite under `cargo llvm-cov nextest` with
  `all-targets`, `all-features`, and `doctests: 'true'`). Evidence:
  `/tmp/R15-ci-coverage-499.out`. Caveat recorded there too: a `pull_request`
  run checks out the **merge ref**, so this certifies the head *merged with
  main*, not the bare head. The two agree here because `git merge-tree` reports
  no conflicted path.
- [x] (2026-10-01) Pushed the branch and opened it for review. The SHAs this
  marker originally named — `ca5fb1fb`, `1e968b86` — were rewritten by the
  rebase onto `7b86b904` and no longer exist; the published head is the rebased
  one, `3d1408c0`, which is what `origin` records for this branch. Pull request
  #505 was marked ready for review (it had been a draft) and a full review was
  queued against that head.

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
- [x] (2026-10-01) Rebased the 30 surviving commits onto `origin/main`
  (`7b86b904`) and resolved the one conflict the rebase raised. Upstream's #559
  (`3e29ac27`) retired the local CodeScene contract family: it deleted
  `tests/test_codescene_environment_contract.py`, its
  `tests/helpers/codescene_environment_rules.py` reader (329 lines), and five
  sibling CodeScene modules, replacing them with shared CV-005 contracts. The
  branch's rename of that module therefore became a rename/delete conflict, and
  the renamed module was dropped: its only dependency was the deleted helper,
  so it could not run and there was nothing left for it to assert. Six of the
  seven renames survive upstream intact and replay cleanly. The final
  `typos.toml` regeneration was dropped by the rebase as already upstream,
  which is correct — the file is generated from the shared estate dictionary.
- [x] (2026-10-01) Updated the guard for the new base. The named-modules test
  went from seven to six, and its docstring now records why the seventh is
  gone. The population-wide checks then failed on
  `tests/test_workflow_bash_env_contract.py`, a module upstream added in #466
  that no selector collects — an independent recurrence of this issue's exact
  defect, caught by the guard this branch ships. Renamed it to
  `tests/test_ci_workflow_bash_env_contract.py`, the remedy the guard's own
  failure message prescribes, which keeps `EXCEPTIONS` empty as designed. All
  54 guard assertions pass.
- [x] (2026-10-01) Reconciled three review findings on the published head.
  Each was verified against the tree before repair, and each repair is a
  behaviour change rather than a comment edit:
  - *Reject unevaluated PR guard clauses* (the load-bearing one). The lane check
    read guards through `ci_leg_matrix.admits`, which resolves a clause over the
    leg alone and treats an unmodellable clause as **satisfied** — the safe
    superset direction for the cache-ownership caller, and exactly the wrong one
    for a check that asks "does this run on a pull request". A suite step gated
    `github.event_name == 'push' && matrix.python-suite && env.LEG_RUNS ==
    'true'` was therefore reported as running on two pull-request lanes while
    running on none. Fixed by giving the event-aware question its own resolver,
    `ci_leg_gate.admits_event`, which evaluates `github.event_name` comparisons
    against the event, reads matrix clauses over the leg, admits the status
    functions, and **refuses** any other clause rather than trusting it.
    `admits` is unchanged, because
    `tests/test_ci_cache_families_helper.py` pins its permissive contract as
    deliberate. The hole is now closed by two tests in
    `test_ci_suite_wiring_contract.py`: the report's own `push` guard must
    report no lane, the pull-request spelling of the same guard must report the
    lanes back, and an unmodellable `github.ref` clause must fail loudly.
  - *Non-mapping matrix `include` entries were dropped silently.* `matrix_legs`
    now refuses an entry that is not a mapping, naming the workflow, the job,
    and the index. A list of such entries previously expanded to zero legs,
    which makes every "some leg admits this" question vacuously false rather
    than loud.
  - *The ExecPlan's `Interfaces and dependencies` block had drifted from the
    code*: it attributed `selected_paths` to `tests/helpers/makefile.py` (it
    lives in `tests/helpers/suite_selection.py`), named a `_remedy` that is
    public as `remedy`, and cited a test name retired by the rebase. Corrected
    against the modules rather than the prose.
  The negative control for the first finding was run as a probe before the fix
  and recorded: the old reader returns `True` for the `push`-guarded step
  evaluated for a pull request, which is precisely the false certification the
  report describes.
- [x] (2026-10-01) Reconciled the three failed pre-merge rows (Testing,
  Unit Architecture, Developer Documentation) on the same head. The walkthrough
  was evaluated at `19be7820`, which the rebase rewrote, so every row was
  re-tested against the current tree rather than trusted either way — a stale
  anchor proves neither that a finding needs work nor that it does not. Two
  rows were live; the third was half stale.
  - *Unit Architecture — live, repaired.* `makeutil_document` leaked
    `FileNotFoundError` from `subprocess.run` although it documents only
    `AssertionError`, so a missing `makeutil` reported a broken toolchain as
    though the Makefile were at fault. Reproduced with a probe before the fix.
    `makeutil_document` now takes `root` and `runner`, translates process-start
    and timeout failures at that boundary, and `variable_expansion` and
    `recipe_of` propagate both through.
  - *Testing — live, repaired.* Two blind spots, each reproduced. A resolver
    that returned `frozenset(root_modules())` satisfied every guard assertion
    (`uncovered() -> 0`), because after #499 the enumerated population and the
    selector's expansion coincide, so an equality check cannot separate
    "reads the selector" from "returns the tree"; the discriminating control
    narrows the selector and requires the resolver to follow it. And
    `recipe_of` joins the recipe onto one line, so a `#` disables it while the
    words survive a substring check. `recipe_tokens` now tokenizes with comment
    markers honoured, and
    `test_a_commented_out_recipe_does_not_satisfy_the_endpoint_check` seeds
    that fault on the reader. Focused parsing-boundary tests for
    `makefile.py`, `ci_documents.py`, and `ci_run_scripts.py` live in
    `tests/test_ci_helper_boundaries.py`.
  - *Developer Documentation — partly stale, remainder repaired.* The
    `selected_paths` misattribution had already been corrected by the previous
    entry, so that half needed no work. Still live: `ci_documents` and
    `ci_run_scripts` were undocumented, and the interface block claimed
    "four names" while the module exports seven and omits `MAKEFILE`. Both
    corrected; the stale `Status:` line and the unchecked push marker are
    updated above.
  Every repair was re-tested with a negative control that was shown to
  discriminate: injecting the fault produced exactly the intended failure, and
  the injection was reverted before the next gate run.
- [x] (2026-10-01) Cleared two gate failures the repairs introduced, both
  caught by the first full gate run at `2bb733f9`.
  - *`make lint` / pylint-classic C0302.* The repairs took
    `tests/helpers/makefile.py` to 528 lines against pylint's 400-line module
    cap. `tests` is in `PYLINT_STRICT_TARGETS`, and the exemption list
    (`cuprum/unittests`, `scripts/tests`, `tests/behaviour`, `tests/features`)
    does not cover it. The gate aborted there, so the checks after it in the
    recipe — ruff, interrogate, DF12 pylint, ambrleaks, Skylos, rust-lint, and
    the workflow lint — were *unobserved* at that candidate rather than
    passing; they only regained evidence on the re-run.
    Fixed by splitting on the family's usual seam, as `workflow_shell` and
    `workflow_recipe` already do and as the plan's own risk table
    (`AGENTS/400-line-limit -> EP-M3 -> tests/helpers/makefile.py line count`)
    anticipated. `tests/helpers/makeutil.py` (196 lines) owns the process
    boundary — `MAKEFILE`, `MAKEUTIL_TIMEOUT_SECONDS`, `Runner`,
    `DEFAULT_RUNNER`, and `makeutil_document` — while `makefile.py`
    (390 lines) owns `make`'s semantics for the parsed document and
    re-exports the three names a caller of either half needs. `DEFAULT_RUNNER`
    exists so the semantics half can default to `subprocess.run` without
    importing `subprocess`. The shared `require` is `ci_documents.require`
    rather than a local copy, per the export-and-reuse sweep `AGENTS.md`
    requires; it is a leaf (`strict_yaml` only), so no cycle closes. The split
    is behaviour-preserving: the same 86 contract assertions pass unchanged.
  - *`make markdownlint` / spelling.*
    `test_the_success_path_finds_this_repositorys_scripts`
    tripped the en-GB gate on `repositorys`. Renamed to
    `test_the_success_path_finds_the_estates_scripts`, which is the term the
    surrounding modules already use.

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
  selector plainly names. Evidence: reproduced at `f87139f3`, the revision that
  introduced the guard, by re-injecting the bare comparison —
  `54 failed, 2 passed`, the two survivors being the controls that touch
  neither selector. That revision enumerated 50 root modules, which with its 6
  fixed tests is the 56 the denominator names. Impact: the comparison now uses
  a module-level `_TESTS_DIR = pth.Path(_TESTS)`, and the constant's comment
  records why a string comparison cannot be used here.

- Observation: the guard's own name puts it inside the selector it polices.
  `tests/test_ci_test_selection_contract.py` matches `tests/test_ci_*.py`, so
  removing that pattern also removes the guard. Evidence: negative control B,
  which deleted the pattern, failed 54 of the suite's 58 cases rather than all
  of them. The four survivors are three of the five controls that need no
  selector, plus the single parametrized case the selector still resolves —
  `test_each_root_module_matches_a_selector_pattern[tests/test_native_sdist.py]`,
  which survives because that module is named in `PYTEST_TARGETS` as a literal
  path rather than reached through the deleted glob. Impact: accepted and
  recorded in `Context and orientation` as a residual gap. The alternative —
  putting the guard outside the selector — is not collected either, so it would
  guard nothing. The `test_ci_` family is the right home; a repository-wide
  selector rewrite is a bigger change than issue #499 asks for.

- Observation: the host's Cargo package cache deadlocked at 02:53:12 on the
  day this revision was written, and it did not recover. The holder is pid
  1832225, a `cargo test --all-targets --all-features` in an unrelated
  repository's worktree, holding an exclusive `flock` on
  `~/.cargo/.package-cache-mutate`. Its grandchild pid 1855450 is a nested
  cargo, launched by a `trybuild` test, waiting in `locks_lock_inode_wait` for
  that same file. The holder waits in `do_wait` for the child that waits for
  the holder: a cycle no amount of waiting can break. Sixty-four processes
  across thirteen worktrees were queued behind it, including peers belonging to
  six other sessions on this machine. Evidence: `/proc/locks` shows the write
  lock and its waiter chain under one inode; `ps -o stat,wchan` shows `SN`/
  `do_wait` on the holder and `SN`/`locks_lock_inode_wait` on the nested cargo;
  `ps -o etime` showed the holder at four hours with every cargo process at
  0.00% CPU. Impact: three of the seven commit gates — `make lint`,
  `make test-python`, `make test-rust` — could not complete at this revision
  and are recorded as unobserved rather than passed. The other four
  (`check-fmt`, `markdownlint`, `typecheck`, `nixie`) do not take the lock and
  all passed at this head. This is a host fault that happened to coincide with
  the change; it is not caused by it, and the branch's two lock-sensitive
  failures were each shown to pass where the lock was free. Recovery: the
  affected trees belong to other sessions; per the standing rule against
  killing another agent's processes and against working around the cache with a
  private one, the deadlock is reported rather than worked around.

- Observation: the first annotation written against the aborted
  `make test-python` log was itself wrong. It reported the run as reaching 47%
  with one test failed; the log body shows 51% with two failed. The 47% marker
  belongs to the `test_maturin_build.py` batch, which the run cleared, and the
  second failure — `test_maturin_wheel_build_snapshot` — was missed because the
  annotation was written from an intermediate reading rather than measured
  against the log. Evidence: re-deriving the markers and the pass/fail tallies
  from lines 1-1331 of the log gave 51%, 1309 passed, 2 failed, 1 skipped, 0
  errors. Impact: the annotation stands corrected in place, with the error left
  visible beside its fix rather than edited away. The lesson is the one this
  document keeps relearning: an annotation about a run is a claim about that
  run and has to be measured from it, not recalled.

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
`tests/test_ci_*.py` pattern collecting 750 tests, and each of the seven
appears in the collected set by name. `make test-dev-fast-contract` collects
the renamed dev-fast module and its two snapshots. The guard,
`tests/test_ci_test_selection_contract.py`, passes 58 tests and rejects both
negative controls: renaming one module back out of the selector fails four of
its tests naming that module, and deleting `tests/test_ci_*.py` from
`PYTEST_TARGETS` fails 54. The rule is documented under "Test selection" in the
developers' guide.

These counts are as measured at revision `df3c59ad`. The guard's own size grows
whenever a root-level module is added, so a later reader who re-runs the
commands in `Validation and acceptance` should expect a larger denominator and
should not read the difference as a change in what the tests assert.

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

Post-rebase outcome (2026-10-01). At `d16904f5`, on a base of `7b86b904`:
`make check-fmt`, `make lint`, `make markdownlint`, `make nixie`,
`make typecheck`, `make test-python`, and `make test-rust` all exit 0 with the
tree clean before and after. `make test-python` reports 2537 passed and 70
skipped; `make test-rust` reports 127 passed and 0 skipped. The guard passes 54
tests, and both negative controls still reject, on the smaller denominator the
retired seventh module leaves: renaming one module out of the selector fails
`4 failed, 50 passed`, and deleting `tests/test_ci_*.py` from `PYTEST_TARGETS`
fails `50 failed, 4 passed`. Six of issue #499's modules are in the suite by
name, the seventh is gone upstream, and
`tests/test_ci_workflow_bash_env_contract.py` — upstream's own uncollected
module — is collected too.

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

`test_dev_fast_action.ambr` is the one syrupy snapshot file affected; it lives
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

  fails four of the 58 collected cases. The named check and the per-module
  check both report, and the message names the module and both fixes:

  The rename *target* decides the count, which matters for a reader reproducing
  this. Renaming to a name that still matches the enumeration glob
  `tests/test_*.py` — as the command above does — leaves the module visible to
  `root_modules`, so the population check, the named check, the population
  cross-check, and that module's own parametrized case all report. Renaming it
  outside the glob instead, such as to `tests/setup_sccache_action_out.py`,
  hides the module from the enumeration and only the named check fires: 1
  failed, 56 passed. Both spellings are correct controls; they exercise
  different halves of the machinery, and the four-test form is the one that
  reaches the per-module report.

  ```plaintext
  AssertionError: these root-level modules are collected by no target the suite
  runs, so nothing executes them:
    - tests/test_setup_sccache_action.py
  Fix by either renaming each module to `tests/test_ci_*.py`, which
  PYTEST_TARGETS already collects, or adding it to PYTEST_TARGETS in the
  Makefile. A module that is deliberately collected elsewhere can be listed in
  tests.helpers.suite_selection.EXCEPTIONS as an Exemption naming its selector,
  target, and reason instead.
  ```

- Red, control B: delete the `tests/test_ci_*.py` pattern from
  `PYTEST_TARGETS` and run the same command. It fails 54 of the 58 collected
  cases. This is the control that proves the covered set is not satisfied by
  some other pattern, and it also demonstrates the residual gap recorded above:
  the guard lives inside the selector it polices, so removing the pattern
  removes it too.

  The four survivors are three of the five controls that need no selector —
  `test_the_exception_mechanism_reports_an_uncovered_module`,
  `test_an_exemption_naming_a_target_that_ignores_its_selector_is_rejected`, and
  `test_a_module_the_selector_drops_is_reported_as_uncovered` — plus the
  single parametrized case the selector still resolves. That last one is
  `test_each_root_module_matches_a_selector_pattern[tests/test_native_sdist.py]`,
  which survives because `tests/test_native_sdist.py` is named in
  `PYTEST_TARGETS` as a literal path rather than reached through the deleted
  glob. Its survival is what the pattern deletion does *not* reach, not an
  artefact of the fixture. The suite collects 58 cases: 51 parametrized (one
  per root-level `tests/test_*.py` module) and 7 fixed, five of which are
  selector-independent.

- Green: on the final tree,

  ```bash
  uv run pytest tests/test_ci_test_selection_contract.py -q
  env -u BASH_ENV make test-python
  make test-dev-fast-contract
  ```

  report 58 passed at revision `df3c59ad`; the full suite at 2467 passed and 63
  skipped, within which the `tests/test_ci_*.py` batch is 750 passed with all
  seven modules present by name; and 47 passed with 2 snapshots.

  The figures in this section were re-measured at `df3c59ad` rather than
  carried forward. They drift between revisions as the suite grows, and two of
  them had gone stale in the previous revision of this document: the guard's
  own count read 56 where the tree collected 58, and the batch read 688 where
  it collected 750. A reader comparing the pairs is seeing that drift, not a
  regression. Each figure here is what the command above printed at the head
  named, and the exact commands are given so any of them can be re-derived.

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

The reader is split across two modules, on the family's usual seam:
`tests/helpers/makeutil.py` owns the process — reaching for the parser, running
it, and reporting the ways a process fails — while `tests/helpers/makefile.py`
owns `make`'s own semantics for the document it returns. These are the
signatures as built, which differ from the plan's first sketch and are recorded
here so a reader is not misled by the earlier version:

```python
# In tests/helpers/makeutil.py:
MAKEFILE: Final[str] = "Makefile"
MAKEUTIL_TIMEOUT_SECONDS: Final[int] = 60
type Runner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_RUNNER: Runner = subprocess.run


def makeutil_document(
    *,
    makefile: str = MAKEFILE,
    root: Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> dict[str, Any]: ...


# In tests/helpers/makefile.py:
def variable_expansion(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> tuple[str, ...]: ...


def recipe_of(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> str: ...


def recipe_tokens(recipe: str) -> tuple[str, ...]: ...
```

`variable_expansion` returns the *words* of a named Makefile variable — a
tuple, not one string — after collapsing `\`-newline continuations the way
`make` does and expanding `$(VAR)` references recursively. It raises
`AssertionError` on an undefined reference or a cycle rather than substituting
an empty string.

The `makefile` keyword exists so the helper's own tests can parse a temporary
Makefile; the guard never passes it, and `MAKEFILE` names the default so a
second reader cannot hard-code the same string. `root` and `runner` expose the
process boundary: `root` is the directory the parse runs in, defaulting to the
repository root, and `runner` is the process callable, defaulting to
`DEFAULT_RUNNER` — `subprocess.run`, aliased so the semantics half can default
to it without importing `subprocess` itself. Both exist so
`makeutil_document`'s failure modes — a non-zero exit, malformed JSON, a
missing binary, a wedge that outlives `MAKEUTIL_TIMEOUT_SECONDS` — are
exercised with a fake process rather than by installing or breaking the real
`makeutil`. Process-start and timeout failures are translated at that boundary
into the documented `AssertionError`, because `subprocess.run` raises
`FileNotFoundError` for a binary it cannot start and that is not the error a
read API may leak; `variable_expansion` and `recipe_of` propagate the same
failure through the `root` and `runner` they accept.

`recipe_tokens` exists for the recipe contract in
`tests/test_ci_suite_wiring_contract.py`. `recipe_of` joins a target's recipe
onto one line, so a single `#` comments out every command after it while the
words stay in the string; tokenizing with `shlex` and comment markers honoured
is what makes the endpoint assertion a claim about what the shell would run
rather than about text that happens to survive.

`selected_paths` lives in `tests/helpers/suite_selection.py`, not in the
Makefile reader, because resolving patterns needs the root-module enumeration's
view of the tree rather than the Makefile's. Its signature:

```python
# In tests/helpers/suite_selection.py:
def selected_paths(
    patterns: Iterable[str], *, root: Path | None = None
) -> tuple[Path, ...]: ...
```

It resolves patterns against the repository root and returns the files they
name, sorted and deduplicated. A pattern without a `.py` suffix contributes
nothing, and a pattern matching nothing contributes nothing — the same
behaviour as `make test-python`, whose loop skips a pattern whose first
expansion does not exist. A bare word in the selector is therefore a mistake
rather than a file, and the guard's
`test_the_selector_resolves_the_whole_root_module_population` is what makes
that visible.

`recipe_of` returns one target's recipe with `make`'s leading `@` stripped and
continuations collapsed, preserving line structure so a caller can still tell
one command from the next.

In `tests/test_ci_test_selection_contract.py`, the guard as built:

```python
# In tests/helpers/suite_selection.py:
EXCEPTIONS: Final[dict[str, Exemption]] = {}  # module -> (selector, target, reason)


def test_every_root_level_module_has_a_ci_route() -> None: ...
def test_the_exception_mechanism_reports_an_uncovered_module() -> None: ...
def test_the_six_reported_modules_are_now_collected() -> None: ...
def test_the_selector_resolves_the_whole_root_module_population() -> None: ...
def test_each_root_module_matches_a_selector_pattern(module: str) -> None: ...
def test_ci_invokes_the_target_that_consumes_the_selector() -> None: ...
def test_the_suite_target_recipe_consumes_the_selector() -> None: ...
```

The two guard readings of a step guard both live in `ci_leg_matrix.py` as
built, because the second is the first plus one more resolved value rather than
a different subject:

```python
# In tests/helpers/ci_leg_matrix.py:
def matrix_legs(workflow_name: str, job_name: str) -> list[dict[str, object]]: ...
def clauses(condition: str) -> list[str]: ...
def admits(condition: object, leg: Mapping[str, object]) -> bool: ...
def admits_event(
    condition: str, leg: Mapping[str, object], event: str, *, subject: str
) -> bool: ...
```

`admits` resolves a clause over the leg alone and reads anything else as
satisfied — the superset direction the cache-ownership caller needs, pinned as
deliberate by `tests/test_ci_cache_families_helper.py`. `admits_event` is the
reading the pull-request question needs: it evaluates `github.event_name`
comparisons against the event, delegates the matrix clauses to `admits`, admits
the status functions (`always()` and siblings), and **refuses** any clause
outside those three forms. `ci_leg_gate.py` keeps the leg flag — `ungated`,
`flag_holds_on` — and composes the two in `pull_request_legs`, which is the
only caller of `admits_event`.

Two tests beyond the plan's sketch are worth naming.
`test_the_exception_mechanism_reports_an_uncovered_module` is the seeded-fault
control: it drives `remedy` with a module that cannot exist so the empty
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

2026-09-28, sixth revision. Supplements the fifth with the measurement that
closes the gap it left open: CI.

The fifth revision recorded the three lock-dependent gates as unobserved,
because the host could not run them. They have since been run — not on this
host, but in CI, at `0220baf6`. Run `36380609989` is `success` on all 17 jobs.
The jobs that matter are `lint-test`, `Typecheck and test` on 3.12, 3.14 and
3.15a, and `coverage`, and between them they invoke exactly the three targets
this host could not: `make lint`, `make test-python`, and the Rust suite under
an instrumented `cargo llvm-cov nextest`. The command that matters most is
`ci.yml:326`, `/usr/bin/make ACTIONLINT="$GITHUB_WORKSPACE/actionlint" lint` —
the same `make lint` that terminated locally at `lint-clippy`.

Two precision points, because the citation is only worth its caveats. First, a
`pull_request` run checks out the **merge ref**, so CI certified the head
merged with `main` rather than the bare head; the two agree here because
`git merge-tree HEAD origin/main` reports no conflicted path, but they are not
the same object and the record should not pretend otherwise. Second, the
literal string `make lint` occurs in `ci.yml` only inside comments, at lines
221 and 583. A grep for it finds the comments and misses the step, which is how
the first pass of this revision wrongly concluded CI does not run `make lint`
at all. The step is real; it is spelled with an environment prefix.

2026-09-28, fifth revision. Records a host fault that interrupted delivery, and
corrects an annotation this document's own author wrote about it.

The host's Cargo package cache deadlocked at 02:53:12 and stayed deadlocked.
One repository's `cargo test` holds the cache's exclusive lock while waiting on
a nested cargo its own `trybuild` test launched, which waits for that lock: a
cycle. Sixty-four processes across thirteen worktrees queued behind it. Three
of the seven commit gates — `make lint`, `make test-python`, and
`make test-rust` — take that lock, and none could complete at `1e968b86`. They
are recorded as unobserved at that head, not as passed. The remaining four
gates do not take the lock and all passed there.

The interruption does not put the change in doubt, and the reason is stronger
than an assurance. Everything the three blocked gates read is byte-identical
between `1e968b86` and `df3c59ad`, where all three passed in full: the branch
delta between those two revisions is one file under `docs/`, and the subtree
hashes of `tests/`, `rust/`, `cuprum/`, `Makefile`, and `pyproject.toml` are
equal. The gates are unobserved because the host could not run them, not
because they might fail. The reader should nonetheless treat the three as
outstanding work rather than letting the byte-identity argument stand in for a
measurement; the plan's `Progress` keeps them open for that reason.

The correction is to this revision's own first draft. The annotation written
against the aborted `make test-python` log reported 47% and one failure; the
log holds 51% and two. The second failure, in the maturin build, was missed,
and the percentage was transcribed from an intermediate reading rather than
measured. Both are corrected in the log in place, with the error left visible
beside its fix.

2026-09-28, fourth revision. Every quantitative claim in this document that is
not anchored to a revision was re-derived at `df3c59ad`, and the unanchored
ones had drifted. Nothing about the change's behaviour is different; this
revision corrects the record.

- The three test counts in `Validation and acceptance` and `Outcomes` were
  measured afresh: the guard reads 58 collected cases, the `tests/test_ci_*.py`
  batch 750, the full Python suite 2467 passed / 63 skipped, and
  `test-dev-fast-contract` 47 passed with 2 snapshots. The document previously
  said 56, 688, and — correctly — 2467/63 and 47. The sentence claiming the
  batch figure showed the guard's own size was also wrong about which command
  reports which number; it now names each command against each figure.

- The two negative controls were re-run rather than carried. Control B
  measures 54 failed of 58, not "52 of 56"; the four survivors are now named,
  including the one that survives because `tests/test_native_sdist.py` is a
  literal `PYTEST_TARGETS` entry rather than a glob match. Control A measures
  four failures or one, depending on whether the rename target still matches
  the enumeration glob — a distinction the document did not make, and which a
  reader reproducing it needs. Both spellings are recorded.

- The third revision's note gave the controls as "4 of 62" and "54 failed / 8
  passed". Both are right, and 62 is not a single module's size: it is the two
  guard modules together, `test_ci_test_selection_contract.py` (58) plus
  `test_ci_suite_wiring_contract.py` (4). Re-measured over that union, control
  A is `4 failed, 58 passed` and control B is `54 failed, 8 passed` — the pair
  the note recorded. The figures elsewhere in this document are scoped to the
  selection module alone, where the same controls read 4 of 58 and 54 of 58;
  the two scopes are both correct and differ only in which modules are
  collected, which is why the counts in this document name their scope.

  62 was briefly declared unreachable here, on the strength of a single-module
  measurement. That was this revision's own error: the denominator was quoted
  from a two-module run and checked against a one-module collection, and the
  disagreement was attributed to the figure rather than to the scope. It is the
  reason the corrected sentences name both the command and the modules it
  collects.

- One figure survived scrutiny and is now anchored rather than corrected. The
  `Surprises` entry reporting that the bare `Path`-versus-`str` comparison
  failed "54 of 56" guard tests is exactly right — reproduced at `f87139f3`, by
  re-injecting the comparison, at `54 failed, 2 passed`. Its companion claim
  that the failures named "all 49 root modules" was off by one: that revision
  enumerated 50. The entry now records the reproduction, the revision it
  applies to, and why 56 is the denominator there.

- The parenthesized file sizes in `Progress` (97, 358, 181, 339, 226, 319
  lines) were checked against the revisions that created each module and are
  all exact at those revisions. They are provenance, not current sizes, and are
  deliberately left as written.

- The spellings in this note were themselves wrong on first writing. It
  introduced the American spelling of "artefact", and the "-ised" form of
  "parenthesized", into a document that had held neither; `make spelling`
  failed on both at the same head — the repository's house style is British,
  and the `typos` correction table maps the second token to the other spelling
  outright. Both are now corrected. The lesson is narrower than the count one:
  prose written *about* the corrections is still prose, and the gates read it
  like any other. The tokens were found by re-running the spelling gate rather
  than by reading the diff, which is why the fix carries its own gate evidence.

The lesson is the one `Prose numbers need re-derivation` already records, met
again: a count copied forward through three revisions of a document that keeps
growing is a claim about a tree that no longer exists. Each figure now either
carries its revision or states the command that re-derives it.

2026-09-28, third revision. A CodeRabbit pass over the pushed head reported
seven findings, all of which were verified against the tree before any edit;
six were real, one misread a correct sentence. The verification changed two of
them, and one self-measured defect was found alongside.

- `flag_holds_on` returned `False` — "every leg switched off" — for a flag
  expression it could not read, while its docstring promised an
  `AssertionError`. An unreadable predicate produced no `_FLAG_TERMS` matches,
  so the loop left `holds` at its initial `True` and the function returned
  `not holds`. Confirmed by overriding `job_env` with
  `${{ !(github.repository_owner == 'leynos') }}`: the call returned `False`
  rather than raising. The function now refuses a predicate naming neither a
  matrix key nor an event, which is the same class of failure the surrounding
  branches already report.
- The suite-wiring docstring described the 3.13 exclusion as satisfying a
  substring check while the merge lane collected nothing. Measured, that is not
  the mechanism: re-gating the step on the 3.13 leg alone still passes
  `assert lanes` and fails only the separate lane-count assertion. The
  docstring now states the measured behaviour, and a third assertion pins the
  exclusion that makes the count meaningful.
- The lane-count assertion itself was the subtlest of the three. Checking
  `admitted < len(matrix_legs(...))` would have been vacuous — the experimental
  leg is never enabled on a pull request, so an admit-everything guard yields
  three admitted lanes against four declared legs and passes. The denominator
  is the flag-enabled legs, and the negative control confirms the corrected
  assertion fires on a seeded admit-all guard.
- Three copies of `require` existed across `ci_documents`, `ci_placement`, and
  `suite_selection`; `ci_placement`'s own docstring called itself the
  alternative to "a third copy". Consolidating into `ci_documents` is forced by
  the import graph: `ci_placement` depends on `ci_workflows`, which depends on
  `ci_documents`, so hosting the shared helper in `ci_placement` would close a
  cycle — exactly the fallback the review suggested. Verified after the change
  that all six importing modules resolve `require` to one object.
- `variable_expansion`'s docstring claimed the parser had already collapsed
  continuation backslashes. The opposite is true: `makeutil` reports
  `raw_value` with `\\\n` intact, which is why `_expand` calls
  `_join_continuations`. Both halves were measured before rewriting the claim.
- `covered_modules` was re-resolved in each of the 51 parametrized cases, at
  16 ms apiece. A module-scoped fixture removes the redundancy; the
  parametrization still collects 51 cases and the monkeypatch seam still fires.

One earlier commit message is wrong and is corrected here rather than by
history surgery. `7e875b7b` says it moves `pull_request_legs` to `ci_leg_gate`.
It does not: `git show 7e875b7b` contains a single `+def pull_request_legs` and
no deletion of that function anywhere, and the name is absent from the parent
revision. The function was newly written as part of that commit. Rewriting a
published commit would need a force-push, so the record is corrected here;
treating the message as accurate would have the next reader hunting for a
predecessor that never existed.

A second finding was declined as stated: the execplan sentence the review read
as naming a non-existent path does place its subject under `tests/`, but the
same sentence names the correct snapshot path immediately after; the queue is
one filename, not a claimed repository path. The redundant `tests/` prefix was
dropped so the sentence cannot be misread that way again.

2026-09-26, second revision. `EP-M1` through `EP-M3` are complete. This
revision records the Task 2 and Task 3 outcomes, the three defects found during
implementation (`makeutil` continuation backslashes, the `Path`/`str`
comparison, and the guard's placement inside its own selector), the corrected
`Interfaces` signatures, and the replaced Red-stage evidence.

2026-09-26, first revision. Records the completed triage, the rename route
decision, the discovery that the `#488` platform gate does not exist, and the
latent sampler timeout conflict that Task 1 must resolve.
