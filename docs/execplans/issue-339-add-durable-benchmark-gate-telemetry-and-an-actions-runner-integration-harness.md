# Add durable benchmark-gate telemetry and an Actions-runner integration harness

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances (exception triggers)`, `Risks`, `Progress`,
`Surprises & discoveries`, `Decision log`, `Outcomes & retrospective`,
`Conformance basis`, and `Verification plan` must be kept up to date as work
proceeds.

Status: IN PROGRESS

## Purpose / big picture

After this change, a maintainer can answer "how often is the paid
`benchmark-ratchet` job being skipped, and why?" from a durable external time
series rather than by reading individual CI runs. The `changes` job in
`.github/workflows/ci.yml` already decides `run` / `skip` /
`skip-detector-failed` on every CI run and records that decision in the GitHub
Actions step summary, but the summary lives only as long as the run does.
Nothing aggregates it across runs, so a trend — for example, an increase in
`skip-detector-failed`, which means the gate is silently not measuring
performance — is invisible.

This plan adds two things. First, a telemetry sink: the `changes` job pushes a
bounded `benchmark_gate_decisions_total` counter to Grafana Cloud's OTLP
gateway, secret-gated and fail-open, carrying exactly three labels
(`event_class`, `detector_status`, `decision`) whose values come from closed
sets. Second, an Actions-runner integration harness: a pytest-driven `act`
harness that executes the real `changes` job and asserts what
`dorny/paths-filter` decided and what the gate did with that decision, for
relevant, irrelevant, mixed, and empty changed-path sets, for pull-request and
non-pull-request events, and for a failing detector.

You can see it working by running `make test` and observing the new contract
and behavioural tests pass, and by running the integration harness locally with
`act` against rootless podman and observing the real `changes` job print its
gate table.

## Constraints

- Every metric label value must come from a closed, finite set. Changed file
  paths, command text, commit SHAs, run IDs, and timestamps must never appear
  in a label position. This is an issue acceptance criterion, not a preference:
  unbounded labels explode the time-series budget and leak repository content
  into an external service.
- The telemetry push must be **fail-open**. A CI run must never fail, or block
  `benchmark-ratchet`, because the external sink is down, misconfigured, or has
  no credentials. CI's job is to measure the product, not to depend on the
  observability platform.
- The telemetry push must be **secret-gated**, reusing the existing
  optional-integration pattern at `.github/workflows/ci.yml` lines 1007-1018
  (`if: env.CS_ACCESS_TOKEN != ''`), so that forks and repositories without the
  secret are unaffected.
- The metric must be emitted on the same code path that already computes the
  decision. Recomputing the decision in a second step would allow the summary
  and the metric to disagree.
- `make check-fmt`, `make lint`, `make typecheck`, `make test`,
  `make markdownlint`, and `make nixie` must all pass.
- No new third-party runtime dependency may be added to `pyproject.toml`. The
  telemetry push must use `curl`, which is present on `ubuntu-latest`.
- The integration harness must auto-skip when Docker/podman or `act` is
  unavailable, so that `make test` remains runnable on machines without a
  container runtime. It must never run on a paid runner.
- Existing contract tests must keep passing unchanged. In particular
  `cuprum/unittests/test_benchmark_gate_ci_contract.py:test_the_changes_job_has_only_the_permissions_its_filter_needs`
  pins the `changes` job permissions to exactly
  `{"contents": "read", "pull-requests": "read"}`. Adding `id-token: write`
  would break it, so the sink must authenticate with a static secret rather
  than OIDC workload identity.

If satisfying the objective requires violating a constraint, do not proceed.
Document the conflict in `Decision log` and escalate.

## Tolerances (exception triggers)

- Scope: if implementation requires changes to more than 25 files or 1500 lines
  (net), stop and escalate.
- Interface: if a public Python API signature in `cuprum/` must change, stop and
  escalate.
- Dependencies: if a new external dependency (Python, Rust, or a new GitHub
  Action) is required, stop and escalate.
- Ambiguity: multiple reasonable readings of the label vocabulary exist, so
  Task 1 pins them against the shipped `ci.yml` and the issue text; if a
  contradiction is found between the issue's
  `{run, skip, skip-detector-failed}` and the shipped script, stop and escalate
  rather than picking one.
- Iterations: if a gate still fails after three fix-and-rerun attempts, stop and
  escalate with the log path.
- Time: no wall-clock limit is imposed by the task, so there is no time
  tolerance. Gate runs are delegated to `scrutineer`.

## Risks

- Risk: `act` cannot reach the real metric push, because the sink needs a
  secret that CI has and a local run does not, so the harness proves nothing
  about the telemetry path. Severity: medium. Likelihood: low. Mitigation: the
  harness asserts *launch* behaviour, not delivery. The emission step's command
  line is captured with a stubbed `curl` (or asserted from the step summary),
  so the harness verifies that the push is gated, fail-open, and correctly
  labelled, while delivery to Grafana is out of scope and documented as such.

- Risk: `act` mutates the host worktree through its bind mount, corrupting the
  developer's checkout. Severity: high. Likelihood: medium. Mitigation: the
  harness copies the workflow and fixtures into a temporary git repository,
  exactly as validated in the feasibility probe, rather than pointing `act` at
  the real worktree. The worktree is never bound.

- Risk: the OTLP/JSON payload is rejected by the gateway for a wire-format
  reason (enum encoding, int64 encoding, temporality) that only shows up in
  production. Severity: medium. Likelihood: low. Mitigation: the wire format is
  pinned to the OpenTelemetry specification rather than to examples found
  online — integer enums, string int64s, lowerCamelCase keys — and every
  load-bearing claim is recorded in `Decision log` with its source. The
  operational doc states the read-back verification the maintainer performs
  once, and the push is fail-open, so a rejection degrades to "no data" rather
  than "broken CI".

- Risk: label cardinality is higher than intended because an OTLP resource
  attribute is projected into Prometheus labels, and the series count exceeds
  the free-tier budget. Severity: medium. Likelihood: medium. Mitigation:
  resource attributes are deliberately minimal, and the read-back step in the
  operational doc explicitly checks the resulting label set. The contract test
  additionally asserts the three intended labels and nothing else in label
  position.

- Risk: a new job added to `ci.yml` fails
  `tests/test_ci_runner_placement.py`, which requires every job to be
  classified in `tests/helpers/ci_runners.py`. Severity: low. Likelihood: high
  (it is a certainty, not a risk, if a job is added). Mitigation: the harness
  is exposed as a *target*, not a new scheduled job, so no new `ci.yml` job is
  required; if one is added it must be classified in the manifest in the same
  commit.

## Progress

- [x] (2026-09-16 19:00Z) Reconnaissance: read `ci.yml` `changes` job, the gate
  contract and behaviour tests, `workflow.py` helpers, `docs/contents.md`,
  `docs/developers-guide.md`, and the act design doc.
- [x] (2026-09-16 19:12Z) Feasibility experiment: `act` + rootless podman runs
  the *real* `changes` job offline. Verdict: not falsified.
- [x] (2026-09-16 19:20Z) Probe the fixture matrix for relevant, irrelevant,
  push, and detector-failure inputs against the real job; all four produce the
  expected decision row.
- [x] (2026-09-16 19:25Z) Pin the OTLP/JSON wire format from the OpenTelemetry
  specification and the Grafana Cloud OTLP gateway contract.
- [x] (2026-09-16 19:30Z) Rename branch to
  `issue-339-add-durable-benchmark-gate-telemetry-and-an-actions-runner-integration-harness`.
- [x] (2026-09-16 20:05Z) Write ADR-011 (telemetry sink) and ADR-012
  (integration harness); link from `docs/developers-guide.md` and
  `docs/contents.md`.
- [x] (2026-09-16 21:40Z) Red then Green: contract tests for the telemetry
  step, then the emission step in the `changes` job. The step publishes the
  gate step's three `$GITHUB_OUTPUT` values rather than recomputing them, so
  the published series and the step summary cannot disagree.
- [x] (2026-09-16 22:30Z) Fix the shared shlex tokenizer in
  `tests/helpers/workflow_shell.py`; the new step's ordinary
  `payload="$(mktemp)"` raised `ValueError: No closing quotation` under the old
  single-mode tokenizer. See Surprises & discoveries.
- [x] (2026-09-16 23:15Z) Split the telemetry contract suite in two at the
  declaration/execution seam, and parametrize the verbatim-publish test on a
  `Verdict` rather than three positional strings. See Decision log.
- [ ] Red: add the act integration harness tests; observe them skip without a
  runtime and fail without the fixture/report plumbing.
- [ ] Green: implement `tests/helpers/act_harness.py` and fixtures.
- [ ] Write `docs/ci-benchmark-gate-telemetry.md`; update `docs/contents.md`
  and `docs/developers-guide.md`. It must document `count_over_time` as the
  query surface, not `rate()`/`increase()`; see Surprises & discoveries.
- [ ] Wire new test paths into `PYTEST_TARGETS` (Makefile) and any CI gate job.
- [ ] Full gate run via `scrutineer`; `coderabbit review --agent`; draft PR.
- [ ] Red: add the act integration harness tests; observe them skip without a
  runtime and fail without the fixture/report plumbing.
- [ ] Green: implement `tests/helpers/act_harness.py` and fixtures.
- [ ] Write `docs/ci-benchmark-gate-telemetry.md`; update `docs/contents.md`
  and `docs/developers-guide.md`.
- [ ] Wire new test paths into `PYTEST_TARGETS` (Makefile) and any CI gate job.
- [ ] Full gate run via `scrutineer`; `coderabbit review --agent`; draft PR.

## Surprises & discoveries

- Observation: `actions/checkout` under `act` checks out the *scratch*
  repository, not the host worktree, and the real `changes` job completes
  offline against rootless podman. Evidence: the probe run
  `/tmp/act-changes-339/run.out` shows `stepResult=success` for "Check out
  repository" and prints `| pull_request | success | true | run |`. Exit code
  0. Impact: the harness can exercise the real job rather than a fixture that
  imitates it, which is what the issue asks for. No network, no token, and no
  worktree mutation are required.

- Observation: the pinned `dorny/paths-filter` takes the GitHub API branch
  whenever `github.token` is non-empty, and only falls back to local `git diff`
  when it is empty. Evidence: `getChangedFilesFromApi` is entered under
  `if (token)` in the cached action source, and `-s GITHUB_TOKEN=` makes
  `${{ github.token }}` expand to the empty string. Impact: an offline harness
  must pass `-s GITHUB_TOKEN=` explicitly. Without it the action calls the
  GitHub API with a token `act` fabricates, and fails with
  `::error::Not Found` — a failure that looks like a broken detector.

- Observation: `act --json` emits cumulative job state, so the *last*
  `set-output` for a given output name is the operative value, and step-summary
  content is recoverable from the `⚙ Summary -` log message. Evidence: earlier
  lines of the stream carry stale `bench=false` from an intermediate state even
  when the run's final value is `true`. Impact: the harness must scan the whole
  stream and take the last value, not the first match. This is a real
  correctness trap and is asserted by the harness's own parsing tests.

- Observation: the detector-failure scenario exits non-zero, yet the summary is
  still recorded. Evidence: the `broken` probe run exits 1 with
  `STEP FAILED: Detect performance-relevant changes` and still prints
  `| pull_request | failure | unknown | skip-detector-failed |`. Impact: the
  harness must assert the recorded decision on the failing path as well as the
  exit code, rather than treating non-zero exit as "no evidence". This is
  exactly the case the step's `!cancelled()` condition exists for.

- Observation: a cumulative counter whose value is always `1` cannot be read
  with `rate()` or `increase()`. Evidence: both compute `last - first` over the
  window, and this series' value is `1` at every sample, so the difference is
  always zero; the *count* lives in the sample timestamps, not in the value.
  Impact: the documented query surface must be `count_over_time`. This is a
  property of the chosen encoding, not a defect in it — one run contributes one
  sample, and the samples are what accumulate — but a maintainer who reaches for
  `rate()` will read a flat zero and conclude the telemetry is broken. It must
  be stated in `docs/ci-benchmark-gate-telemetry.md`.

- Observation: the shared shlex tokenizer could not lex the new step's script.
  Evidence: `payload="$(mktemp)"` — an ordinary assignment, not a corner case —
  raised `ValueError: No closing quotation` from
  `tests/helpers/workflow_shell.py` under the mode that preserved quotes, which
  broke two `test_extension_ci_contract.py` tests. Probing five shlex mode
  combinations showed no single configuration both survives a double-quoted
  command substitution and preserves quote delimiters:
  `posix=True, preserve_quotes=True` does not preserve quotes on Python 3.13
  (documented behaviour only from 3.14), and `posix=False` raises on `$(...)`.
  Impact: the helper now uses two passes — `posix=True, punctuation_chars=True`
  for the base tokenize, `posix=False, punctuation_chars=False` for the quoted
  comparison — gated on an equal token count, so a differing split is reported
  as "cannot be compared" rather than silently misaligned. The trade-off is
  that quoted-heredoc detection (`cat <<'EOF'`) is now best-effort where the
  base pass is authoritative. Fixing the shared helper was preferred to
  contorting the workflow script to avoid `x="$(...)"`, which would have left
  the trap in place for the next workflow change.

- Observation: `.github/actionlint.yaml`'s `config-variables` list is
  additionally pinned by `cuprum/unittests/test_workflow_lint.py`, which
  asserts the parsed file equals a literal two-rule dict. Evidence: `make test`
  failed with
  `{'config-variables': […]} != {'config-variables': ['CODESCENE_CLI_SHA256']}`
  at that file's line 341. Impact: registering a new repository variable is a
  two-file change, and the pin is intentional — it is what makes a typo in a
  `vars.*` reference an actionlint error rather than an empty string at run
  time — so the fix was to extend the expectation, in the same order as the
  YAML, rather than to loosen the assertion.

- Observation: `docs/developers-guide.md`'s "Accepted architectural decisions"
  list is stale — it omits ADR-001, ADR-008, and ADR-010, which
  `docs/contents.md` does list. Evidence: the list at lines 9-15 ends at
  ADR-009, while `docs/contents.md` carries ADR-010. Impact: this plan appends
  ADR-011/ADR-012 without renumbering or back-filling, and records the drift as
  an observation rather than silently expanding scope to fix it.

## Decision log

- Decision: push the metric to **Grafana Cloud's OTLP gateway** with `curl`
  and a static API token, rather than to a Prometheus remote-write endpoint or
  a bespoke sink. Rationale: the issue names Grafana Cloud as the chosen sink.
  OTLP/JSON is a documented, versioned wire format, so the payload can be
  constructed with `printf` in shell and pushed with `curl`, introducing no new
  binary or language dependency. Prometheus remote-write would require a
  protobuf or snappy encoder, which is a new dependency. Date/Author:
  2026-09-16, planning agent.

- Decision: name the metric `benchmark_gate_decisions_total`, with a `_total`
  suffix. Rationale: it is a monotonic counter. The OpenTelemetry Prometheus
  compatibility specification states that when a monotonic sum's name already
  ends in `_total`, "the name MUST remain unchanged", so the queryable
  Prometheus name is exactly the emitted name and does not acquire a doubled
  `_total_total` suffix. Verified against
  `https://opentelemetry.io/docs/specs/otel/compatibility/prometheus_and_openmetrics/`.
  Date/Author: 2026-09-16, planning agent.

- Decision: emit an OTLP Sum with `isMonotonic: true` and
  `aggregationTemporality: 2` (CUMULATIVE) carrying the value `1` once per run,
  rather than a delta of 1. Rationale: 2 is the specification's integer enum
  value for `AGGREGATION_TEMPORALITY_CUMULATIVE`; the OTLP/JSON specification
  requires that "Values of enum fields MUST be encoded as integer values" and
  that "the enum name strings MUST NOT be used". A cumulative sum of 1 per run
  is the correct shape for a process that starts near zero each run and
  contributes exactly one observation. A delta push of 1 would also work where
  the gateway accumulates deltas, but cumulative is the closer description of
  what the run observed: the gate decided once. Date/Author: 2026-09-16,
  planning agent.

- Decision: encode `startTimeUnixNano` and `timeUnixNano` as JSON strings, and
  `asInt` as a string, not as bare numbers. Rationale: the specification
  requires 64-bit integers to be "encoded as decimal strings". A bare JSON
  number risks precision loss in a JavaScript-based parser. Date/Author:
  2026-09-16, planning agent.

- Decision: authenticate with a repository **secret** and gate the step on
  `env.<TOKEN> != ''`, not with OIDC workload identity. Rationale: the
  `changes` job's permissions are pinned to exactly `contents: read` and
  `pull-requests: read` by an existing contract test. OIDC would require
  `id-token: write` and would break that test, and widening the job's
  permissions to serve telemetry is a poor trade. The static secret reuses the
  repository's existing optional-integration pattern. Date/Author: 2026-09-16,
  planning agent.

- Decision: the integration harness runs the real `changes` job via `act` in a
  **temporary git repository**, passing `-s GITHUB_TOKEN=` and `-W`. Rationale:
  `-W` is required because `--job` matches by job name across all workflow
  files and would otherwise run unrelated jobs; `-s GITHUB_TOKEN=` is required
  to push `dorny/paths-filter` onto its local `git diff` path offline; and a
  temporary repository avoids binding the developer's worktree into a container
  that runs `git checkout`. Date/Author: 2026-09-16, planning agent, from the
  feasibility experiment.

- Decision: split the telemetry contract suite into
  `tests/test_ci_benchmark_gate_telemetry.py` (declaration assertions, read from
  `ci.yml`) and `tests/test_ci_benchmark_gate_telemetry_execution.py`
  (assertions on the body the real `run:` block produced). Rationale: the
  single module was 485 lines against pylint's `max-module-lines = 400`. The
  seam is the one the repository already uses —
  `test_benchmark_gate_summary_behaviour` contrasts "the contract tests next
  door" with the tests that execute the script — and it also states the
  adequacy argument: the declaration half is necessary but not sufficient,
  which is why both exist. Splitting on that line was preferred to raising the
  cap or suppressing C0302, since the cap is a real ceiling and no
  `# pylint: disable` exists anywhere in the repository. Date/Author:
  2026-09-16, implementing agent.

- Decision: expose the harness as a pytest suite and a Makefile target, not as
  a new scheduled `ci.yml` job. Rationale: the issue asks for a supported
  harness, and CI jobs are the expensive part; a scheduled job would also
  require classification in `tests/helpers/ci_runners.py` and a
  runner-placement test update. The suite auto-skips without a container
  runtime, so it is safe in any environment. Date/Author: 2026-09-16, planning
  agent.

## Outcomes & retrospective

Not yet complete. To be written at the final milestone, reconciling the
implementation against the ADRs in `Conformance basis`.

## Context and orientation

`cuprum` is a Python library with a Rust extension. Its Continuous Integration
workflow is `.github/workflows/ci.yml`, which declares a `changes` job that
answers one question on every run: did this run change anything that could
affect performance? It does so with `dorny/paths-filter`, pinned by commit SHA,
against eight path patterns (`cuprum/**`, `rust/**`, `benchmarks/**`,
`conftest.py`, `Makefile`, `pyproject.toml`, `uv.lock`,
`.github/workflows/ci.yml`). The job's `bench` output is consumed by the
`benchmark-ratchet` job, which runs on a paid `ubicloud-standard-2` runner and
is gated on `needs.changes.outputs.bench == 'true'` for pull requests.

The `changes` job has a second, later step, "Record the benchmark gate
decision", which maps the event name and the detector's outcome onto three
closed vocabularies and writes a Markdown table to `$GITHUB_STEP_SUMMARY` plus a
`::notice` annotation. Those three vocabularies are:

- `event_class`: `pull_request` when the event is a pull request, else `other`.
- `detector_status`: `success`, `failure`, or `unknown`.
- `decision`: `run`, `skip`, or `skip-detector-failed`.

These are already the "stable, finite values" the issue asks for; what is
missing is a durable sink for them.

"Fail-open" means the step does not fail the job when the external service is
unreachable or unconfigured. "Secret-gated" means the step is skipped entirely
when its credential is absent, so forks are unaffected. "OTLP" is the
OpenTelemetry Protocol; "Grafana Cloud" is the managed observability platform
receiving the metric.

Test layout relevant to this work: `cuprum/unittests/` holds unit and contract
tests; `tests/behaviour/` holds pytest-bdd scenarios that execute real `run:`
scripts extracted from `ci.yml`; `tests/helpers/` holds narrow workflow models
shared by those tests, and `tests/helpers/workflow.py` is documented as the one
place `ci.yml` is parsed. The Makefile's `PYTEST_TARGETS` variable is an
explicit list of globs, so a new test directory is not collected by `make test`
until it is added there.

"act" (nektos/act) executes GitHub Actions workflows locally in containers. It
is not a perfect runner emulation, and the repository already documents it in
`docs/local-validation-of-github-actions-with-act-and-pytest.md`.

## Conformance basis

There is no upstream Terms of Reference or technical-design document for this
work; the governing specification is GitHub issue #339, whose acceptance
criteria are the traced items below. ADR-011 and ADR-012 are created by this
plan and become part of the conformance basis once accepted.

- Upstream artefact: GitHub issue #339 "Add infrastructure for durable
  benchmark-gate telemetry and an Actions-runner integration harness".
- Governing standard: OpenTelemetry Protocol (OTLP)/JSON, specifically
  <https://opentelemetry.io/docs/specs/otlp/> (field naming, enum and int64
  encoding) and
  <https://opentelemetry.io/docs/specs/otel/compatibility/prometheus_and_openmetrics/>
  (metric-name translation).
- External contract: Grafana Cloud OTLP gateway, `/otlp/v1/metrics`, HTTP Basic
  authentication, `metrics:write` scope.
- ADRs referenced: ADR-006 (context package split) supplies the ADR format;
  ADR-011 and ADR-012 are delivered by this plan.

Trace links from issue acceptance criteria to milestones and evidence:

```plaintext
#339-AC1 (durable sink receives bounded metrics)
  -> EP-M2 -> tests/test_benchmark_gate_telemetry.py
#339-AC2 (documented query and retention guidance)
  -> EP-M3 -> docs/ci-benchmark-gate-telemetry.md
#339-AC3 (harness executes the workflow boundary; verifies detector output and
          benchmark admission for pull requests and non-pull-request events)
  -> EP-M1 -> tests/integration/test_workflow_integration.py
#339-AC4 (harness covers relevant, irrelevant, mixed, empty and detector-failure)
  -> EP-M1 -> tests/integration/test_workflow_integration.py (five cases)
#339-AC5 (no unbounded or sensitive values in label positions)
  -> EP-M2 -> tests/test_benchmark_gate_telemetry.py
```

## Verification plan

The obligations below are stated against the implementation this plan
introduces. Each names the method, the artefact, the evidence, and the
non-vacuity argument that stops the evidence being vacuous.

Axioms (external facts treated as given, not verified here):

- A1: The OpenTelemetry specification's encoding rules are as quoted in
  `Decision log`. Verified by reading the specification; not re-derived.
- A2: Grafana Cloud accepts OTLP/JSON at `/otlp/v1/metrics` with HTTP Basic
  authentication. Verified from Grafana's own published curl example for the
  sibling traces endpoint, which documents the same gateway and auth scheme.
- A3: `act` faithfully executes the subset of Actions semantics the `changes`
  job uses (checkout, `dorny/paths-filter`, `env`, `run`, step summary, step
  outcomes). This is a *non-trivial* axiom and the harness exists precisely to
  exercise the boundary; its limits are documented.
- A4: `curl` is present on `ubuntu-latest` GitHub runners and supports
  `--fail-with-body`, `--data-binary`, and `--max-time`.
- A5: `dorny/paths-filter` at the pinned SHA takes its local `git diff` path
  when `github.token` is empty. Verified by reading the action's cached source;
  the contract is exercised by the harness.

- Obligation: V1 — **Label closure**. Every label value the emission step can
  possibly emit is a member of its declared closed set: `event_class` is one of
  `pull_request` or `other`; `detector_status` is one of `success`, `failure`,
  or `unknown`; and `decision` is one of `run`, `skip`, or
  `skip-detector-failed`. Method: parameterized test plus source-level contract
  assertion. Rationale: the input space is a finite product of two closed sets
  and an event name, so exhaustive enumeration is cheap and complete for the
  behavioural half; a parser-level assertion catches a value that a future edit
  introduces without a matching test case. Domain: the triple of event name,
  detector outcome, and `bench`, where the event name ranges over
  `pull_request`, `push`, `workflow_dispatch`, and `schedule`; the detector
  outcome over `success`, `failure`, `cancelled`, and empty; and `bench` over
  `true`, `false`, and empty. Artefact:
  `tests/test_benchmark_gate_telemetry.py`. Evidence: `make test` passes; the
  test fails if a fourth label or a non-member value is introduced.
  Non-vacuity: witnesses exist for every member of all three vocabularies (the
  three `decision` values are produced by the three named scenarios). The
  negative control is a seeded mutation — adding `,run_id="$RANDOM"` to the
  label string — which the test must reject; this is applied by hand during the
  Green step and reverted.

- Obligation: V2 — **No unbounded or sensitive label**. The emitted label
  string contains no substituted value drawn from the changed-path set, command
  text, commit SHA, run ID, or wall-clock time. Method: contract test that
  parses the step's script and asserts that the label positions are built only
  from the three named shell variables, and that those variables are assigned
  only from literals or from the three gated inputs. Rationale: a static
  assertion over the script's own text is stronger than sampling outputs,
  because it constrains the whole input space rather than the cases a generator
  reaches. Domain: the `run:` text of the emission step. Artefact:
  `tests/test_benchmark_gate_telemetry.py`. Evidence: the test fails if
  `github.run_id`, `github.sha`, a timestamp, or a path list is introduced into
  a label. Non-vacuity: the negative control is the introduction of
  `printf '%s\n' "${notice} run_id=${GITHUB_RUN_ID}"`, which the test must
  reject for the intended reason.

- Obligation: V3 — **Gate equivalence**. The decision carried by the metric
  equals the decision that gates `benchmark-ratchet`, for every input in the
  enumerated domain. Stated as a lemma: the metric's `decision` label is a
  function of the same three inputs, computed by the same shell code, as the
  summary table's `decision` column. Method: reuse of the existing behavioural
  suite (`tests/behaviour/test_benchmark_gate_summary_behaviour.py`) extended
  to assert the metric, plus the harness's end-to-end observation. Rationale:
  the existing suite already executes the real script and already asserts
  agreement with a pure model (`tests/helpers/workflow_gate.py`), so extending
  it keeps one source of truth rather than adding a parallel one. Domain: as
  V1. Artefact: `tests/behaviour/test_benchmark_gate_summary_behaviour.py` and
  `tests/integration/test_workflow_integration.py`. Evidence: `make test`
  passes; the harness asserts the gate table rendered by the real job for five
  scenarios. Non-vacuity: the pre-existing pure model `benchmark_runs` is the
  oracle and is already exercised; the harness supplies an independent,
  out-of-process witness (the job's own output) that would disagree if the
  script drifted.

- Obligation: V4 — **Fail-open**. With the sink credential absent, unset, or
  pointing at an unreachable host, the `changes` job still reaches its terminal
  state with the same recorded decision, and does not fail. Method: integration
  test against the real job with the credential unset (already the default under
  `act`), plus a contract assertion that the step cannot fail the job.
  Rationale: the property is about process behaviour under a degraded
  dependency, which is observable only by running the process. Domain:
  credential absent; credential present but host unresolvable. Artefact:
  `tests/integration/test_workflow_integration.py`,
  `tests/test_benchmark_gate_telemetry.py`. Evidence: the harness run for the
  relevant scenario exits 0 and prints its table with no telemetry step
  executed; the contract test asserts `|| true`-equivalent semantics.
  Non-vacuity: the positive control is that the step *is* present and would run
  given a credential, asserted by the contract test, so "fail-open" is not
  achieved by the step being absent.

- Obligation: V5 — **Detector-failure evidence is recorded**. When
  `dorny/paths-filter` fails, the decision recorded is `skip-detector-failed`,
  the `bench` column reads `unknown` rather than `false`, and the job's
  non-zero exit does not suppress the record. Method: integration test against
  the real job with a fixture that makes the detector fail; parameterized
  behavioural test for the script in isolation. Rationale: this is the case
  where a naive implementation records a misleading `false`, and it is the case
  a maintainer most needs. Domain: base SHA set to the null SHA, which makes
  the action's `git diff` fail with exit 128. Artefact:
  `tests/integration/test_workflow_integration.py`. Evidence: observed during
  planning as `| pull_request | failure | unknown | skip-detector-failed |`
  with exit code 1 and `STEP FAILED: Detect performance-relevant changes`.
  Non-vacuity: the same fixture against a healthy base SHA produces `success`
  and `run`, proving the failure is caused by the injected fault and not by the
  fixture being universally broken.

- Obligation: V6 — **Harness input coverage**. The harness exercises relevant,
  irrelevant, mixed, and empty changed-path sets, and both pull-request and
  non-pull-request event classes. Method: parameterized integration cases, one
  per cell, each asserting the `bench` output and the resulting decision.
  Rationale: the four path sets are the complete partition of the filter's
  interesting behaviour (match, no match, partial match, no changes at all),
  and the two event classes are the branch in the gate expression. Domain:
  {relevant, irrelevant, mixed, empty} × {pull_request, push}. Artefact:
  `tests/integration/test_workflow_integration.py` and `tests/fixtures/` event
  and change-set fixtures. Evidence: each case asserts both
  `steps.filter.outputs.bench` and the gate table's decision column.
  Non-vacuity: committed during planning for four of the cells (relevant,
  irrelevant, push, detector-failure); the mixed and empty cells are added in
  the implementation and each must be shown to produce a distinct `bench`
  value, so they cannot be silently collapsed into an existing case.

- Obligation: V7 — **The harness itself is correct**. Its `act --json` parsing
  takes the last value for a repeated output name, and its summary extraction
  recovers the table the job actually wrote. Method: unit tests over captured
  `act` JSON streams, including a stream with a stale earlier value. Rationale:
  the parser is repository-owned logic layered on a third-party interface
  (axiom A3), which the plan is required to verify at the boundary; and the
  cumulative-stream trap was observed during planning, so a test that does not
  encode it would pass vacuously. Domain: synthetic streams with one value,
  repeated values, and no value. Artefact: `tests/helpers/act_harness.py` tests
  within `tests/integration/test_workflow_integration.py`. Evidence: the
  stale-value case fails against a "first match" parser and passes against a
  "last match" parser. Non-vacuity: the negative control is the first-match
  implementation, which is expected to produce the wrong value on the synthetic
  stream.

Residual gaps, stated explicitly: the harness does not verify that Grafana
Cloud *accepts* the payload, because that requires a live credential and would
make the suite depend on a third-party service. It verifies that the payload is
well-formed against the specification and that the step is correctly wired. The
operator-facing read-back check for this residual gap is a documented step in
`docs/ci-benchmark-gate-telemetry.md`.

## Milestones and plateaus

- EP-M0: **Decisions recorded.** ADR-011 and ADR-012 exist, are linked from
  `docs/contents.md` and the "Accepted architectural decisions" list in
  `docs/developers-guide.md`, and record the wire format and harness design
  decisions with their sources. Requirements and gaps: #339-AC1, #339-AC3
  (design half). Acceptance evidence: `make markdownlint` passes; the two links
  resolve. Conformance check: no interface, dependency, trust boundary, or
  persisted-format change beyond those the ADRs record. Recovery: revert the
  two ADR files and the two index edits. Remaining gaps: no code yet.
  Compatibility decision: none required; ADRs are new documents.

- EP-M1: **Harness plateau.** `tests/helpers/act_harness.py`,
  `tests/fixtures/` event fixtures, and
  `tests/integration/test_workflow_integration.py` exist and pass on a machine
  with `act` and podman, and skip cleanly without them. Requirements and gaps:
  #339-AC3, #339-AC4. Acceptance evidence: `make test` passes and the
  integration module reports five passing or skipping cases. Conformance check:
  the harness runs the real `changes` job; no fake workflow is introduced; the
  worktree is never bound into a container. Recovery: delete
  `tests/helpers/act_harness.py`, `tests/fixtures/`, and `tests/integration/`.
  Remaining gaps: the telemetry step does not yet exist, so the harness's
  assertion of the metric is a no-op at this plateau. Compatibility decision:
  none; these are test-only surfaces, which the execplans standard explicitly
  exempts from compatibility machinery.

- EP-M2: **Telemetry plateau.** The emission step is present in the `changes`
  job, secret-gated, fail-open, and carries exactly the three closed
  vocabularies; the contract tests pass. Requirements and gaps: #339-AC1,
  #339-AC5. Acceptance evidence: `make test` passes;
  `tests/test_benchmark_gate_telemetry.py` passes; a seeded mutation to the
  label string is rejected. Conformance check: the step is on the same code
  path as the summary; the `changes` job's permissions are unchanged, so the
  existing pinned contract test still passes. Recovery: revert the `ci.yml`
  hunk. Remaining gaps: no operational documentation yet. Compatibility
  decision: none; the step is new.

- EP-M3: **Documentation and gate plateau.**
  `docs/ci-benchmark-gate-telemetry.md` documents the sink, the label
  vocabulary, the query surface, the retention window, the alerting rule, and
  the fail-open degradation; `docs/contents.md` and `docs/developers-guide.md`
  link it; `docs/local-validation-of-github-actions-with-act-and-pytest.md` is
  aligned with the harness as built. Requirements and gaps: #339-AC2.
  Acceptance evidence: `make markdownlint` and `make check-fmt` pass.
  Conformance check: every claim in the operational doc matches an assertion in
  a test or a recorded decision here. Recovery: revert the documentation
  commit. Remaining gaps: none for this issue. Compatibility decision: none.

## Plan of work

Stage A is complete (reconnaissance and design validation, above).

Stage B: red tests. Add `tests/test_benchmark_gate_telemetry.py` asserting the
properties of a step that does not yet exist; run it and observe it fail for
the intended reason ("no step named …"). Add
`tests/integration/test_workflow_integration.py` and
`tests/helpers/act_harness.py` with the parsing unit tests first, so the
harness's own correctness is red before the harness is used. Add the event
fixtures.

Stage C: implementation. Add the emission step to the `changes` job in
`.github/workflows/ci.yml`, immediately after "Record the benchmark gate
decision" and carrying the same `if: ${{ !cancelled() }}` condition, so both
the summary and the metric are recorded on the detector-failure path. Add the
harness implementation and the Makefile target.

Stage D: refactor and documentation. Write the operational doc, update the
indices, align the act design doc, then run the full gates.

## Concrete steps

All commands run from the repository root, which is the worktree directory.

1. Observe the red state for the telemetry contract test:

   ```bash
   uv run pytest -v tests/test_benchmark_gate_telemetry.py
   ```

   Expected: collection succeeds and the assertions fail with a message naming
   the missing step.

2. Observe the red state for the harness parser tests:

   ```bash
   uv run pytest -v tests/integration/test_workflow_integration.py -k parser
   ```

   Expected: the stale-value case fails against a first-match parser.

3. After the `ci.yml` edit, observe green:

   ```bash
   uv run pytest -v tests/test_benchmark_gate_telemetry.py \
     tests/behaviour/test_benchmark_gate_summary_behaviour.py
   ```

   Expected: all pass, with the pre-existing behavioural suite unchanged.

4. Run the integration harness end to end:

   ```bash
   uv run pytest -v tests/integration/test_workflow_integration.py
   ```

   Expected with podman and `act` present: five passing cases. Expected without
   them: five skips carrying the reason.

5. Run the full gates sequentially, delegating to `scrutineer`:
   `make check-fmt`, `make lint`, `make typecheck`, `make test`,
   `make markdownlint`, `make nixie`.

## Validation and acceptance

Acceptance is behavioural. After the change:

- `make test` passes, and the new module
  `tests/test_benchmark_gate_telemetry.py` fails before the `ci.yml` edit and
  passes after it. Its failure mode before the edit is a missing step, not an
  import error.
- `uv run pytest -v tests/integration/test_workflow_integration.py` runs the
  real `changes` job for five scenarios and asserts, for each, the detector's
  `bench` output and the gate's recorded decision. On this machine it is
  expected to pass rather than skip, because podman and `act` are present.
- With the telemetry secret unset — the default under `act` — the job still
  reaches `stepResult=success` for the recording step, demonstrating fail-open
  behaviour.
- The recorded decisions observed during planning, which the harness must
  reproduce, are:

  ```plaintext
  relevant          | pull_request | success | true    | run
  irrelevant        | pull_request | success | false   | skip
  push to main      | push         | success | false   | run
  detector failure  | pull_request | failure | unknown | skip-detector-failed
  ```

Red-Green-Refactor evidence is recorded under `Progress` as each stage lands.

Quality criteria:

- Tests: the full `make test` suite passes.
- Verification: obligations V1–V7 are discharged as described in the
  `Verification plan`.
- Lint/typecheck: `make lint` and `make typecheck` pass, including
  `interrogate` docstring coverage at 100 per cent for new modules.
- Security: no credential is committed; the token is read from `secrets` only;
  no repository content other than the three closed vocabulary values leaves
  the runner.

## Idempotence and recovery

Every step is re-runnable. The `ci.yml` edit is additive and reverts with
`git checkout -- .github/workflows/ci.yml`. The harness creates and removes its
own temporary repository under the system temporary directory and cleans up its
containers; if a run is interrupted, remove leftovers with
`podman ps -a --filter name=act- --format '{{.Names}}' | xargs -r podman rm -f`.
The harness never binds the developer's worktree, so an interrupted run cannot
corrupt the checkout.

## Artefacts and notes

The decisive feasibility evidence, captured during planning from the real
`changes` job under `act`:

```plaintext
$ DOCKER_HOST=unix:///run/user/1000/podman/podman.sock timeout 420 act pull_request \
    -W .github/workflows/ci.yml --job changes --eventpath ev.json -s GITHUB_TOKEN= \
    -P ubuntu-latest=catthehacker/ubuntu:act-latest --json
EXIT=0  stdout=50 stderr=1

  -> stepResult=success step='Check out repository'
[info] step='Detect performance-relevant changes' :: '  ⚙  ::set-output:: bench=true'
  -> stepResult=success step='Detect performance-relevant changes'
  -> stepResult=success step='Record the benchmark gate decision'
[info] step='Record the benchmark gate decision' :: '  ⚙  Summary - ### Benchmark gate

| event | detector | performance-relevant changes | benchmark-ratchet |
| --- | --- | --- | --- |
| pull_request | success | true | run |
'
```

## Interfaces and dependencies

No Python or Rust production interface changes. The new surfaces are:

- `.github/workflows/ci.yml`: a new step in the `changes` job named
  `Publish the benchmark gate decision`, placed after
  `Record the benchmark gate decision`, with `if: ${{ !cancelled() }}` and an
  environment of `TELEMETRY_TOKEN`, `ENDPOINT`, `EVENT`, `BENCH`, `DETECTOR`.

- `tests/helpers/act_harness.py`:

  ```python
  def run_act(
      *,
      event: str,
      event_path: pth.Path,
      repository: pth.Path,
      job: str = "changes",
      timeout: float = 600.0,
  ) -> ActRun: ...
  ```

  where `ActRun` exposes `exit_code`, `last_output(name)` for the last
  `set-output` value of a named output, `failed_steps`, and `summary()` for the
  Markdown block last written to `$GITHUB_STEP_SUMMARY`.

- `tests/fixtures/` event payloads, one per scenario, and change-set builders
  that produce the relevant, irrelevant, mixed, and empty path sets in the
  temporary repository.

- `tests/integration/test_workflow_integration.py`: the parameterized
  scenarios, and the parser unit tests.

- A Makefile target that runs the integration module, gated on a container
  runtime being available, following the `CUPRUM_RUN_BENCHMARKS` precedent.

## Revision note

2026-09-16 (telemetry milestone): the emission step, the contract suite that
covers it, and the shared-tokenizer fix are implemented and staged. `Progress`,
`Surprises & discoveries`, and `Decision log` carry this stage's evidence,
including the `count_over_time` query requirement that
`docs/ci-benchmark-gate-telemetry.md` must state. Two gates were red on the
first run and are fixed pending re-run.

2026-09-16: initial draft, written after reconnaissance and the feasibility
experiment. Status set to IN PROGRESS because the task instructions authorize
implementation without a separate approval gate. The `Verification plan`
obligations and the `Decision log` entries are pre-populated from evidence
gathered during planning, so that the implementation stages can discharge them
rather than invent them.
