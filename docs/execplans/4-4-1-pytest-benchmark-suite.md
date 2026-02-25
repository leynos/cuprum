# Create benchmark suite for stream performance (4.4.1)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: COMPLETE

Roadmap reference: `docs/roadmap.md` item `4.4.1`.

`PLANS.md` is not present in this repository, so this plan follows the
`execplans` skill template and repository instructions in `AGENTS.md`.

## Purpose / big picture

Roadmap item `4.4.1` requires a benchmark suite that measures both:

1. Microbenchmarks with `pytest-benchmark` (pump latency and consume
   throughput).
2. End-to-end pipeline throughput with `hyperfine`.

After this work, maintainers can run a repeatable benchmark workflow and obtain
JSON artefacts that compare Python and Rust pathways on the same workload
shapes. This work must preserve pure Python as a first-class pathway, keep
benchmark execution opt-in (so `make test` stays practical), and document the
workflow for contributors.

This task is complete only when:

- benchmark harness code exists and runs;
- unit tests (`pytest`) and behavioural tests (`pytest-bdd`) cover the new
  benchmark feature surface;
- `docs/cuprum-design.md` and `docs/users-guide.md` are updated with the final
  benchmark contract;
- `docs/roadmap.md` marks `4.4.1` as done;
- quality gates pass: `make check-fmt`, `make typecheck`, `make lint`,
  and `make test`.

## Constraints

- Keep scope bounded to roadmap item `4.4.1`. Do not implement CI workflow
  gating or report publication from `4.4.3` and `4.4.4` in this task.
- Keep pure Python and Rust pathways both benchmarkable. No benchmark may
  assume Rust is installed.
- Follow repository test-first policy:
  - add or modify tests first;
  - capture failing state;
  - implement minimal code to pass.
- Provide both test styles for this feature:
  - unit tests using `pytest` in `cuprum/unittests/`;
  - behavioural tests using `pytest-bdd` in `tests/behaviour/` with Gherkin
    in `tests/features/`.
- Keep benchmark-heavy runs opt-in so default `make test` remains viable.
- Do not change public runtime APIs exported from `cuprum/__init__.py`.
- Do not introduce runtime dependencies; dev-only dependency additions are
  allowed for benchmark tooling.
- Keep docs aligned:
  `docs/cuprum-design.md`, `docs/users-guide.md`, and `docs/roadmap.md`.
- Keep Python changes compliant with `.rules/python-*.md`, Ruff,
  type-checking, and project scripting standards.

## Tolerances (exception triggers)

- Scope: if implementation requires edits to more than 12 files, stop and
  escalate.
- Interface: if a public API signature must change, stop and escalate.
- Dependencies: if a dependency beyond `pytest-benchmark` is required,
  stop and escalate.
- Iterations: if the same failing test needs more than 3 fix attempts, stop
  and escalate with options.
- Runtime: if benchmark smoke tests make `make test` materially slower
  (target: less than 10% increase), stop and propose an isolation strategy.
- Ambiguity: if roadmap language for pump latency or consume throughput is too
  ambiguous to encode deterministic scenarios, stop and resolve definitions
  before proceeding.

## Risks

- Risk: Benchmark tests can become flaky on shared CI hardware.
  Severity: medium. Likelihood: high. Mitigation: separate deterministic smoke
  assertions from performance measurement, use bounded inputs for tests, and
  keep real benchmarks opt-in.

- Risk: `hyperfine` command composition may drift across platforms.
  Severity: medium. Likelihood: medium. Mitigation: centralise command
  construction in one Python module, and test command generation directly.

- Risk: Benchmarking only one pathway would violate the design requirement that
  both pathways are first-class. Severity: high. Likelihood: medium.
  Mitigation: require scenario matrix entries for both `python` and `rust`
  (with Rust skip/fallback logic when unavailable).

- Risk: Large payload runs can be too slow for developer loops.
  Severity: medium. Likelihood: high. Mitigation: put large runs behind
  explicit benchmark commands and use smaller smoke payloads in
  unit/behavioural tests.

## Progress

- [x] (2026-02-25) Drafted ExecPlan for roadmap item `4.4.1`.
- [x] (2026-02-25) Stage A: added fail-first unit and behavioural tests and
  captured the expected import error before implementation.
- [x] (2026-02-25) Stage B: implemented `pytest-benchmark` microbenchmark suite
  and `make benchmark-micro`.
- [x] (2026-02-25) Stage C: implemented `hyperfine` throughput runner, worker,
  and `make benchmark-e2e`.
- [x] (2026-02-25) Stage D: updated `docs/cuprum-design.md`,
  `docs/users-guide.md`, and marked roadmap item `4.4.1` done.
- [x] (2026-02-25) Stage E: completed full quality gates and Markdown/diagram
  checks.

## Surprises & discoveries

- Observation: the roadmap and design/ADR documents already require benchmark
  CI semantics, but there is no benchmark suite or benchmark command in the
  repository today. Evidence:
  `rg -n "pytest-benchmark|hyperfine|benchmark" ...` finds roadmap and design
  mentions only, with no implementation modules or tests. Impact: this task
  must introduce both tooling hooks and docs, not just add one test file.

- Observation: current stream behaviour docs already note that stream
  consumption uses Python while Rust currently accelerates inter-stage pumping.
  Evidence: `docs/users-guide.md` performance extension section. Impact:
  consume throughput benchmarks must document exactly what is being measured
  and avoid implying Rust consume-path dispatch that does not exist.

- Observation: Ruff/typing rules in this repository are strict for benchmark
  files too (`S404`, `PLR0913`, `ANN401`, and import-convention rules).
  Evidence: first `make lint` run failed on new benchmark modules. Impact:
  benchmark modules were refactored/annotated to meet production lint standards
  rather than relying on broad ignores.

## Decision log

- Decision: keep this implementation limited to benchmark suite creation and
  local benchmark runner support; defer CI failure thresholds and report
  publication to roadmap items `4.4.3` and `4.4.4`. Rationale: preserves
  roadmap atomicity and keeps this change reviewable. Date/Author: 2026-02-25 /
  Codex.

- Decision: make benchmark execution explicit (dedicated commands/files) rather
  than running heavyweight benchmark loops in default `make test`. Rationale:
  preserves normal developer feedback speed while still shipping a reproducible
  benchmark suite. Date/Author: 2026-02-25 / Codex.

- Decision: gate benchmark pytest module execution behind
  `CUPRUM_RUN_BENCHMARKS=1`. Rationale: keeps normal `make test` deterministic
  and fast while still allowing benchmark collection in dedicated targets.
  Date/Author: 2026-02-25 / Codex.

- Decision: add a `--dry-run` mode to the `hyperfine` runner that writes plan
  JSON without executing benchmarks. Rationale: enables fast behavioural tests
  and predictable CI smoke checks without hardware-dependent timing variance.
  Date/Author: 2026-02-25 / Codex.

## Outcomes & retrospective

Completed implementation for roadmap item `4.4.1`.

Delivered:

- `pytest-benchmark` microbenchmarks for pump latency and consume throughput in
  `benchmarks/test_stream_microbenchmarks.py`.
- `hyperfine` end-to-end throughput runner and worker:
  `benchmarks/pipeline_throughput.py` and `benchmarks/pipeline_worker.py`.
- Benchmark orchestration unit and behavioural tests:
  `cuprum/unittests/test_benchmark_suite.py`,
  `tests/features/benchmark_suite.feature`,
  `tests/behaviour/test_benchmark_suite_behaviour.py`.
- Make targets `benchmark-micro` and `benchmark-e2e`.
- Dev dependency update for `pytest-benchmark`.
- Documentation updates in design and users guide, and roadmap item `4.4.1`
  marked done.

Quality and validation summary:

- Red phase: targeted tests failed with `ModuleNotFoundError: benchmarks`.
- Green phase: targeted benchmark-suite tests passed (`6 passed`).
- Microbench command passed and emitted JSON artefact.
- Throughput runner passed in dry-run and real smoke modes, emitting JSON
  artefacts.
- Final gates passed:
  - `make check-fmt`
  - `make typecheck`
  - `make lint`
  - `make test`
  - `make markdownlint`
  - `make nixie`

## Context and orientation

Relevant repository areas:

- Stream implementation and backend selection:
  - `cuprum/_streams.py`
  - `cuprum/_streams_rs.py`
  - `cuprum/_pipeline_streams.py`
  - `cuprum/_backend.py`
- Existing backend parity tests:
  - `cuprum/unittests/test_stream_parity.py`
  - `tests/behaviour/test_stream_parity_behaviour.py`
  - `tests/features/stream_parity.feature`
- Existing property stress helpers that can be reused for payload generation:
  - `tests/helpers/parity.py`
- Existing quality gates and commands:
  - `Makefile` targets `check-fmt`, `typecheck`, `lint`, and `test`.
- Benchmark requirements source:
  - `docs/roadmap.md` section `4.4.1`
  - `docs/cuprum-design.md` section `13.9`
  - `docs/adr-001-rust-extension.md` benchmarking section.

Gap summary:

- Closed:
  - `pytest-benchmark` is configured in dev dependencies.
  - benchmark pytest module and hyperfine runner are implemented.
  - unit and behavioural benchmark workflow tests exist.

## Plan of work

Stage A: define benchmark workflow and add tests first.

Add fail-first unit tests for benchmark scenario generation and command
construction, plus behavioural scenarios that validate user-visible benchmark
invocation and artefact emission. Keep these tests lightweight and
deterministic so they run under `make test`.

Go/no-go: proceed only after new tests fail with missing benchmark modules or
functions.

Stage B: implement microbenchmark suite using `pytest-benchmark`.

Add benchmark module(s) that exercise pump latency and consume throughput for
both backend modes, with explicit payload and iteration controls. Keep this
suite opt-in and emit JSON artefacts on demand.

Go/no-go: proceed only after targeted benchmark commands run and emit expected
JSON output.

Stage C: implement `hyperfine` end-to-end throughput harness.

Add a small script that constructs and runs pipeline throughput benchmarks
using `hyperfine`, writing structured JSON results per scenario matrix.
Validate script behaviour via unit tests and behavioural smoke scenarios.

Go/no-go: proceed only when both Python and Rust command variants are produced
correctly (Rust path may skip when unavailable).

Stage D: documentation and roadmap completion.

Update design and user docs with benchmark methodology, how to run the suite,
artefact locations, and interpretation caveats. Mark roadmap item `4.4.1` done
only after verification and quality gates pass.

Stage E: full validation.

Run required quality gates and retain log evidence.

## Concrete steps

1. Add fail-first tests and feature coverage.

   Planned files:

   - `cuprum/unittests/test_benchmark_suite.py`
   - `tests/features/benchmark_suite.feature`
   - `tests/behaviour/test_benchmark_suite_behaviour.py`

   Unit-test focus:

   - benchmark scenario matrix generation (payload sizes and backend modes);
   - `hyperfine` command construction and artefact path handling;
   - skip/fallback handling when Rust is unavailable.

   Behavioural-test focus:

   - user can run benchmark workflow in smoke mode;
   - JSON artefacts are created in expected paths;
   - command reports indicate which backend(s) executed.

2. Capture initial failing state (red).

```plaintext
set -o pipefail
UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
  cuprum/unittests/test_benchmark_suite.py \
  tests/behaviour/test_benchmark_suite_behaviour.py \
  2>&1 | tee /tmp/4-4-1-targeted-pre.log
```

Expected result before implementation: failures due to missing benchmark suite
modules/functions.

1. Implement benchmark suite and runner.

   Planned files:

   - `benchmarks/test_stream_microbenchmarks.py` (uses `pytest-benchmark`)
   - `benchmarks/pipeline_throughput.py` (`hyperfine` orchestration CLI)
   - `pyproject.toml` (add dev dependency `pytest-benchmark`)
   - `Makefile` (add explicit benchmark target(s), not part of default `test`)

2. Re-run targeted tests and benchmark smoke commands (green).

```plaintext
set -o pipefail
UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
  cuprum/unittests/test_benchmark_suite.py \
  tests/behaviour/test_benchmark_suite_behaviour.py \
  2>&1 | tee /tmp/4-4-1-targeted-post.log

set -o pipefail
UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
  benchmarks/test_stream_microbenchmarks.py \
  --benchmark-json=/tmp/4-4-1-microbench.json \
  2>&1 | tee /tmp/4-4-1-microbench.log

set -o pipefail
UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run python \
  benchmarks/pipeline_throughput.py \
  --smoke \
  --output /tmp/4-4-1-hyperfine.json \
  2>&1 | tee /tmp/4-4-1-hyperfine.log
```

Expected evidence:

- targeted tests pass;
- `/tmp/4-4-1-microbench.json` exists and contains benchmark entries;
- `/tmp/4-4-1-hyperfine.json` exists and contains `hyperfine` results.

1. Update documentation and roadmap completion state.

   Planned files:

   - `docs/cuprum-design.md` (benchmark methodology and boundaries)
   - `docs/users-guide.md` (how consumers run/interpret benchmarks)
   - `docs/roadmap.md` (mark `4.4.1` as done)

2. Run full quality gates and capture logs.

```plaintext
set -o pipefail
make check-fmt 2>&1 | tee /tmp/4-4-1-check-fmt.log

set -o pipefail
make typecheck 2>&1 | tee /tmp/4-4-1-typecheck.log

set -o pipefail
make lint 2>&1 | tee /tmp/4-4-1-lint.log

set -o pipefail
make test 2>&1 | tee /tmp/4-4-1-test.log
```

## Validation and acceptance

Acceptance is behaviour-based:

- Running the benchmark micro-suite produces reproducible benchmark JSON with
  pump latency and consume throughput metrics.
- Running the end-to-end benchmark runner executes `hyperfine` scenarios and
  emits JSON throughput results.
- Unit tests validate benchmark matrix and runner command semantics.
- Behavioural tests validate user-visible benchmark workflow behaviour.
- Documentation explains:
  - what is measured;
  - how to run the suite;
  - how to interpret results;
  - how Rust unavailability affects runs.
- `docs/roadmap.md` item `4.4.1` is checked `[x]`.
- Quality criteria:
  - `make check-fmt` passes;
  - `make typecheck` passes;
  - `make lint` passes;
  - `make test` passes.

## Idempotence and recovery

- All benchmark commands are re-runnable and overwrite artefacts at fixed
  output paths.
- If a benchmark step fails, rerun only that step after fixing the issue; no
  destructive cleanup is required.
- Recovery procedure for stale artefacts:

```plaintext
rm -f /tmp/4-4-1-*.log /tmp/4-4-1-*.json
```

- If dependency resolution changes `uv.lock`, rerun `make build` and repeat the
  failing step.

## Artifacts and notes

Implementation should retain these logs for review:

- `/tmp/4-4-1-targeted-pre.log`
- `/tmp/4-4-1-targeted-post.log`
- `/tmp/4-4-1-microbench.log`
- `/tmp/4-4-1-hyperfine.log`
- `/tmp/4-4-1-check-fmt.log`
- `/tmp/4-4-1-typecheck.log`
- `/tmp/4-4-1-lint.log`
- `/tmp/4-4-1-test.log`

And these benchmark outputs:

- `/tmp/4-4-1-microbench.json`
- `/tmp/4-4-1-hyperfine.json`

## Interfaces and dependencies

Prescriptive interfaces for this milestone:

- Dev dependency:
  - `pytest-benchmark` in `[dependency-groups].dev` in `pyproject.toml`.
- Hyperfine runner script:
  - `benchmarks/pipeline_throughput.py`
  - accepts `--smoke`, `--output`, and `--dry-run`.
- Throughput worker script:
  - `benchmarks/pipeline_worker.py`
  - executes a configurable multi-stage pipeline for one scenario.
- Microbenchmark module:
  - `benchmarks/test_stream_microbenchmarks.py`
  - uses `pytest-benchmark` fixture for pump latency and consume throughput
    cases.
- Make targets:
  - `make benchmark-micro`
  - `make benchmark-e2e`
  - both opt-in and separate from `make test`.

## Revision note

Updated from draft to complete implementation state. Changes:

- recorded completed stages, design decisions, outcomes, and validation
  evidence;
- updated interface section to reflect final shipped files and commands.
Impact on remaining work:
- roadmap item `4.4.1` is complete; follow-on CI gating/reporting items remain
  under `4.4.3` and `4.4.4`.
