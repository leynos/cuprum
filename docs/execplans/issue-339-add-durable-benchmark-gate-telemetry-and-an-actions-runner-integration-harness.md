# Persist benchmark-gate decisions and verify Actions-runner admission

Status: IMPLEMENTED; hosted receipt verified, rebased candidate green, rebased
again onto `b63a0f21`

This living ExecPlan records the implementation of issue #339. The maintainer's
2026-09-17 instruction supersedes the original Grafana deployment requirement:
keep persistent benchmark data that can be analysed and visualized, without
provisioning Grafana or installing another application. Earlier implementations
and their evidence remain available in Git history.

## Purpose / big picture

A maintainer should be able to download structured benchmark-gate decisions
across CI runs, count decisions by stable categories, and visualize those
counts without operating a telemetry service. The decision log complements the
existing benchmark measurement reports. It records whether the gate permits the
benchmark; other failed quality prerequisites can still prevent benchmark
execution.

The same change supplies a supported local Actions compatibility test. Running
`make test-act` must execute the real path detector, its output propagation,
the recorded decision, and the production benchmark admission condition for
pull requests and pushes. It must distinguish an irrelevant diff from a failed
path detector.

## Constraints

- Do not provision Grafana, manage a telemetry credential, introduce an external
  service, or install a new analysis application. Use existing GitHub Actions
  artefact storage and Python's standard library for analysis.
- The labels are exactly `event_class`, `detector_status`, and `decision`.
  Their closed vocabularies are `{pull_request, other}`,
  `{success, failure, unknown}`, and `{run, skip, skip-detector-failed}`.
  Paths, commands, secrets, run IDs, and timestamps must never become labels.
- The decision step remains the single source of truth. Persistence validates
  and transports its outputs, without recomputing admission from event data.
- Log writes and uploads fail open. Detector failure must still produce a
  decision record unless the workflow was cancelled or persistence failed.
- Preserve the real `changes` job and the benchmark job's `needs` and `if`
  declarations in the harness. Substitute only expensive prerequisite and
  benchmark bodies with probes; use GitHub-hosted runner mappings.
- Run commit gates sequentially, capture logs under `/tmp`, and commit only
  validated changes. Request CodeRabbit after deterministic gates; resolve
  verified findings before the next milestone. Do not kill other agents' jobs.

## Tolerances (exception triggers)

The user has authorized continuation, the logging replacement, commits, push,
and a pull request; its readiness for review was set at the user's direction.
Routine fixes within this scope do not require renewed approval. Stop if the
implementation cannot be salvaged, the disk fills, or completing this design
would require a new service or application. Report a denied external operation
precisely; do not retry it without authorization.

Do not claim hosted receipt from a local test or a successful upload
declaration. If hosted verification cannot run, document that gap explicitly in
this plan and the pull request rather than marking delivery complete.

## Context and orientation

`.github/workflows/ci.yml` owns `changes` and `benchmark-ratchet`. The former
uses pinned `dorny/paths-filter`, exports `bench`, and records bounded gate
outputs. The latter depends on quality jobs and `changes`, and permits healthy
non-PR events or relevant PRs. A failed `changes` job prevents admission.

`tests/helpers/workflow.py` provides validated YAML accessors.
`tests/helpers/act_workflow.py` projects the production workflow into a
temporary Git repository, retaining the detector and admission boundary.
`tests/helpers/act_harness.py` creates real changed-path histories and invokes
`act`; `act_runtime.py` probes the runtime and `act_stream.py` reads structured
logs. `tests/integration/test_workflow_integration.py` owns the scenario
matrix. JSON webhook templates live under `tests/fixtures/events/`.

`.github/workflows/benchmark-gate-harness.yml` runs the harness weekly or
through an opted-in manual dispatch. Its schedule is separate from CI so it
cannot start paid build jobs. CLI and runner-image pins are documented in
ADR-012.

`tests/helpers/benchmark_gate_telemetry.py` executes the workflow log writer.
The two `tests/test_ci_benchmark_gate_telemetry*.py` modules check declarations
and persisted bytes. Operational guidance lives in
`docs/ci-benchmark-gate-telemetry.md`; ADR-011 records the storage decision.

## Conformance basis

The upstream requirement is issue #339, following PR #289, as amended by the
maintainer's explicit 2026-09-17 no-Grafana/no-new-application instruction.
There is no separate terms-of-reference or technical-design document for this
work. The architecture decisions are ADR-011 for persistent logs and ADR-012
for the Actions compatibility harness. Repository `AGENTS.md`, documentation
style, and scripting standards govern implementation and verification.

The original external-sink requirement is superseded, not silently deferred.
The requirement for bounded decisions maps to milestone M2 and its schema
tests; the runtime-boundary requirement maps to M1 and the `act` scenarios;
persistent receipt and publication map to M3 and a downloaded hosted artefact.

## Progress

- [x] 2026-09-17: Established that the existing implementation was salvageable.
  Repaired shell-token and workflow-contract checks in `d254d808` and
  `081150ab`, with failing controls before fixes and all gates afterwards.
- [x] 2026-09-17: Hardened the then-proposed telemetry labels in `8c9f8662`.
  CodeRabbit against `d254d808` completed with zero findings. That transport is
  now superseded by the maintainer's storage-only direction.
- [x] 2026-09-17: Added real downstream admission tests, both event classes,
  immutable image pinning, and a dedicated scheduled workflow in `11ea4cba`.
  All repository gates and eleven `act` checks passed.
- [x] 2026-09-17: Resolved four distinct CodeRabbit typing concerns (six
      reports)
  in `736a86a0`, reusing validated readers and deep-copying event payloads.
  Malformed-workflow controls failed against the old helper and passed after
  repair; all code gates and eleven runtime checks passed afterwards.
- [x] 2026-09-17: Recorded the required failure before replacing the publisher:
  the secret-free persistence contract failed because no log step existed.
- [x] 2026-09-17: Implemented JSON Lines persistence and a 90-day Actions
  artefact upload; 31 focused schema, bounds, and storage-failure tests passed.
- [x] 2026-09-17: Relevant and irrelevant PR scenarios passed with the new
  runtime record assertions. The documented CSV/SVG recipe passed valid,
  duplicate, empty-input, and invalid-label smoke checks.
- [x] 2026-09-17: Validated the full runtime matrix and every applicable gate.
  Eleven harness checks passed with no skips, including ten container runs.
- [x] 2026-09-17: Completed the full-branch CodeRabbit review. Replaced a
  substring contract assertion with the shared shell parser in `2c1c4e57`; four
  negative controls failed before the fix and passed afterwards. Added the
  requested NumPy parameter documentation in `bbe408f0`. All code gates passed
  for both repairs; the final review returned zero findings.
- [x] 2026-09-17: Pushed the requested branch and updated existing draft PR
      #418,
  including `Closes #339`, the issue suffix in its title, and the final session
  reference.
- [x] 2026-09-17: Downloaded a decision artefact from the published candidate
  and verified its schema, labels, run association, and approximately 90-day
  retention using GitHub's artefact metadata.
- [x] 2026-09-17: Recorded terminal hosted CI outcomes: `changes` and
  `benchmark-ratchet` passed. CodeScene coverage parsing and a rustup TLS
  connection failed independently of the implemented boundary. Final-head
  results are tracked in draft PR #418.
- [x] 2026-09-18: Re-rebased onto `83cd8df2` after review feedback. All 21
  commits replayed as identical patches; the semantic audit confirmed every
  branch-only path byte-identical and every target-only path inherited
  unmodified.
- [x] 2026-09-18: Repaired the two CodeScene argument-count diagnostics in the
  summary-support and telemetry-execution tests, then re-ran the repository
  gates for the rebased candidate.
- [x] 2026-09-18: Confirmed the rebased candidate's hosted run `35337586749`
  passed all 17 jobs, re-validated its downloaded decision and measurement
  artefacts, corrected PR #418's stale "unresolved failure" claim, and marked
  it ready for review at the user's direction.
- [x] 2026-09-18: Rebasing onto the advanced `origin/main` head `b63a0f21`,
  which brought five new commits (the idle heartbeat in `#359`, Dependabot
  alignment `#304`, mutation-testing workflow-contract docs `#209`, Skylos
  dead-code detection `#307`, and a §13.2 design-doc alignment `#248`). Exactly
  one of 24 replays conflicted: `.PHONY` in the `Makefile`, where main's
  `makeutil`/`skylos-allow` reflow met the branch's `test-act`. Resolved as a
  union, preserving both sides' targets; the other 23 replays applied cleanly.
- [x] 2026-09-18: Force-pushed the rebased series with lease, re-flowed the
  plan prose that `make check-fmt` rejected, re-ran all four gates green at
  `2a671157`, updated PR #418's body for the new base and head, and validated
  the resulting hosted run `35394569533`, whose decision and measurement
  artefacts were downloaded and checked. Committing that record produced
  `b61c5d3d`, whose own hosted run `35396687688` passed every job as well.

## Surprises & discoveries

The original harness only ran `changes`; an assertion requiring the downstream
admission marker failed even for a relevant PR. The corrected harness executes
the dependency graph while preserving the actual admission expression.

The original weekly schedule belonged to the main CI workflow, so it would
start paid jobs as well as the harness. A dedicated single-job workflow removes
that unrelated scheduled work. The original runner tag was mutable; a verified
multi-platform image digest now fixes the image content.

An empty GitHub token makes the pinned detector use its local Git fallback.
This tests real path filtering and Actions output propagation, but not the
hosted PR REST API or permission model. The harness is deliberately a
compatibility test rather than proof of identical hosted behaviour.

Repository secret and variable listing returned HTTP 403 during the abandoned
Grafana design. No resource was provisioned and no receipt was verified. Those
operations are unnecessary under the maintainer's revised requirement and must
not be retried as part of the storage-only implementation.

## Decision log

On 2026-09-17 the maintainer explicitly rejected Grafana provisioning and new
applications. M2 therefore replaces the external publisher with structured
records in existing Actions artefacts. The offered retention choices received
no answer before implementation continued; the announced default is a requested
90-day artefact window, subject to repository limits and deletion. Downloaded
copies can be retained longer without changing CI infrastructure.

Each `decisions.jsonl` file contains one versioned record with metric name
`benchmark_gate_decisions_total`, value `1`, exactly three bounded labels, and
separate run ID, attempt, and UTC time metadata. Summing record values counts
observations; there is no claim that isolated value-one samples constitute a
shared cumulative counter. Deduplicate downloaded records by run ID and attempt.

Use the runner's preinstalled Python standard library to serialize the log.
This small inline workflow operation needs no package installer or project-code
execution. The existing pinned `actions/upload-artifact` action archives it.
Both persistence and upload are fail-open and produce visible diagnostics.

The local harness executes the writer and checks its structured record output.
It skips the hosted upload under `ACT=true`, avoiding another local artefact
service. Actual storage receipt is checked separately on GitHub. This
deliberate boundary is recorded in ADR-012 and the operational guide.

## Risks

GitHub artefacts are retained storage, not an indefinite archive. The requested
90 days can be shortened by repository policy, deletion, or expiration. Missing
records must not be interpreted as zero decisions. Maintainers needing a longer
history must download records before expiration.

The gate can admit a benchmark whose other prerequisites later fail. Analysis
must distinguish gate decisions from actual benchmark executions and join the
existing measurement reports where execution results matter.

`act` and its container image differ from GitHub-hosted runners. Pin versions,
run the weekly compatibility workflow after merge, and retain a separate hosted
receipt check. Action and image downloads can still need network access even
though the detector uses local Git history.

The pre-rebase candidate passed `changes` and `benchmark-ratchet`, but the
broader run was not wholly green: CodeScene rejected coverage XML with
`No matching field found: close for class java.io.InputStreamReader`, and the
Linux x86 wheel installer failed with `curl(35)` and `SSL_ERROR_SYSCALL` while
connecting to `sh.rustup.rs:443`. Neither check was weakened. Both failures are
now accounted for rather than merely re-observed: main's PR #411 (`2070a41b`)
retired the pull-request CodeScene check and moved that upload to
`coverage-main.yml`, so the rebased head no longer runs the failing step, and
its hosted run `35337586749` passed all 17 jobs. The residual risk is external
service behaviour, not a suppressed check: a default-branch CodeScene change or
a rustup/registry outage can still fail unrelated jobs without touching this
boundary.

The second rebase onto `b63a0f21` inherited main's new `makeutil` prerequisite
on `make test` and `make test-python`. That is an added external binary
requirement for local and CI runs of those targets, and it is now installed in
both `ci.yml` jobs that need it. The harness workflow's `make test-act` does
not depend on `makeutil`, so the opt-in scenario lane is unaffected.
Installation of `makeutil` remains a failure mode for `make test` that the
branch neither introduces nor can remove.

## Verification plan

V1: Labels remain exactly the three approved names and finite vocabularies. The
execution tests run the real writer for all 18 allowed combinations and reject
empty, path-shaped, and quote/newline inputs for every label. They inspect the
actual JSON file, its schema, and its step-output copy. These checks are not
vacuous: ordinary valid records must be written, while injected values produce
no record and are not echoed into diagnostics.

V2: Persistence cannot change admission. Contract tests pin `!cancelled()`,
`continue-on-error`, the upload failure warning, and reuse of canonical
outputs. A deliberately blocked output directory exercises an actual write
failure and must produce a warning with exit status zero. Hosted uploader
behaviour is a third-party interface assumption; the final downloaded artefact
verifies the configured successful storage boundary, not every service outage
mode.

V3: Detector output reaches the actual admission condition. Eight healthy
scenarios cover relevant, irrelevant, mixed, and empty changed-path sets for
PRs and pushes; two more force the pinned detector to fail with an invalid
input. The tests check `bench`, bounded outputs, summary cells, the JSON
record, and the downstream marker. The old changes-only harness failed the
marker control. Malformed shape controls independently reject invalid workflow
dependencies and steps. The prerequisite probes intentionally assume successful
quality jobs; this isolates the benchmark-gate decision from unrelated build
failures.

V4: The supported harness stays isolated from paid CI scheduling. Static tests
pin its sole job, separate weekly trigger, immutable runner digest, checksum-
verified `act` installation, and refusal to silently skip when CI lacks a
runtime.

No Rust production logic or new formal lemma is introduced. Exhaustive finite
label combinations, negative controls, real container execution, and hosted
receipt provide proportionate evidence. Third-party runtime internals are not
claimed to be formally verified.

## Milestones and concrete steps

M0 is the completed salvage and parser repair. M1 is the completed runtime
harness and its review fixes. Reverting their atomic commits restores their
previous states without rewriting shared history.

M2 replaces the transport and its obsolete tests together, updates ADR-011,
retention and analysis guidance, and exercises the record through the harness.
Run the following gates sequentially from the repository root, capturing each
command through `tee` with `set -o pipefail`:

```bash
make fmt
make check-fmt
make lint
make typecheck
make test
make markdownlint
make nixie
make test-act
```

All must exit zero. The integration target currently runs eleven checks and
must report no runtime skips. Only after deterministic success request
`coderabbit review --agent` against the full branch. Verify each finding
against the live candidate, fix valid concerns, and repeat applicable gates. If
the review service rate-limits, use the user-requested foreground `vsleep`
interval of a random 45–90 minutes before retrying.

M3 publishes the already named branch, retaining its matching origin upstream,
and updates the existing PR rather than opening a duplicate. The PR title must
include `(#339)` and its summary must contain `Closes #339`. Its final
`## References` section must link session
`https://lody.ai/leynos/sessions/103f642f-34a2-46a6-b03c-f280276fdbc9`.

Inspect the hosted `changes` result for that exact pushed SHA. Download its
`benchmark-gate-decision-*` artefact using the operational guide and compare
the record identity, schema, and labels with the run. Record this evidence
before claiming persistence is operational. The new dedicated dispatch workflow
may only become available after merge to the default branch; local `act`
execution is separate evidence and must not be described as a hosted harness
run.

## Artefacts and evidence

Logs under `/tmp` are local evidence and may disappear; commit identifiers and
PR records provide the durable review history. Useful logs from this session:

- `/tmp/issue339-review-telemetry.log`: zero findings for the earlier repair.
- `/tmp/issue339-admission-red.log`: old harness lacks downstream admission.
- `/tmp/issue339-harness-test-act.log`: eleven scenarios passed.
- `/tmp/issue339-review-harness.log`: six reports covering four typing issues.
- `/tmp/issue339-harness-shapes-red.log` and
  `/tmp/issue339-harness-shapes-green.log`: malformed-input controls.
- `/tmp/issue339-harness-types-*.log`: repaired harness gates and eleven runtime
  checks passed.
- `/tmp/issue339-log-red.log` and `/tmp/issue339-log-green.log`: missing
  persistence control and 31 passing log tests.
- `/tmp/issue339-log-*.log`: passing full repository, documentation, and runtime
  gates for the persistent-log milestone.
- `/tmp/issue339-command-red.log`: four false-positive command controls failed
  before the contract repair; `/tmp/issue339-command-focused.log`: 27 passed
  afterwards.
- `/tmp/issue339-command-*.log` and `/tmp/issue339-docstring-*.log`: passing
  code gates for the final review repairs.
- `/tmp/issue339-review-command-final.log`: zero-finding review after repair.
- [Hosted CI run 35248836322](https://github.com/leynos/cuprum/actions/runs/35248836322)
  ran published commit `bbe408f011d4a4c08d7c0e9f4ff3bf83c2b817a8`.
  [Decision artefact 10508771038](https://github.com/leynos/cuprum/actions/runs/35248836322/artifacts/10508771038)
  contains exactly one schema-version-1 record with labels
  `event_class=pull_request`, `detector_status=success`, and `decision=run`.
  Its run ID is `35248836322` and attempt is `1`. The downloaded bytes and
  separately retrieved API metadata are in
  `/tmp/issue339-hosted-receipt.ATRFG9/`. GitHub reports expiry at
  `2026-12-16T16:48:18Z`, approximately 90 days after creation.
- The same run's successful benchmark job uploaded
  [measurement artefact 10509590666](https://github.com/leynos/cuprum/actions/runs/35248836322/artifacts/10509590666)
  with the same expiry. Its fifteen downloaded JSON files parsed successfully,
  and its Markdown comparison summary was present.
- `/tmp/issue339-hosted-final.json` records the terminal run result;
  `/tmp/issue339-hosted-coverage-job.log` and
  `/tmp/issue339-hosted-wheel-job.log` contain the two external failures above.

## Outcomes & retrospective

The existing implementation was salvaged. The runtime harness and persistent
logging are implemented, validated, reviewed, and published in PR #418, which
is open for review rather than a draft. GitHub accepted the new decision log,
and its downloaded contents and retention metadata satisfy the revised
persistence contract. No external service or telemetry credential was
introduced. The documented standard-library analysis recipe produces CSV and
SVG without another application.

The local harness proves compatibility for the specified event and changed-path
matrix; the hosted receipt proves the configured upload boundary. The hosted
benchmark also ran successfully and retained its measurements. This does not
establish exact runtime parity or success for unrelated hosted CI jobs; the
pre-rebase CodeScene parser and installer TLS failures are recorded above, and
the head reached by the first rebase, `67539822`, passed all 17 jobs in run
`35337586749`. That receipt predates the second rebase onto `b63a0f21`, so the
head reached by that rebase, `3051257e`, needed its own hosted run. It has one:
[run 35394569533](https://github.com/leynos/cuprum/actions/runs/35394569533)
completed **successfully** for `2a671157`, the plan-commit head of the rebased
series, with every job passing. Its
[decision artefact](https://github.com/leynos/cuprum/actions/runs/35394569533/artifacts/10566788571)
was downloaded and validated against the schema, the three bounded labels
(`event_class=pull_request`, `detector_status=success`, `decision=run`),
`value=1`, and a `run_id` matching the producing run, and its
[measurement artefact](https://github.com/leynos/cuprum/actions/runs/35394569533/artifacts/10567253937)
holds eleven valid JSON files plus the comparison summary, whose ratchet
decision is `passed` with no regressions against a five-sample compatible
history. Both request 90-day retention. The `changes` job's
`Persist the benchmark gate decision` and `Upload the benchmark gate log` steps
both succeeded, and the warn step was correctly skipped.

Every hosted receipt in this plan is bound to the commit its run names, not to
whatever head the branch carries when the plan is read: recording a receipt
necessarily adds commits, and the head moves on. Later runs repeat the same
checks for their own heads; run `35396687688`, for example, passed all jobs for
`b61c5d3d` and retained both artefacts, and every push to this branch starts a
fresh run in the same way. Read the head from the run, never from the plan.

## Revision note

2026-09-17: Replaced the obsolete, externally provisioned sink plan with the
maintainer-authorized storage-only design. Preserved the implementation and
review history in concise form, updated all acceptance evidence and remaining
work, and separated local runtime proof from hosted artefact receipt.

2026-09-17: Added successful full-gate and final-review evidence, verified
hosted decision and measurement downloads, and recorded terminal external CI
failures without weakening their checks.

2026-09-18: Rebased the 21-commit series from `a97d0a8b` (its exclusive base)
onto `83cd8df2`, the current `origin/main` head. Main had advanced by two
commits: the pull-request coverage change in `2070a41b` and a pyright bump in
`83cd8df2`. Every replay applied as an identical patch, so no conflict
resolution was needed and no branch-only path changed. Main's `ci.yml` coverage
rewrite, its `job_env` helper, and its pyright `uv.lock` revision are inherited
unchanged; the branch owns no `uv.lock` delta, so no lockfile rebuild was
required. Verified with `git range-diff`, a byte-identity check over all 39
branch-only paths, and the target-only-path check from the semantic audit.

2026-09-18: Repaired two CodeScene "Excess Number of Function Arguments"
diagnostics by replacing parameter bundles with values that already represent
the data. `_execute_summary_script` now takes one `_SummaryScriptExecution`
dataclass instead of five keyword arguments, and the closed-label matrix test
takes one `Verdict` per parametrized case instead of three stacked
parametrizations. `SummaryCase` was deliberately left unchanged: it models
expected results rather than execution inputs. The subprocess command,
environment, output files, return-code assertion, the 18-case matrix, and its
deterministic ordering are all unchanged.

2026-09-18: Corrected current-state claims that the rebase had invalidated. The
published pull-request description still reported the pre-rebase CI failures as
unresolved; it now records run `35337586749` succeeding for `67539822` with all
17 jobs passing, explains that main's PR #411 retired the pull-request
CodeScene check into `coverage-main.yml`, and reports the rebased head's
re-validated decision and measurement artefacts (requesting 90-day retention).
The plan's Risks, Outcomes, and M3 sections were updated to match: the PR is
open for review rather than a draft, and the earlier failures are retained as
history rather than as an open blocker. Historical entries that describe what
was true on 2026-09-17 are deliberately left unchanged.

2026-09-18: Rebased the 24-commit series from `83cd8df2` (its exclusive base)
onto `b63a0f21`, the advanced `origin/main` head. Main had gained five commits,
including Skylos dead-code detection (`6a9b2de4`) and the idle heartbeat for
quiet children (`b63a0f21`). A `git merge-tree` preview taken before any
mutation showed exactly one conflicted file: the branch's `test-act` addition to
`.PHONY` and main's `makeutil`/`skylos-allow` reflow collided in the same
target list, while the branch's other `Makefile` hunks and all four other
overlapping paths (`ci.yml`, `contents.md`, `developers-guide.md`, and
`test_workflow_lint.py`) merged without conflict. The resolution keeps the
union of both sides' targets, so main's `makeutil` and `skylos-allow` and the
branch's `test-act` are all present; `makeutil parse Makefile` reports
`status: complete` with no diagnostics, and the contract assertions in
`test_ci_test_coverage_overlap.py` and `test_skylos_lint_contract.py` hold. The
branch owns no `uv.lock` or `pyproject.toml` delta, so both files match the
target byte-for-byte and `uv lock --check` passes without a rebuild. The
semantic audit after the replay found no unexplained deletions and no new
repeated blocks: the only repeated-line count changes in `ci.yml` are the
branch's own persistence steps matching counts already present at the
pre-rebase head.

2026-09-18: Published the second rebase and closed its evidence loop. Two
doc-only commits followed the replay: an mdtablefix re-flow of this plan, whose
token stream is identical before and after, and a two-line correction that
stops calling the post-rebase head "current" once later commits landed on top.
All four gates then passed at `2a671157`. The force push used
`--force-with-lease` bound to the recorded remote head `1de3e659`, so the
update could not silently overwrite a concurrent push. Hosted run `35394569533`
followed and passed every job, which is the first receipt covering the rebased
series; its decision artefact carries the expected three labels and its
measurement artefact shows a ratchet pass with no regressions. The PR body was
updated in the same pass: the previous text still cited the rebase onto
`83cd8df2`, and its two branch-relative anchors had drifted (`ci.yml` `#L1089`
to `#L1113`, and the harness-boundary negative control from `#L88` to `#L98`).
