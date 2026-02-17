# Behavioural parity tests for stream edge cases (4.3.2)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

## Purpose / big picture

Cuprum offers two stream backends for inter-stage pipeline data transfer: a
pure Python asyncio-based implementation (`cuprum/_streams.py`) and an optional
Rust extension (`cuprum/_streams_rs.py`). Roadmap item 4.3.1 parametrized
existing pipeline tests to run against both backends, proving basic
correctness. However, no tests currently verify that both backends produce
identical output for edge cases: empty streams, partial UTF-8 sequences at
chunk boundaries, broken pipes, and backpressure scenarios.

The existing edge-case coverage in `cuprum/unittests/test_rust_streams.py` only
exercises the Rust backend in isolation at the raw file-descriptor level. The
gap this task fills is pipeline-level parity tests that run the same scenarios
through end-to-end pipeline execution with backend selection via the
`stream_backend` fixture, verifying identical results.

After this change, a developer can:

1. Run `make test` and observe new BDD scenarios (in
   `tests/behaviour/test_stream_parity.py`) and unit tests (in
   `cuprum/unittests/test_stream_parity.py`) that execute identical edge-case
   scenarios through end-to-end pipeline execution under both backends.
2. Confirm that both backends produce identical `PipelineResult.stdout`,
   `PipelineResult.ok`, and per-stage exit codes for every edge case.
3. Read updated documentation in `docs/users-guide.md` describing the
   guaranteed behavioural parity across backends for edge cases.
4. See roadmap item 4.3.2 marked as complete in `docs/roadmap.md`.

## Constraints

Hard invariants that must hold throughout implementation:

- **No changes to production source code.** The files `cuprum/_streams.py`,
  `cuprum/_streams_rs.py`, `cuprum/_backend.py`, and
  `cuprum/_pipeline_streams.py` must not be modified. If a test reveals a
  behavioural divergence between backends, that divergence must be documented
  and escalated, not fixed within this task's scope.
- **Pipeline-level testing.** Parity tests must operate at the pipeline level
  (using `sh.make()`, `Pipeline` composition via `|`, and `pipeline.run_sync()`
  within `scoped(ScopeConfig(allowlist=...))`) rather than at the raw
  file-descriptor level, because the dispatcher (`_pump_stream_dispatch`)
  routes between backends at the pipeline layer.
- **`stream_backend` fixture parametrization.** All parity tests must accept
  the `stream_backend` fixture from `conftest.py` so they automatically run
  against both Python and Rust backends, with Rust skipped when the extension
  is unavailable.
- **Python 3.12+ compatibility.** Use `match` statements, `StrEnum`, type
  union syntax (`X | Y`), and other 3.12+ constructs as appropriate.
- **Strict linting.** All code must satisfy the Ruff rule set in
  `pyproject.toml` (line length 88, max complexity 8, max args 4, max locals
  10), Pyright strict mode (via `ty`), and `make check-fmt`.
- **NumPy docstrings.** All functions, including test helpers, require
  NumPy-style docstrings per project convention.
- **Test conventions.** Unit tests in `cuprum/unittests/`, BDD steps in
  `tests/behaviour/`, feature files in `tests/features/`.
- **No deadlocks.** Tests involving large payloads or broken pipes must
  complete within 30 seconds (enforced by `pytest-timeout`).
- **No new runtime or dev dependencies.**
- **British English** in documentation per `docs/documentation-style-guide.md`.

## Tolerances (exception triggers)

- **Scope**: If implementation requires changes to more than 8 files, stop
  and escalate.
- **Interface**: If any existing public API signature must change, stop and
  escalate.
- **Dependencies**: No new runtime or dev dependencies allowed.
- **Iterations**: If tests still fail after 3 fix attempts per failure, stop
  and escalate.
- **Behavioural divergence**: If any edge case produces materially different
  output between the Python and Rust backends, document the divergence in
  `Decision Log` and escalate. Do not modify production code to fix it.

## Risks

- **Risk**: Partial UTF-8 sequences at chunk boundaries may not be directly
  controllable at the pipeline level because the framework controls chunk sizes
  internally (`_READ_SIZE = 4096` in `_streams.py`, default `65536` for Rust).
  - Severity: medium
  - Likelihood: medium
  - Mitigation: Use payloads significantly larger than `_READ_SIZE` containing
    many multi-byte characters so splits are statistically guaranteed. A
    secondary mitigation is dedicated unit tests for each UTF-8 byte width.

- **Risk**: Broken pipe tests may produce non-deterministic behaviour because
  timing determines how much data the upstream has written before the
  downstream exits.
  - Severity: medium
  - Likelihood: low
  - Mitigation: Assert on properties that must be invariant (pipeline
    completes without deadlock, no unhandled exceptions) rather than on exact
    byte counts. The key parity assertion is that both backends exhibit the
    same high-level behaviour.

- **Risk**: Backpressure tests with large payloads may be slow or flaky.
  - Severity: low
  - Likelihood: low
  - Mitigation: Keep payloads to 1 MB, matching the pattern in
    `test_rust_streams_behaviour.py`.

## Progress

- [x] (2026-02-17) Stage A: Write ExecPlan document
- [x] (2026-02-17) Stage B: Create BDD feature file
- [x] (2026-02-17) Stage C: Create parity test helpers
- [x] (2026-02-17) Stage D: Implement BDD step definitions
- [x] (2026-02-17) Stage E: Implement unit tests
- [x] (2026-02-17) Stage F: Update documentation
- [x] (2026-02-17) Stage G: Run full quality gate suite and verify

## Surprises & discoveries

- Observation: Embedding a 1 MB payload directly in a `python -c "print(...)"`
  command line exceeds the Linux `MAX_ARG_STRLEN` limit (131072 bytes per
  argument), causing `OSError: [Errno 36] File name too long`. Evidence:
  `make test` failed with this error for both backpressure tests. Impact:
  Changed approach to generate large payloads inside the subprocess
  (`sys.stdout.write('x' * 1048576)`) rather than passing them as command-line
  arguments. UTF-8 payloads (under 21 KB) were unaffected.

- Observation: pytest requires unique basenames for test modules across
  the entire project. Having both `cuprum/unittests/test_stream_parity.py` and
  `tests/behaviour/test_stream_parity.py` caused an import collision error.
  Evidence: `make test` reported import file mismatch. Impact: Renamed the BDD
  test file to `test_stream_parity_behaviour.py` following the existing naming
  convention (`test_*_behaviour.py`).

## Decision log

- **Decision**: Test at the pipeline level, not at the raw FD level
  - Rationale: The `stream_backend` fixture controls the
    `CUPRUM_STREAM_BACKEND` env var, which routes through
    `_pump_stream_dispatch` in `cuprum/_pipeline_streams.py`. Testing at the
    pipeline level exercises the actual dispatch path. Raw FD tests already
    exist in `test_rust_streams.py` for the Rust backend. The parity claim is
    about end-to-end pipeline behaviour.
  - Date: 2026-02-17

- **Decision**: Use Python subprocess scripts (`python -c "..."`) as pipeline
  stages for controlled byte output
  - Rationale: Python scripts give precise control over what bytes are written
    to stdout, including empty output, raw bytes, partial writes, and early
    exits. The existing test patterns in `test_stream_backend_pipeline.py` and
    `test_stream_fidelity.py` already use this approach via
    `python_catalogue()`.
  - Date: 2026-02-17

- **Decision**: "Identical output" is defined as identical values for
  `PipelineResult.stdout`, `PipelineResult.ok`, and the pattern of per-stage
  exit codes
  - Rationale: `PipelineResult.stdout` is the captured output of the final
    stage. `ok` indicates overall success. Per-stage exit codes verify that
    failure propagation is consistent. PIDs and timing are inherently
    non-deterministic and cannot be compared.
  - Date: 2026-02-17

- **Decision**: Separate BDD scenarios from granular unit tests
  - Rationale: BDD scenarios verify the four edge-case categories at the
    pipeline level from a user perspective. Unit tests provide more granular
    coverage with controlled payloads and assertions. This follows the
    established `AGENTS.md` pattern requiring both behavioural and unit tests
    for new functionality.
  - Date: 2026-02-17

- **Decision**: UTF-8 stress payload uses a deterministic generator with
  fixed seed cycling through 1, 2, 3, and 4-byte characters
  - Rationale: Reproducible across runs. Encoded size exceeds 32 KB to
    guarantee chunk boundary splits in both the Python path (4096-byte reads)
    and Rust path (65536-byte reads).
  - Date: 2026-02-17

## Outcomes & retrospective

Implementation completed successfully:

- Created 4 BDD scenarios verifying parity for empty streams, multi-byte
  UTF-8, broken pipes, and backpressure (8 test runs: 4 Python + 4 Rust, with
  Rust skipped when extension unavailable)
- Created 8 unit tests with finer granularity (16 test runs: 8 Python +
  8 Rust, with Rust skipped when extension unavailable)
- Created shared parity test helpers including a deterministic UTF-8 stress
  payload generator cycling through 1, 2, 3, and 4-byte characters
- Updated `docs/users-guide.md` with parity guarantee documentation
- Marked roadmap item 4.3.2 as complete

All quality gates passed:

- `make check-fmt`: 69 files formatted
- `make typecheck`: All checks passed
- `make lint`: All checks passed
- `make test`: 294 passed, 38 skipped
- `make markdownlint`: 0 errors

Files created (4):

1. `tests/features/stream_parity.feature` -- BDD feature file
2. `tests/behaviour/test_stream_parity_behaviour.py` -- BDD steps
3. `tests/helpers/parity.py` -- Shared parity test helpers
4. `cuprum/unittests/test_stream_parity.py` -- Unit tests

Files modified (3):

1. `docs/roadmap.md` -- Marked 4.3.2 complete
2. `docs/users-guide.md` -- Added parity guarantee paragraph
3. `docs/execplans/4-3-2-behavioural parity tests.md` -- This document

Lessons learned:

- Large subprocess payloads must be generated inside the subprocess rather
  than passed as command-line arguments to avoid Linux `MAX_ARG_STRLEN` limits.
  The threshold is approximately 128 KB per argument.
- pytest test module basenames must be unique across the entire project.
  Following the `test_*_behaviour.py` convention for BDD tests avoids
  collisions with `cuprum/unittests/test_*.py` files.

## Context and orientation

### Project structure

Cuprum is a typed, async command-execution library for Python 3.12+ with
optional Rust extensions for stream performance. Key directories:

- `cuprum/` -- Python package (source)
- `cuprum/unittests/` -- Unit tests (pytest)
- `tests/behaviour/` -- BDD step implementations (pytest-bdd)
- `tests/features/` -- Gherkin feature files
- `tests/helpers/` -- Shared test utilities
- `docs/` -- Design docs, users guide, roadmap, execplans

### Stream architecture

Two independent stream implementations exist:

1. **Python** (`cuprum/_streams.py`): `_pump_stream(reader, writer)` uses
   asyncio `StreamReader`/`StreamWriter` with backpressure via `drain()`.
   `_consume_stream(stream, config)` decodes with optional line callbacks. Read
   chunk size is `_READ_SIZE = 4096`.

2. **Rust** (`cuprum/_streams_rs.py`):
   `rust_pump_stream(reader_fd, writer_fd, buffer_size=65536)` operates on raw
   file descriptors, releases the GIL.
   `rust_consume_stream(reader_fd, buffer_size=65536)` reads and decodes UTF-8
   with replacement semantics.

### Dispatch mechanism

`cuprum/_pipeline_streams.py` contains `_pump_stream_dispatch(reader, writer)`
which checks `get_stream_backend()`, and if `RUST` and FDs are extractable,
runs `rust_pump_stream` via `loop.run_in_executor()`. Otherwise, it delegates
to `_pump_stream(reader, writer)`.

The `stream_backend` fixture in `conftest.py:50-88` parametrizes tests with
`python` and `rust` backend values, setting `CUPRUM_STREAM_BACKEND` via
`monkeypatch.setenv`. The `_clear_backend_cache` autouse fixture at line 91
clears LRU caches between tests.

### Existing test patterns

Pipeline tests follow a consistent pattern:

1. Build a catalogue using `python_catalogue()`, `cat_program()`, and
   `combine_programs_into_catalogue()` from `tests/helpers/catalogue.py`.
2. Create builders via `sh.make(program, catalogue=catalogue)`.
3. Compose pipelines via `builder(...) | builder(...)`.
4. Execute within `scoped(ScopeConfig(allowlist=...))` via
   `pipeline.run_sync()`.
5. Assert on `PipelineResult` attributes.

### Quality gates

All four must pass before committing:

    make check-fmt   # ruff format --check
    make typecheck   # pyright via ty
    make lint        # ruff check
    make test        # pytest -v -n auto

## Plan of work

### Stage A: Write ExecPlan document

Write this document to `docs/execplans/4-3-2-behavioural parity tests.md`.

### Stage B: Create BDD feature file

Create `tests/features/stream_parity.feature` with four scenarios:

1. Empty stream produces identical output across backends
2. Multi-byte UTF-8 survives pipeline across backends
3. Broken pipe is handled gracefully across backends
4. Large payload survives backpressure across backends

### Stage C: Create parity test helpers

Create `tests/helpers/parity.py` with shared infrastructure:

- `parity_catalogue()` -- builds catalogue with Python + cat + echo
- `run_parity_pipeline(pipeline, allowlist)` -- runs within scoped config
- `utf8_stress_payload(n_chars=8192)` -- deterministic multi-byte UTF-8
  string using fixed seed `random.Random(20260217)`, cycling through 1, 2, 3,
  and 4-byte characters, encoded size exceeding 32 KB

### Stage D: Implement BDD step definitions

Create `tests/behaviour/test_stream_parity.py` with `@scenario` decorators and
`@given/@when/@then` step implementations. Each test function accepts the
`stream_backend` fixture. Edge case implementations:

- **Empty**: `python -c "pass"` | `cat`
- **UTF-8**: Python script writing `utf8_stress_payload()` | `cat`
- **Broken pipe**: large producer | `python -c "read 10 bytes and exit"`
- **Backpressure**: 1 MB producer | `cat` | `cat`

### Stage E: Implement unit tests

Create `cuprum/unittests/test_stream_parity.py` with 8 parametrized tests
covering each edge case at finer granularity.

### Stage F: Update documentation

- `docs/roadmap.md` line 152: `[ ]` to `[x]`
- `docs/users-guide.md`: add parity guarantee paragraph

### Stage G: Quality verification

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/check-fmt.log
    make typecheck 2>&1 | tee /tmp/typecheck.log
    make lint 2>&1 | tee /tmp/lint.log
    make test 2>&1 | tee /tmp/test.log

## Concrete steps

All commands run from `/home/user/project`.

**Step B1: Create the feature file.** Write
`tests/features/stream_parity.feature`.

**Step C1: Create parity helpers.** Write `tests/helpers/parity.py`.

**Step D1: Create BDD steps.** Write `tests/behaviour/test_stream_parity.py`.

**Step E1: Create unit tests.** Write `cuprum/unittests/test_stream_parity.py`.

**Step F1: Update roadmap.** Mark 4.3.2 as `[x]`.

**Step F2: Update users guide.** Add parity guarantee paragraph.

**Step G1: Full verification.**

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/check-fmt.log
    make typecheck 2>&1 | tee /tmp/typecheck.log
    make lint 2>&1 | tee /tmp/lint.log
    make test 2>&1 | tee /tmp/test.log

## Validation and acceptance

**Quality criteria (what "done" means):**

- Tests: All existing tests pass. New BDD scenarios pass under both backends
  (Rust skipped when unavailable). New unit tests pass under both backends.
- Lint/typecheck: `make lint` and `make typecheck` report no errors.
- Format: `make check-fmt` reports no differences.
- Documentation: Roadmap item 4.3.2 is marked done. Users guide describes
  parity guarantees.

**Behavioural acceptance:**

- Empty stream pipeline produces `stdout == ""` and `ok is True` under both
  backends.
- UTF-8 pipeline with multi-byte characters produces byte-identical stdout
  under both backends.
- Broken pipe pipeline completes without deadlock under both backends.
- Backpressure pipeline with 1 MB payload produces byte-identical stdout
  under both backends.
- No existing test is broken.

## Idempotence and recovery

All steps can be repeated safely. Writing files is idempotent. Tests can be
re-run at any time. Cache clearing in `_clear_backend_cache` autouse fixture
prevents cross-test pollution.

## Artifacts and notes

### Files to create (4)

1. `tests/features/stream_parity.feature` -- BDD feature file
2. `tests/behaviour/test_stream_parity.py` -- BDD step implementations
3. `tests/helpers/parity.py` -- Shared parity test helpers
4. `cuprum/unittests/test_stream_parity.py` -- Unit tests

### Files to modify (2)

1. `docs/roadmap.md` -- Mark 4.3.2 as complete
2. `docs/users-guide.md` -- Add parity guarantee documentation

### Files that must NOT be modified

- `cuprum/_streams.py`
- `cuprum/_streams_rs.py`
- `cuprum/_backend.py`
- `cuprum/_pipeline_streams.py`
- `cuprum/_rust_backend.py`

### Existing patterns reused

- `stream_backend` fixture from `conftest.py:50-88`
- `_clear_backend_cache` autouse fixture from `conftest.py:91-99`
- `python_catalogue()` from `tests/helpers/catalogue.py:17-26`
- `cat_program()` from `tests/helpers/catalogue.py:35-37`
- `combine_programs_into_catalogue()` from `tests/helpers/catalogue.py:40-68`
- BDD scenario pattern from
  `tests/behaviour/test_stream_backend_pipeline.py`

## Interfaces and dependencies

### New file: `tests/helpers/parity.py`

    def parity_catalogue() -> tuple[
        ProgramCatalogue, Program, Program, Program
    ]:
        """Build a catalogue for parity tests."""

    def run_parity_pipeline(
        pipeline: Pipeline,
        allowlist: frozenset[Program],
    ) -> PipelineResult:
        """Execute a pipeline within a scoped allowlist."""

    def utf8_stress_payload(n_chars: int = 8192) -> str:
        """Generate a deterministic multi-byte UTF-8 stress payload."""

### Dependencies

- `pytest` and `pytest-bdd` (existing dev dependencies)
- `cuprum` public API: `ECHO`, `ScopeConfig`, `scoped`, `sh`
- `tests.helpers.catalogue`: `python_catalogue`, `cat_program`,
  `combine_programs_into_catalogue`
- No new external dependencies
