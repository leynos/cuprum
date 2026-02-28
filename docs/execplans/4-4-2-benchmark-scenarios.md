# Define benchmark scenario matrix (4.4.2)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: COMPLETE

Roadmap reference: `docs/roadmap.md` item `4.4.2`.

## Purpose / big picture

Roadmap item `4.4.2` requires defining a systematic benchmark scenario matrix
that exercises the throughput harness across three payload sizes, two pipeline
depths, and two line-callback modes. After this work, running:

```bash
uv run python benchmarks/pipeline_throughput.py \
  --smoke --dry-run --output /tmp/plan.json
```

produces a JSON plan with 12 scenarios (Python-only; 24 when Rust is
available), each with a systematic name, the correct payload size, stage count,
and callback flag. All scenarios follow the naming convention
`{backend}-{size}-{depth}-{callbacks}` (for example
`python-small-single-nocb` or `rust-large-multi-cb`).

This task is complete only when:

- `default_pipeline_scenarios()` returns the full combinatorial matrix;
- unit tests (`pytest`) and behavioural tests (`pytest-bdd`) validate the
  matrix properties;
- `docs/cuprum-design.md` and `docs/users-guide.md` are updated;
- `docs/roadmap.md` marks `4.4.2` as done;
- quality gates pass: `make check-fmt`, `make typecheck`, `make lint`,
  and `make test`.

## Constraints

- Keep scope bounded to roadmap item `4.4.2`. Do not implement CI workflow
  gating or report publication from `4.4.3` and `4.4.4` in this task.
- Do not modify `benchmarks/_benchmark_types.py`. The existing
  `PipelineBenchmarkScenario` dataclass already supports all required fields
  (`name`, `backend`, `payload_bytes`, `stages`, `with_line_callbacks`).
- Do not modify `benchmarks/pipeline_worker.py`. It already handles all
  scenario field combinations (arbitrary `stages >= 2`, both callback modes,
  arbitrary payload sizes).
- The minimum stage count remains 2 (`_MIN_PIPELINE_STAGES = 2`).
  "Single-stage" in the roadmap context means `stages=2` (writer|sink, zero
  passthrough stages). "Multi-stage" means `stages=3`
  (writer|passthrough|sink).
- Follow repository test-first policy: add or modify tests first, capture
  failing state, then implement minimal code to pass.
- Provide both test styles for this feature: unit tests using `pytest` in
  `cuprum/unittests/`; behavioural tests using `pytest-bdd` in
  `tests/behaviour/` with Gherkin in `tests/features/`.
- Do not change public runtime APIs exported from `cuprum/__init__.py`.
- Do not introduce runtime dependencies; no new dev dependencies are needed.
- Keep docs aligned: `docs/cuprum-design.md`, `docs/users-guide.md`, and
  `docs/roadmap.md`.
- Keep Python changes compliant with `.rules/python-*.md`, Ruff,
  type-checking, and project scripting standards.

## Tolerances (exception triggers)

- Scope: if implementation requires edits to more than 8 files (including the
  ExecPlan itself), stop and escalate.
- Interface: if `default_pipeline_scenarios()` must change its function
  signature (parameter names or types), stop and escalate. Adding new
  keyword-only parameters with defaults is acceptable.
- Dependencies: no new external dependencies are permitted.
- Iterations: if the same failing test needs more than 3 fix attempts, stop
  and escalate with options.
- Runtime: if the expanded smoke matrix makes `make test` materially slower
  (target: less than 10% increase), stop and propose an isolation strategy.
- Ambiguity: the "single-stage" interpretation (`stages=2`) is settled per the
  design constraint above. If any other ambiguity arises, stop and present
  options.

## Risks

- Risk: The expanded smoke-mode matrix (12 scenarios) may slow down `make test`
  via the BDD dry-run test.
  Severity: low. Likelihood: low (dry-run does not invoke hyperfine; scenario
  generation is fast). Mitigation: smoke payload sizes are kept small. Monitor
  `make test` wall time.

- Risk: Existing tests that assert on scenario counts or specific scenario
  names will break.
  Severity: medium. Likelihood: medium. Mitigation: review every existing test
  assertion before modifying the scenario generator. Existing tests use
  `any()`/`all()` assertions that remain valid with more scenarios.

- Risk: Large payload (100 MB) in non-smoke mode may cause resource issues in
  certain environments.
  Severity: low. Likelihood: low (non-smoke benchmarks are only run via
  `make benchmark-e2e`, which is opt-in). Mitigation: the 100 MB size matches
  the roadmap requirement. Document the resource expectation.

## Progress

- [x] (2026-02-28) Stage A: wrote failing tests (8 unit + 1 BDD scenario).
  Red phase confirmed: 7 new unit tests and 1 BDD scenario failed, all
  existing tests passed.
- [x] (2026-02-28) Stage B: implemented expanded scenario matrix. Replaced
  payload constants and rewrote `default_pipeline_scenarios()` with matrix
  generation. All 25 targeted tests passed. Dry-run output verified: 12
  scenarios with systematic names.
- [x] (2026-02-28) Stage C: updated documentation. Added scenario matrix
  description to `docs/cuprum-design.md` Section 13.9 and
  `docs/users-guide.md` "Benchmark suite" section. Marked roadmap 4.4.2 done.
- [x] (2026-02-28) Stage D: full validation. All quality gates passed:
  `make check-fmt`, `make typecheck`, `make lint`, `make test` (330 passed,
  43 skipped), `make markdownlint` (0 errors), `make nixie`.

## Surprises & discoveries

- Observation: the `test_default_pipeline_scenarios_no_duplicate_names` test
  passed even before implementation because the old 2-scenario set already had
  unique names. This was expected and harmless; the test still validates a
  meaningful invariant for the 24-scenario matrix.
  Evidence: Stage A red-phase output showed 7 failures, not 8.
  Impact: none; the test is valuable for the final matrix.

## Decision log

- Decision: "Single-stage" means `stages=2` (writer|sink, zero passthrough
  stages). Rationale: a cuprum pipeline requires at least 2 processes. The
  worker builds `1 writer + (stages-2) passthrough + 1 sink`. With `stages=2`,
  there are zero passthrough stages, which is the minimal "single-stage"
  pipeline. The existing `_MIN_PIPELINE_STAGES = 2` constraint is already
  correct. Date/Author: 2026-02-28 / DevBoxer.

- Decision: multi-stage uses `stages=3` (one passthrough), not a higher count.
  Rationale: 3 stages is the simplest multi-stage pipeline that exercises the
  passthrough code path. Higher stage counts can be added later if needed for
  stress testing. Date/Author: 2026-02-28 / DevBoxer.

- Decision: naming convention `{backend}-{size}-{depth}-{callbacks}` with
  short tokens (`nocb`/`cb`). Rationale: systematic names make scenario
  identification unambiguous in benchmark output. This replaces the current
  flat `pipeline-python`/`pipeline-rust` naming. Date/Author: 2026-02-28 /
  DevBoxer.

- Decision: smoke mode reduces payload sizes but preserves the full matrix
  shape. Smoke payloads are small=1 KB, medium=64 KB, large=1 MB. Rationale:
  the purpose of smoke mode is fast validation that the harness works, not
  performance measurement. Keeping the matrix shape identical ensures all code
  paths are exercised. Date/Author: 2026-02-28 / DevBoxer.

## Outcomes & retrospective

Completed implementation for roadmap item `4.4.2`.

Delivered:

- Expanded `default_pipeline_scenarios()` to return a 12-scenario-per-backend
  combinatorial matrix covering 3 payload sizes, 2 pipeline depths, and
  2 callback modes.
- Added 8 unit tests validating matrix count, naming, payload coverage, depth
  coverage, callback coverage, smoke payloads, and uniqueness.
- Added 1 BDD scenario with 5 step definitions validating the dry-run matrix
  structure through the CLI workflow.
- Updated `docs/cuprum-design.md` and `docs/users-guide.md` with scenario
  matrix documentation.
- Marked roadmap item `4.4.2` as done.

Quality and validation summary:

- Red phase: 7 new unit tests and 1 BDD scenario failed against the old
  2-scenario implementation.
- Green phase: all 25 targeted tests passed after implementation.
- Full suite: 330 passed, 43 skipped (Rust backend not available).
- All quality gates passed: `make check-fmt`, `make typecheck`, `make lint`,
  `make test`, `make markdownlint`, `make nixie`.

Lessons learned:

- The existing `PipelineBenchmarkScenario` dataclass and `pipeline_worker.py`
  needed zero changes, validating the original 4.4.1 design's extensibility.
- Ruff formatting preferences (no aligned inline comments) required adjusting
  constant formatting from the plan's proposed layout.

## Context and orientation

The benchmark suite lives in the `benchmarks/` package at the repository root,
separate from the main `cuprum/` library:

- `benchmarks/_benchmark_types.py` -- frozen dataclasses
  (`PipelineBenchmarkScenario`, `PipelineBenchmarkConfig`, `HyperfineConfig`,
  `PipelineBenchmarkRunResult`), `BackendName` type alias, and validation
  helpers. This is the data contract layer. NOT modified by this plan.
- `benchmarks/pipeline_throughput.py` -- contains
  `default_pipeline_scenarios()` (the function being expanded),
  `build_hyperfine_command()`, `run_pipeline_benchmarks()`, and CLI `main()`.
  This is the primary file to modify.
- `benchmarks/pipeline_worker.py` -- worker process that builds actual
  pipelines with writer/passthrough/sink stages. NOT modified by this plan.
- `cuprum/unittests/test_benchmark_suite.py` -- unit tests for the benchmark
  suite (12 existing tests).
- `tests/behaviour/test_benchmark_suite_behaviour.py` -- BDD step definitions
  for benchmark smoke workflow (1 existing scenario).
- `tests/features/benchmark_suite.feature` -- Gherkin feature file (1 existing
  scenario).
- `docs/cuprum-design.md` -- design document, Section 13.9 covers benchmark
  testing strategy (lines 1905-1939).
- `docs/users-guide.md` -- users' guide, "Benchmark suite" section (lines
  1025-1069).
- `docs/roadmap.md` -- roadmap, item 4.4.2 (lines 166-168).

Current state of `default_pipeline_scenarios()`: returns at most 2 scenarios
(1 Python + 1 optional Rust), both with `stages=3`,
`with_line_callbacks=False`, and a single payload size (1 KB smoke / 1 MB
normal). After this plan: 12 per backend (24 with Rust) covering the full
combinatorial matrix.

## Plan of work

Stage A: write failing tests first.

Add unit tests that assert the expected properties of the expanded scenario
matrix (count, naming pattern, payload coverage, depth coverage, callback
coverage). Add a BDD scenario that validates the expanded matrix through the
dry-run CLI workflow. These tests will fail against the current implementation,
confirming they correctly test the new behaviour.

Go/no-go: proceed only after new tests fail with assertion errors while all
existing tests pass. Lint, format, and type-check must pass.

Stage B: implement the expanded scenario matrix.

Replace the payload constants in `benchmarks/pipeline_throughput.py` with a
richer set covering three payload sizes (normal and smoke variants). Rewrite
`default_pipeline_scenarios()` to iterate over the Cartesian product of
payload sizes, pipeline depths, and callback modes for each backend. Import
`BackendName` from `benchmarks._benchmark_types` for type annotation.

Go/no-go: proceed only after `make test` passes (all existing + new tests).
Lint, format, and type-check must pass.

Stage C: update documentation.

Add scenario matrix description to `docs/cuprum-design.md` Section 13.9. Add
a "Scenario matrix" subsection to the benchmark suite section of
`docs/users-guide.md`. Mark roadmap item `4.4.2` as done.

Go/no-go: `make markdownlint` and `make nixie` must pass.

Stage D: full validation.

Run all quality gates and verify dry-run JSON output for both smoke and normal
modes.

## Concrete steps

All commands are run from `/home/user/project`.

Stage A:

```plaintext
# After adding tests:
set -o pipefail
uv run pytest -v \
  cuprum/unittests/test_benchmark_suite.py \
  tests/behaviour/test_benchmark_suite_behaviour.py \
  2>&1 | tee /tmp/4-4-2-stage-a.log
```

Expected: 8 new unit tests FAIL, 1 new BDD scenario FAIL, all existing tests
PASS.

```plaintext
set -o pipefail; make check-fmt 2>&1 | tee /tmp/4-4-2-stage-a-fmt.log
set -o pipefail; make lint 2>&1 | tee /tmp/4-4-2-stage-a-lint.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/4-4-2-stage-a-type.log
```

Stage B:

```plaintext
set -o pipefail; make test 2>&1 | tee /tmp/4-4-2-stage-b-test.log
set -o pipefail; make check-fmt 2>&1 | tee /tmp/4-4-2-stage-b-fmt.log
set -o pipefail; make lint 2>&1 | tee /tmp/4-4-2-stage-b-lint.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/4-4-2-stage-b-type.log

# Verify dry-run output:
uv run python benchmarks/pipeline_throughput.py \
  --smoke --dry-run --output /tmp/smoke-plan.json
```

Expected: 12 scenarios with systematic names, payloads
`{1024, 65536, 1048576}`, stages `{2, 3}`, both callback modes.

Stage C:

```plaintext
set -o pipefail; MDLINT=/root/.bun/bin/markdownlint-cli2 \
  make markdownlint 2>&1 | tee /tmp/4-4-2-stage-c-mdlint.log
set -o pipefail; make nixie 2>&1 | tee /tmp/4-4-2-stage-c-nixie.log
```

Stage D:

```plaintext
set -o pipefail; make check-fmt 2>&1 | tee /tmp/4-4-2-check-fmt.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/4-4-2-typecheck.log
set -o pipefail; make lint 2>&1 | tee /tmp/4-4-2-lint.log
set -o pipefail; make test 2>&1 | tee /tmp/4-4-2-test.log
```

## Validation and acceptance

Acceptance is behaviour-based:

- Running `default_pipeline_scenarios(smoke=True, include_rust=False)` returns
  exactly 12 scenarios.
- Running `default_pipeline_scenarios(smoke=False, include_rust=True)` returns
  exactly 24 scenarios.
- Every scenario name matches
  `^(python|rust)-(small|medium|large)-(single|multi)-(nocb|cb)$`.
- Non-smoke payload bytes are `{1024, 1048576, 104857600}`.
- Smoke payload bytes are `{1024, 65536, 1048576}`.
- Stage values are `{2, 3}`.
- Both `True` and `False` appear in `with_line_callbacks`.
- All scenario names are unique.
- Dry-run JSON output reflects the expanded matrix.
- Unit tests validate all matrix properties.
- Behavioural tests validate dry-run matrix structure via CLI.
- Documentation explains the scenario matrix, naming convention, and smoke
  mode behaviour.
- `docs/roadmap.md` item `4.4.2` is checked `[x]`.

Quality criteria:

- `make check-fmt` passes.
- `make typecheck` passes.
- `make lint` passes.
- `make test` passes.

## Idempotence and recovery

All stages are idempotent. Running `make test` is safe to repeat. The dry-run
command overwrites its output file. If implementation produces unexpected test
failures, revert changes to `benchmarks/pipeline_throughput.py` and retry.

## Artifacts and notes

Log files retained for review:

- `/tmp/4-4-2-stage-a.log`
- `/tmp/4-4-2-stage-b-test.log`
- `/tmp/4-4-2-check-fmt.log`
- `/tmp/4-4-2-typecheck.log`
- `/tmp/4-4-2-lint.log`
- `/tmp/4-4-2-test.log`

## Interfaces and dependencies

No new modules or external dependencies. The only interface change is the
return value of `default_pipeline_scenarios()` (more scenarios, same type
signature `tuple[PipelineBenchmarkScenario, ...]`).

New constants in `benchmarks/pipeline_throughput.py`:

```python
_SMALL_PAYLOAD_BYTES: int = 1024
_MEDIUM_PAYLOAD_BYTES: int = 1_048_576
_LARGE_PAYLOAD_BYTES: int = 104_857_600
_SMOKE_SMALL_PAYLOAD_BYTES: int = 1024
_SMOKE_MEDIUM_PAYLOAD_BYTES: int = 65_536
_SMOKE_LARGE_PAYLOAD_BYTES: int = 1_048_576
```

Files modified (7 total, within tolerance):

1. `benchmarks/pipeline_throughput.py` -- expand constants and
   `default_pipeline_scenarios()`.
2. `cuprum/unittests/test_benchmark_suite.py` -- add 8 new unit tests.
3. `tests/behaviour/test_benchmark_suite_behaviour.py` -- add 1 new BDD
   scenario function and 5 new step definitions.
4. `tests/features/benchmark_suite.feature` -- add 1 new Gherkin scenario.
5. `docs/cuprum-design.md` -- add scenario matrix description to Section 13.9.
6. `docs/users-guide.md` -- add "Scenario matrix" subsection.
7. `docs/roadmap.md` -- mark 4.4.2 as done.
