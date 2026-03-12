# Generate benchmark comparison report and workflow summary table (4.4.4)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: COMPLETE

Roadmap reference: `docs/roadmap.md` item `4.4.4`.

## Purpose / big picture

Roadmap item `4.4.4` requires the benchmark CI flow to produce a human-readable
comparison report for the Python and Rust stream backends and publish a summary
table to the GitHub Actions workflow summary.

After this work, a maintainer opening a pull request or inspecting a `main`
workflow run can read the workflow summary and immediately see how the current
candidate smoke benchmark compares Python and Rust for the same benchmark
scenario. The existing Rust regression ratchet from `4.4.3` remains the gating
check against the latest successful `main` baseline artefact, while `4.4.4`
adds same-run Python-versus-Rust evidence for the candidate checkout.

This task is complete only when:

- CI still runs the existing `benchmark-ratchet` job on pull requests and
  pushes to `main`.
- The job generates a structured comparison report artefact derived from the
  candidate smoke benchmark plan and throughput JSON.
- The job appends a Markdown summary table to `$GITHUB_STEP_SUMMARY` showing
  Python and Rust results for matched scenarios.
- Unit tests (`pytest`) and behavioural tests (`pytest-bdd`) cover report
  generation, contract validation, and workflow-facing summary output.
- `docs/cuprum-design.md` and `docs/users-guide.md` describe the comparison
  report and workflow summary behaviour.
- `docs/roadmap.md` marks `4.4.4` as done.
- Quality gates pass: `make check-fmt`, `make typecheck`, `make lint`,
  `make test`, `make markdownlint`, and `make nixie`.

## Constraints

- Keep scope bounded to roadmap item `4.4.4`. Do not redesign the `4.4.3`
  ratchet threshold or baseline-fetch logic.
- Follow repository test-first policy:
  - add or modify tests first;
  - capture a failing state;
  - implement the minimal code to pass.
- Provide both test styles:
  - unit tests in `cuprum/unittests/` with `pytest`;
  - behavioural tests in `tests/behaviour/` plus Gherkin in
    `tests/features/` using `pytest-bdd`.
- Keep benchmark scenario naming and selection compatible with
  `benchmarks/pipeline_throughput.py` and
  `benchmarks/ci_benchmark_ratchet_profile.py`.
- Keep the existing public runtime API in `cuprum/` stable.
- Do not add runtime dependencies or new GitHub Actions marketplace actions.
- Keep the comparison report deterministic and derived from checked-in JSON
  contracts rather than ad hoc shell parsing.
- Keep the workflow summary generation additive: when the baseline artefact is
  unavailable, the Python-versus-Rust summary table must still be published for
  the candidate smoke run.
- Keep documentation aligned in `docs/cuprum-design.md`,
  `docs/users-guide.md`, and `docs/roadmap.md`.
- Keep Python changes compliant with `.rules/python-*.md`, Ruff, and typing
  checks.

## Tolerances (exception triggers)

- Scope: if implementation requires edits to more than 10 files, including
  this ExecPlan, stop and escalate.
- Interface: if `benchmarks/ci_benchmark_ratchet_profile.py` or
  `benchmarks/ratchet_rust_performance.py` need breaking CLI changes rather
  than additive flags or a new helper module, stop and escalate.
- Dependencies: if a new third-party Python package or GitHub Action is
  required, stop and escalate.
- Iterations: if the same failing test persists after 3 fix attempts, stop and
  escalate with options.
- Runtime: if the new report-generation step adds more than 2 minutes to the
  `benchmark-ratchet` job on `ubuntu-latest`, stop and propose a cheaper
  summary path.
- Ambiguity: if the expected summary table must include baseline-versus-
  candidate data in addition to Python-versus-Rust data, stop and ask for a
  decision before broadening the report contract.

## Risks

- Risk: the filtered CI smoke profile might not retain perfectly matched Python
  and Rust scenario pairs for every comparison row. Severity: high. Likelihood:
  low. Mitigation: validate pairings explicitly by shared scenario dimensions
  and fail fast if a Python or Rust counterpart is missing.

- Risk: report code may duplicate JSON validation logic already used by the
  ratchet and baseline-fetch helpers. Severity: medium. Likelihood: medium.
  Mitigation: reuse `benchmarks/_validation.py` and extract only minimal new
  helpers when duplication becomes concrete.

- Risk: GitHub Actions workflow summary formatting may become brittle if the
  summary is assembled inline in shell heredocs. Severity: medium. Likelihood:
  medium. Mitigation: generate Markdown from a checked-in Python helper, write
  it to a file, and append that file to `$GITHUB_STEP_SUMMARY`.

- Risk: local validation can miss workflow-only issues because full GitHub
  Actions execution is not available in this environment. Severity: medium.
  Likelihood: medium. Mitigation: cover workflow-facing behaviour with
  CLI-based behavioural tests, keep shell glue minimal, and validate workflow
  syntax statically.

- Risk: a generated local native module
  (`cuprum/_rust_backend_native*.so`) can unexpectedly change `make test`
  behaviour during final gates. Severity: medium. Likelihood: medium.
  Mitigation: remove generated native artefacts before final gate runs unless
  Rust-path testing is intentionally part of the validation step.

## Progress

- [x] (2026-03-08 00:00Z) Reviewed roadmap item `4.4.4`, the completed
  `4.4.3` benchmark-ratchet implementation, the benchmark ADR, and repository
  documentation/testing constraints.
- [x] (2026-03-08 00:10Z) Drafted this ExecPlan for roadmap item `4.4.4`.
- [x] (2026-03-12 23:14Z) Stage A: added fail-first unit and behavioural tests
  for Python-versus-Rust report generation and workflow-summary Markdown
  output. Red phase confirmed with `ModuleNotFoundError` for
  `benchmarks.python_vs_rust_comparison_report`.
- [x] (2026-03-12 23:20Z) Stage B: implemented
  `benchmarks/python_vs_rust_comparison_report.py` with paired scenario
  validation, JSON report writing, ratchet-status loading, Markdown summary
  rendering, and CLI exit codes.
- [x] (2026-03-12 23:25Z) Stage C: integrated comparison-report generation and
  workflow-summary publication into `.github/workflows/ci.yml`, including
  uploaded Markdown artefacts for inspection.
- [x] (2026-03-12 23:28Z) Stage D: updated `docs/cuprum-design.md` and
  `docs/users-guide.md`, and marked roadmap item `4.4.4` done in
  `docs/roadmap.md`.
- [x] (2026-03-12 23:42Z) Stage E: final validation complete. Passed
  `make fmt`, `make check-fmt`, `make typecheck`, `make lint`, `make test`,
  `make markdownlint`, and `make nixie`.

## Surprises & discoveries

- Observation: `.github/workflows/ci.yml` already downloads the latest
  successful `main` baseline artefact, runs the candidate smoke benchmarks, and
  uploads JSON artefacts, but it does not write anything to
  `$GITHUB_STEP_SUMMARY`. Impact: `4.4.4` should extend the existing
  `benchmark-ratchet` job rather than introducing a second benchmark-reporting
  job.

- Observation: `benchmarks/ratchet_rust_performance.py` already produces a
  JSON comparison report, but that report compares baseline versus candidate
  Rust means only. Impact: `4.4.4` needs a separate report contract or additive
  helper focused on Python-versus-Rust data from the candidate smoke run.

- Observation: `benchmarks/ci_benchmark_ratchet_profile.py` already writes a
  filtered candidate plan containing the exact scenario list paired with the
  throughput JSON used by CI. Impact: the new report should consume the
  filtered candidate plan and throughput files instead of rebuilding scenario
  selection logic.

- Observation: the accepted Rust-extension ADR already promises that CI will
  generate comparison reports in the GitHub Actions summary. Impact: the
  implementation should align the workflow with the ADR rather than introducing
  a different reporting surface.

- Observation: placing summary publication after the ratchet command in the
  same shell step would suppress the workflow summary whenever the Rust ratchet
  failed with exit code `1`. Impact: summary generation and publication were
  moved into separate `if: always()` workflow steps so reviewers still get the
  Python-versus-Rust table on failing benchmark runs.

- Observation: `make test` initially failed on an unrelated timing assertion in
  `cuprum/unittests/test_concurrency.py::test_concurrency_none_allows_unlimited`
   while several other gate commands were running concurrently. Impact: the
  final `make test` validation was rerun in isolation and passed cleanly; the
  `4.4.4` implementation did not require changes to concurrency code.

## Decision log

- Decision: generate the Python-versus-Rust comparison from the candidate smoke
  benchmark artefacts (`candidate-plan.json` and `candidate-throughput.json`)
  rather than from baseline artefacts. Rationale: roadmap item `4.4.4` is about
  backend comparison, while `4.4.3` already covers baseline-versus-candidate
  regression gating. Date/Author: 2026-03-08 / Codex.

- Decision: keep report generation in a checked-in Python helper module with a
  small CLI instead of assembling Markdown directly in the workflow shell.
  Rationale: Python code is easier to unit test, validate, and reuse for both
  JSON output and Markdown summary rendering. Date/Author: 2026-03-08 / Codex.

- Decision: treat missing Python or Rust counterparts in a comparison group as
  a contract error. Rationale: a partial comparison table would hide benchmark
  selection drift and undermine confidence in the CI summary. Date/Author:
  2026-03-08 / Codex.

- Decision: publish the summary table in a dedicated `if: always()` workflow
  step and upload `comparison-summary.md` alongside the JSON artefacts.
  Rationale: preserves reviewer-visible output on ratchet failures and keeps
  the generated Markdown easy to inspect outside the GitHub Actions summary.
  Date/Author: 2026-03-12 / Codex.

## Outcomes & retrospective

Implementation completed successfully.

Delivered:

- `benchmarks/python_vs_rust_comparison_report.py` with validated candidate
  plan/throughput pairing, ratchet-status summary loading, JSON report output,
  Markdown workflow-summary rendering, and CLI exit codes.
- Unit tests in `cuprum/unittests/test_benchmark_comparison_report.py`.
- Behavioural tests in
  `tests/behaviour/test_benchmark_comparison_report_behaviour.py` with
  `tests/features/benchmark_comparison_report.feature`.
- Workflow integration in `.github/workflows/ci.yml` so the existing
  `benchmark-ratchet` job writes `comparison-report.json`,
  `comparison-summary.md`, appends the Markdown table to
  `$GITHUB_STEP_SUMMARY`, and uploads both artefacts.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`.
- Roadmap update in `docs/roadmap.md`, marking item `4.4.4` done.

Quality and validation summary:

- Red phase: targeted tests failed with `ModuleNotFoundError` for the new
  comparison-report helper.
- Green phase: benchmark-specific unit and behavioural tests passed.
- Final gates passed:
  - `make fmt`
  - `make check-fmt`
  - `make typecheck`
  - `make lint`
  - `make test`
  - `make markdownlint`
  - `make nixie`

## Context and orientation

Relevant repository areas and current behaviour:

- `.github/workflows/ci.yml`
  - defines the `benchmark-ratchet` job;
  - fetches the latest successful `main` baseline artefact;
  - runs smoke benchmarks for the candidate checkout;
  - writes `candidate-plan.json`, `candidate-throughput.json`, and
    `ratchet-report.json`;
  - uploads benchmark JSON artefacts.
- `benchmarks/ci_benchmark_ratchet_profile.py`
  - reads the full dry-run plan from `benchmarks/pipeline_throughput.py`;
  - filters the smoke matrix used in CI;
  - runs `hyperfine`;
  - writes the filtered plan whose `scenarios` list aligns with the throughput
    `results` list by index.
- `benchmarks/ratchet_rust_performance.py`
  - validates plan and throughput JSON;
  - compares baseline-versus-candidate Rust scenario means;
  - writes `ratchet-report.json`.
- `benchmarks/_validation.py`
  - centralizes JSON-shape validation helpers already reused by benchmark
    helpers.
- `cuprum/unittests/test_ci_benchmark_ratchet_profile.py`
  - covers CI smoke profile selection.
- `cuprum/unittests/test_benchmark_ci_ratchet.py`
  - covers ratchet report JSON and comparison logic.
- `tests/behaviour/test_benchmark_ci_ratchet_behaviour.py` and
  `tests/features/benchmark_ci_ratchet.feature`
  - cover CLI-facing ratchet behaviour.
- `docs/cuprum-design.md` section 13.9
  - documents the benchmark suite, CI ratchet, and artefacts.
- `docs/users-guide.md`
  - documents benchmark commands and CI benchmark behaviour.

Terminology used in this plan:

- Candidate run: the smoke benchmark execution for the commit under test in the
  current workflow run.
- Comparison group: the set of scenarios that share the same payload size,
  stage count, and callback mode, differing only by backend (`python` or
  `rust`).
- Speedup ratio: `python_mean / rust_mean` for the matched comparison group.
  Values greater than `1.0` mean Rust is faster; values less than `1.0` mean
  Python is faster.

## Plan of work

Stage A: tests first.

Add unit tests for a new Python-versus-Rust comparison helper module. Cover:

1. loading and validating the filtered plan and throughput JSON;
2. grouping matching Python and Rust scenarios by shared dimensions;
3. rejecting missing or duplicate backend pairs;
4. calculating comparison rows and speedup ratios deterministically;
5. rendering Markdown summary text with the expected header and table columns.

Add behavioural scenarios that invoke the report CLI with fixture JSON files
and assert:

- the command exits successfully for valid paired inputs;
- the JSON report contains matched Python and Rust means plus derived speedup;
- the Markdown summary contains a table suitable for
  `$GITHUB_STEP_SUMMARY`;
- malformed inputs fail with a configuration-error exit code.

Suggested test files:

- `cuprum/unittests/test_benchmark_comparison_report.py`
- `tests/behaviour/test_benchmark_comparison_report_behaviour.py`
- `tests/features/benchmark_comparison_report.feature`

Go/no-go: proceed only after the new tests fail because the report helper does
not yet exist.

Stage B: implement the report-generation helper and CLI.

Create a dedicated helper module, preferably
`benchmarks/python_vs_rust_comparison_report.py`, that:

1. reads the filtered candidate plan and candidate throughput JSON;
2. validates that each comparison group has exactly one Python and one Rust
   entry;
3. computes per-group comparison rows with explicit fields for scenario label,
   Python mean, Rust mean, speedup ratio, and faster backend;
4. writes a structured JSON report artefact;
5. renders a Markdown summary block that can be appended to the GitHub Actions
   workflow summary;
6. optionally includes ratchet status metadata from `ratchet-report.json` so
   the summary can mention whether baseline comparison passed, failed, or was
   skipped.

Keep the CLI deterministic and file-based. A likely command shape is:

```plaintext
uv run python benchmarks/python_vs_rust_comparison_report.py \
  --plan /tmp/candidate-plan.json \
  --throughput /tmp/candidate-throughput.json \
  --ratchet-report /tmp/ratchet-report.json \
  --output-json /tmp/comparison-report.json \
  --output-markdown /tmp/comparison-summary.md
```

Suggested JSON report contract:

```json
{
  "rows": [
    {
      "comparison_id": "small-single-nocb",
      "python_scenario_name": "python-small-single-nocb",
      "rust_scenario_name": "rust-small-single-nocb",
      "python_mean": 0.42,
      "rust_mean": 0.21,
      "speedup_ratio": 2.0,
      "faster_backend": "rust"
    }
  ],
  "summary": {
    "row_count": 1,
    "rust_wins": 1,
    "python_wins": 0
  }
}
```

The exact field names may evolve during implementation, but the report must
remain explicit, stable, and easy to validate in tests.

Go/no-go: proceed only after the new unit and behavioural tests pass.

Stage C: integrate report generation into the workflow.

Update `.github/workflows/ci.yml` so the existing `benchmark-ratchet` job:

1. runs the new report helper after `ratchet-report.json` is available;
2. writes `comparison-report.json` and `comparison-summary.md` into the same
   artefact directory used by the other benchmark outputs;
3. appends `comparison-summary.md` to `$GITHUB_STEP_SUMMARY`;
4. uploads the new report artefacts with the existing benchmark artefact set.

Keep the workflow summary path simple and observable. Prefer:

```plaintext
cat "${artifact_dir}/comparison-summary.md" >> "${GITHUB_STEP_SUMMARY}"
```

instead of embedding large Markdown blocks inline in shell scripts.

Expected workflow-summary content:

- a short heading explaining that the table reflects the candidate smoke
  benchmark run;
- one row per matched Python/Rust comparison group;
- columns for scenario, Python mean, Rust mean, speedup ratio, and faster
  backend;
- a short ratchet status line stating whether baseline comparison passed,
  failed, or was skipped because no previous `main` baseline artefact exists.

Go/no-go: proceed only after targeted tests and local workflow file checks pass.

Stage D: documentation and roadmap updates.

Update the design and user documentation to describe:

- what the new comparison report contains;
- where the workflow summary table appears;
- how it relates to the existing Rust regression ratchet;
- how to interpret the speedup ratio and winner column.

Files to update:

- `docs/cuprum-design.md`
- `docs/users-guide.md`
- `docs/roadmap.md` (mark `4.4.4` as done only after implementation and
  validation are complete)

Keep wording aligned with the accepted ADR promise that CI generates comparison
reports in the GitHub Actions summary.

Stage E: validation and close-out.

Run targeted tests first, then the full repository gates. Capture outcomes in
`Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective`.

Use `tee` and `set -o pipefail` for all long-running commands so the exit code
is preserved and the logs remain reviewable:

```bash
set -o pipefail; make fmt 2>&1 | tee /tmp/4-4-4-make-fmt.log
set -o pipefail; make check-fmt 2>&1 | tee /tmp/4-4-4-check-fmt.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/4-4-4-typecheck.log
set -o pipefail; make lint 2>&1 | tee /tmp/4-4-4-lint.log
set -o pipefail; make test 2>&1 | tee /tmp/4-4-4-test.log
set -o pipefail; MDLINT=/root/.bun/bin/markdownlint-cli2 make markdownlint \
  2>&1 | tee /tmp/4-4-4-markdownlint.log
set -o pipefail; make nixie 2>&1 | tee /tmp/4-4-4-nixie.log
```

If local native artefacts exist, remove them before the final default gate run:

```bash
rm -f cuprum/_rust_backend_native*.so
```

## Acceptance evidence

The implementing agent should capture concise evidence showing:

- the new unit tests fail before implementation and pass after;
- the behavioural test demonstrates a generated Markdown summary table;
- the workflow writes `comparison-report.json` and `comparison-summary.md`;
- `$GITHUB_STEP_SUMMARY` receives the rendered summary table;
- the final quality gates pass;
- `docs/roadmap.md` item `4.4.4` is checked only in the completed change.

## Approval gate

This document is the draft phase only. Do not start implementation until the
user explicitly approves this ExecPlan or requests revisions.
