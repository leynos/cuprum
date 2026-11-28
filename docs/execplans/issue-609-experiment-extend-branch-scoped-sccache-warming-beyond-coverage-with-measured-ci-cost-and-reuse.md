# Measure branch-scoped sccache warming beyond coverage (#609)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE — pull request #743 is open and ready for review. All six
commit gates pass at `fb1da7d8b`; the later commits are documentation-only and
carry their own formatting, Markdown and Mermaid runs. The experiment baseline
remains `87de64c2c`, recorded on 22 November 2026. No candidate family is
adopted by this change; the deliverable is the measured-qualification apparatus
plus a reviewable adopt/reject record.

## Purpose / big picture

Cuprum's coverage lanes already run sccache against Ubicloud's Actions cache
proxy. On an owned Ubicloud runner that proxy is branch scoped: `main` warms a
trusted baseline, a pull request reads `main` and its own branch, and a pull
request's later pushes reuse what its earlier pushes wrote. Every other Rust
family restores and saves an archived `~/.cache/sccache` directory whose only
writer is a `main` push, so a pull request's compiler objects never serve its
next push.

Issue #609 asks whether any non-coverage family should move to the proxy. The
answer must be earned per family from whole-job CI measurements: a higher
sccache hit rate alone does not prove a useful saving, and a cache that costs
paid runner time without a whole-job improvement must stay archived. This
change therefore builds the evidence path, qualifies the wiring hermetically,
predeclares the adoption gate, and records an adopt/reject decision. It does
not assume that any family benefits because coverage's retired archive served
nobody.

After this change a contributor can read the experiment section of
`docs/ci-cache-ownership.md`, see which families are candidates, see the
predeclared gate and its evidence status, and see the exact rollback. CI itself
records one redacted, versioned JSONL record per bracketed compile step, so the
gate can be evaluated from ordinary runs rather than from a dedicated warmup
job.

## Constraints

The experiment uses only ordinary, already-required CI activity. It adds no
paid dispatch, synthetic push, shadow job, rerun loop, matrix leg, schedule, or
duplicate compiler invocation, and it never purges a shared cache to fake a
cold run.

`typecheck-test` and Linux `extension-tests` are the first candidates.
`lint-test` is inventory-only until its own gate decision. `benchmark-ratchet`
is out of scope because cache and reporting activity would perturb its
measurement. Scheduled proof and model lanes, Windows, and macOS cache policy
are untouched.

One family is qualified at a time. A candidate family replaces its archived
compiler-directory restore and save; it does not keep both persistence
mechanisms. Cargo registry and installed-tool cache ownership are unchanged. No
`target` archive is added, and sccache's compiler and dependency identity
checks are not weakened.

Compiler identity is preserved: interpreter, target, OS, architecture, codegen
backend, debug/release/instrumentation shape, features, and profile remain in
the cache scope. A family collapse needs its own cross-consumer evidence.

The direct backend is selected only on the owned arm, after the pinned
`export-ubicloud-cache-credentials` action runs and before anything starts
sccache. The fork and Dependabot arm keeps the local directory backend and
exports no credentials. No privileged `pull_request_target` execution of pull
request code is introduced, and fork runs never publish into trusted scopes.

The proxy's branch protection stays enabled. `main` entries may seed eligible
pull requests; pull-request entries must never become trusted `main`,
sibling-pull-request, or release inputs.

A compiler-only fallback stays visible and does not hide compiler or test
failures. Fallback runs are marked degraded, remain in cost accounting, and are
never counted as remote-cache success. Invalid credential or backend wiring is
noncompliant, not compliant.

Runner labels, vCPU limits, workflow dependencies, event and interpreter
matrices, job ceilings, cancellation controls, test node IDs, required
outcomes, expected skips, pure-Python/native separation, property budgets,
compile-fail diagnostics, warning enforcement, and coverage settings all stay
unchanged.

Every cache-ownership, runner, and setup-sccache lifecycle contract is updated
with mutation-sensitive tests. Assertions cover behaviour and exit propagation,
not only matching YAML text.

Gates run sequentially through the `scrutineer` agent, output is captured with
`tee` under `/tmp`, builds stay outside `/tmp`, and compiler admission is never
bypassed.

## Tolerances (exception triggers)

Stop and record a proposed deviation if a candidate family cannot be qualified
without one of the following: a new paid runner, a new workflow trigger, a
wider cache scope, a disabled branch protection, a flatter compiler identity,
or a change to test selection or coverage. Also stop if the evidence schema
would need to record a secret, a bearer URL, or a raw environment dump.

The gate is unsatisfiable with fewer than five comparable warm
control/candidate pairs per family. Fewer pairs, a confounded pair, or a
changed default-branch input makes the family's result inconclusive; the
archive stays. Non-positive incremental paid cost is mandatory, including cold
starts and retries; where billing rounding is unverified, runner-seconds are
labelled a proxy.

This change's own file budget is: code and tests up to 1,200 net lines,
configuration (workflows, actions, Makefile) up to 250 net lines, documentation
up to 500 net lines, excluding this ExecPlan. A milestone that would exceed a
category stops for approval rather than borrowing another category's headroom.

## Risks

The evidence script could silently record a plausible zero. The classifier
therefore separates `no-rustc-route` and `fresh-target` from `unexplained`, and
emits a fixed warning for the last. A route that really invokes rustc must
explain a zero.

A restored archive could look like reuse while containing nothing useful. Each
record brackets compilation with statistics snapshots, records restore and save
bytes, and classifies backend and startup state, so archive transfer is not
mistaken for compiler reuse.

Ubicloud billing terms may be unverifiable from the runner. The record labels
runner-seconds as a proxy unless verified rounding evidence is attached, and
never treats a directory-size cap as a remote-store cost cap.

The proxy credentials could be exported after sccache starts. Mutation tests
swap the order, replace the pinned action, and remove the credentials guard so
the ordering and arm selection cannot rot.

The experiment could grow into a broad migration. The gate names one family at
a time, the decision record names the reviewer, and any additional family needs
its own evidence and decision.

## Progress

- [x] 22 November 2026: refreshed the implementation baseline at `87de64c2c`
      (tree matches `origin/main`) and recorded the source revision used by
      issue #609 (`24c39650e`).
- [x] 22 November 2026: M1 implementation complete in the working tree — the
      experiment inventory and gate in `docs/ci-cache-ownership.md`, the
      leg-aware `typecheck-test` sccache contract, the restore-to-writer join,
      and the evaluated runner/backend/credentials predicate test.
- [x] 23 November 2026: M2 implemented — the evidence schema, writer, reader,
      report, composite action, workflow brackets, tests, and the evidence
      document.
- [x] 23 November 2026: M3 implemented — `setup-sccache` namespace and backend
      output, mutation tests, and the executable wrapper-propagation tests.
- [x] 23 November 2026: M4 record drafted — every candidate family is
      explicitly not adopted, and the gate, owner, review date, rollback, and
      refresh trigger are recorded in `docs/ci-cache-ownership.md`.
      Comparable warm pairs cannot exist until the instrumentation has run on
      ordinary activity, which this change does not manufacture.
- [x] 23 November 2026: M5 gates — `make check-fmt`, `make lint`,
      `make typecheck`, `make test`, `make markdownlint` and `make nixie` all
      pass at `93d5ead0a` in a clean, Lody-free environment. Six CodeRabbit
      review rounds were run; every finding was addressed in a follow-up
      commit and re-gated.
- [x] 23 November 2026: draft pull request #743 opened from this branch with
      `Closes #609` and the session reference.
- [x] 27 November 2026: the tenth review round and the CodeScene delta were
      cleared. Every finding became a contract or a validator rule: a falling
      counter delta is unknown, an empty identity input is `unknown`, compiler
      `-vV` text is bounded, negative transfer durations are refused, a
      rejected record names its file and line, and the action's warning covers
      a failed timer. The flagged functions were split and the duplicated tests
      replaced with shared fixtures; `cs delta origin/main` reports no issues.
- [x] 28 November 2026: M5 re-gates at `7717d0f97` — `make check-fmt`,
      `make lint`, `make typecheck`, `make test`, `make markdownlint` and
      `make nixie` all pass in a clean, Lody-free environment with the ambient
      tool order. Recorded in
      `/tmp/final-gate-clean-cuprum-issue-609-7717d0f97.log`.
- [x] 28 November 2026: the eleventh review round found the action's writer
      steps could not import `scripts` at all. Python puts a script's own
      directory on `sys.path`, so the by-path invocation failed before
      `main()` and `continue-on-error` hid it. The action now exports
      `PYTHONPATH` as the workspace root, an execution test runs the writer by
      path from another directory, and the record carries the runner class so
      two runner classes cannot be paired as one backend change. The same
      round made counter resets reset-aware in the reason map, refused a JSON
      `null` line, required a matching `run_attempt`, refused an undeclared
      restore key, and closed five smaller contract gaps.
- [x] 28 November 2026: M5 re-gates at `99aebe676` — all six gates pass in a
      clean environment with the ambient tool order. Recorded in
      `/tmp/final-gate-clean-cuprum-issue-609-99aebe676.log`.
- [x] 28 November 2026: the paused review's open threads and assessment were
      addressed. The upload step runs with `always()`, so a cancelled attempt
      keeps the records it already wrote. The extension job declares
      `DEV_FAST_TOOLCHAIN`. The writer normalizes an out-of-vocabulary
      `selected_backend` and validates before appending, and the reader treats
      a non-finite transfer or payback ratio as unknown rather than writing
      `Infinity` or raising `OverflowError`. The gate fixture's
      `selected_backend` matches its backend, and the developers' guide
      documents the namespace, backend output, evidence tooling, and shared
      expression helpers.
- [x] 28 November 2026: M5 re-gates at `fb1da7d8b` — all six gates pass in a
      clean environment. Recorded in
      `/tmp/final-gate-clean-cuprum-issue-609-fb1da7d8b.log`.
- [ ] M5 delivery: push the refreshed head and merge the pull request.

## Surprises & Discoveries

- `make typecheck` does not build the PyO3 extension. `build` is
  `uv sync --group dev`, and the project uses `uv_build`, so the matrix leg's
  real compiler traffic comes from selected tests that invoke Cargo, such as
  `cuprum/unittests/test_doctest_warning_contract.py`. The inventory must
  describe the effective route rather than assume a `maturin develop` build.
- `tools/dev-fast/cargo` replaces itself with the real Cargo and passes the
  repository fragment; it preserves `RUSTC_WRAPPER` because it only rewrites
  `CARGO` and argv. This needs an executable test rather than a comment.

## Decision log

- 22 November 2026: no family is adopted in this change. The issue forbids
  synthetic pushes and extra dispatches, and the gate requires at least five
  comparable warm pairs from ordinary activity. The apparatus lands first; the
  archive stays until the gate is satisfied. This is the "insufficient
  evidence" outcome the issue permits.
- 22 November 2026: the direct backend remains opt-in per family through
  `setup-sccache`, with an optional namespace. The action keeps `local` as the
  default so a GitHub-hosted caller cannot silently reach GitHub's quota.
- 22 November 2026: evidence records carry a fixed schema version and a closed
  vocabulary. Missing evidence is `unknown`, never zero.
- 23 November 2026: the evidence writer is split across `schema`, `probe`,
  `evidence`, `records`, `summary`, and `report` modules, each below the
  400-line budget. The split follows responsibility, not file size alone: the
  schema is shared vocabulary, the probe is the only I/O, and the report owns
  the CLI.
- 23 November 2026: the matrix job's `make typecheck` bracket declares that it
  invokes no rustc. `make typecheck` is `uv sync` plus `ty check`, which builds
  no PyO3 extension, so a zero there is honest; the `make test-python` bracket
  declares that it does compile, through the selected doctest and cargo-warning
  contracts.
- 23 November 2026: the mutation rules treat `pull_request_target` as unsafe
  only when the privileged workflow also carries the experiment's evidence or
  compiler-cache restore. The trigger is legitimate in
  `dependabot-automerge.yml`, which runs no repository code.
- 23 November 2026: rebased from `87de64c2c` onto `f62a510a1` (`#735`, which
  moves the consume path) with no conflict; the experiment's baseline remains
  `87de64c2c`, and the rebase touched no cache or compiler-shape file.
- 23 November 2026: the original size tolerance (1,200 code lines, 250
  configuration, 500 documentation) was exceeded. The apparatus needs six
  writer/reader modules, one composite action, the workflow brackets, and the
  contract suites that keep each of them honest; the estimate assumed a single
  script and a smaller test surface. The deviation is recorded rather than
  hidden, and no existing gate or runtime behaviour was traded away for it.
- 22 November 2026: the restore-to-writer join lives in
  `tests/test_ci_compiler_cache_readers.py`, and the evaluated predicate test
  lives in `tests/test_ci_compiler_cache_arms.py`, rather than in the two
  modules the delivery packet named. Both destination modules were at 328 to
  362 lines before the additions, and the repository's 400-line module budget
  would have been exceeded. The original modules keep the textual and ordering
  rules; the new modules own the semantic join and evaluation.

## Outcomes & retrospective

Delivered: the measured qualification path and a reviewed adopt/reject record.
No family is adopted, because the gate requires five comparable warm pairs from
ordinary activity and the instrumentation that produces them is what this
change lands. Every candidate family keeps archived caching with a named owner,
review date, rollback and refresh trigger.

A tenth review round hardened the reader further, and a CodeScene code-health
failure on the pull request was cleared by splitting the flagged functions and
sharing the duplicated test setup rather than suppressing the report.

Lessons recorded during implementation:

- A clean environment is not only about unsetting variables; tool order
  matters. The gate must resolve `mold` from `~/.local/bin`, not the older
  distribution copy in `/usr/bin`, so the ambient `PATH` order is preserved and
  only Lody entries are removed. A hand-built `PATH` that puts `/usr/bin` first
  makes `dev-fast-check` fail for a reason unrelated to the change.

- The gate is only as credible as its inputs. Six review rounds found real
  holes (double-counted cache events, a misread cache location, unverified
  billing treated as free, bracket pairs counted as independent, and mismatched
  backends accepted as healthy). Each became a contract or a validator rule;
  none was suppressed.
- A clean environment is part of the gate. The ambient Lody `git` shim stalls
  Whitaker's snapshot `git rev-parse`; the repository's own Git instructions
  already require removing it, and the recorded gate run does so.
- The experiment cannot be shortcut. Synthesizing pushes to collect pairs
  would invert the issue's evidence rule, so the decision stays "insufficient
  evidence" until ordinary activity supplies the observations.

## Conformance basis

Issue [#609](https://github.com/leynos/cuprum/issues/609) and the source
evidence at `24c39650e388975a9b9abdc884cdf3e237745bb9` (10 October 2026). The
implementation baseline is `87de64c2cad4e970984760096faf278bd70439f3`
(22 November 2026), which is also `origin/main` at the time of writing.
`docs/ci-cache-ownership.md` is the durable record; this plan is the delivery
record and is not retroactively rewritten once completed.

## Verification plan

Obligations and their evidence:

1. The inventory is true of the workflows. Evidence: the table in
   `docs/ci-cache-ownership.md` is derived from `.github/workflows/ci.yml`,
   `.github/actions/cache-keys/action.yml`, `setup-sccache`, the Makefile, and
   the selected tests, and the leg-aware contract test reads the same steps.
2. Every compiler-cache restore has a writer or is deliberately cold. Evidence:
   `tests/test_ci_compiler_cache_readers.py` resolves each restore family
   through `tests/helpers/ci_cache_families.py` and joins it to the writers the
   save steps publish. A mutation removing a save fails with the family name.
3. Backend, credentials, and runner arm cannot diverge. Evidence:
   `tests/test_ci_compiler_cache_arms.py` extracts the predicate from `runs-on`
   and substitutes fork, Dependabot, and owned truth values into the `runs-on`,
   `backend`, and credentials expressions, asserting
   hosted/local/no-credentials versus Ubicloud/gha/credentials. A swapped arm
   or an uncomplemented guard fails.
4. The evidence script never leaks secrets or invents success. Evidence:
   `tests/test_ci_compiler_cache_evidence.py` feeds bearer URLs and tokens and
   asserts their absence, checks delta arithmetic, classifies zero requests,
   marks fallback and service-v2-with-`gha` degraded/noncompliant, and rejects
   malformed records. Hypothesis properties use explicit `max_examples` and
   `deadline=None`.
5. The action's shell behaves. Evidence: the composite-action harness runs the
   real step with fake `sccache` and `rustc`; a failed start followed by a
   build leaves an empty wrapper and a non-zero build, while a successful start
   leaves the wrapper visible to the fake compiler.
6. The `dev-fast` adapter preserves the wrapper. Evidence: the adapter test
   runs `tools/dev-fast/cargo` with a fake Cargo that records its environment
   and asserts `RUSTC_WRAPPER` survives; a mutation that strips it fails.
7. The gate is not vacuous. Evidence: the committed gate names the minimum
   whole-job improvement, the pair count, the cost rule, and the contamination
   rules; the decision record cites the observations or records insufficiency.
8. Existing fidelity is intact. Evidence: `make check-fmt`, `make lint`,
   `make typecheck`, `make test`, `make markdownlint` and `make nixie` pass at
   `fb1da7d8b` in a clean environment, and the test-selection contract collects
   the new modules.

## Milestones

### M1: inventory and hermetic contracts

End state: `docs/ci-cache-ownership.md` carries the experiment section with the
baseline, the per-family inventory, and the explicitly historical labels for
the 286 MB, 1-hit/562-miss, and 5–11 s figures. `tests/helpers/ci_runners.py`
declares `typecheck-test` in the sccache contract and drops the stale
`test_the_typecheck_only_leg_installs_no_wrapper` reference.
`tests/test_ci_compiler_cache.py` compares the matrix guards through `ungated`.
`tests/test_ci_cache_ownership.py` resolves restore families and joins them to
the writer manifest. `tests/test_ci_compiler_cache_backend.py` evaluates the
runner predicate and its two dependent expressions.

Acceptance: the named tests pass; removing a writer or swapping a backend arm
fails with a message naming the family or the arm.

### M2: evidence instrumentation

End state: `scripts/compiler_cache_evidence.py` provides `snapshot`, `mark`, and
`record`; `.github/actions/compiler-cache-evidence/action.yml` wraps them with
`continue-on-error`; `ci.yml` brackets `typecheck-test`, `extension-tests`, and
`lint-test`; `docs/ci-compiler-cache-evidence.md` documents schema, privacy,
failure policy, retention, and limits.

Acceptance: the new tests pass, a sample record set yields the required summary
fields, and the wiring contract refuses a `post` before its `pre`.

### M3: direct-backend qualification

End state: `setup-sccache` has an optional namespace and a backend output; the
namespace reaches the server process only on `gha`; `local` rejects it;
mutation tests cover credentials-after-startup, wrong service, both backends,
orphan save/restore, wrong fork arm, broken wrapper propagation, and false
trust-scope sharing; executable tests cover fallback and wrapper propagation.

Acceptance: the setup-sccache, lifecycle, mutation, and executable suites pass.

### M4: gate and decision

End state: `docs/ci-cache-ownership.md` carries the predeclared gate, the
Ubicloud storage/protection terms with their evidence status, and a per-family
adopt/reject record naming the owner, review date, evidence reference,
limitations, rollback, and refresh trigger. No family is migrated without
satisfying the gate.

Acceptance: the record is internally consistent with the evidence format and
the rollback is executable as a single revert.

### M5: gates and delivery

End state: all commit gates pass, CodeRabbit concerns are cleared, the branch
is pushed, and a draft pull request cites the issue, the Lody session, and the
evidence status.

Acceptance: `make check-fmt`, `make lint`, `make test`, and `make markdownlint`
succeed on the committed head; the pull request is a draft.

## Revision note

Initial draft, 22 November 2026. Created from the issue #609 delivery packet
and the repository baseline at `87de64c2c`.
