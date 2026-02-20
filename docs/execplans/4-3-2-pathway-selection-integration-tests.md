# Pathway selection integration tests (4.3.3)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: DRAFT

Roadmap reference: `docs/roadmap.md` item `4.3.3`.

## Purpose / big picture

Cuprum already has backend dispatcher unit tests and pipeline backend behavioural
coverage, but the roadmap item `4.3.3` asks for integration tests that prove
pathway selection behaviour end to end, including environment overrides,
forced fallback, and error handling when Rust is explicitly requested but not
available.

After this work, maintainers can run the test suite and see explicit proof that:

- backend environment overrides are respected during pipeline execution;
- forced fallback to the Python pump path is safe and predictable;
- forcing Rust without Rust support raises a clear `ImportError` at execution
  time.

This task is done only when unit tests (`pytest`) and behavioural tests
(`pytest-bdd`) cover these cases, required docs are updated, quality gates pass,
and roadmap item `4.3.3` is marked done.

## Constraints

- Keep pure Python as a first-class pathway. No test may assume Rust is always
  present.
- Follow test-first workflow from `AGENTS.md`: add/modify tests first, confirm
  failure, then implement minimal code or fixture changes to satisfy them.
- Add both unit tests (`cuprum/unittests/`) and behavioural tests
  (`tests/behaviour/` + `tests/features/`).
- Do not change public APIs exposed from `cuprum/__init__.py`.
- Do not add runtime or development dependencies.
- Keep backend cache handling explicit in tests using
  `get_stream_backend.cache_clear()` and `_check_rust_available.cache_clear()`
  where needed to avoid false positives from cached state.
- Keep documentation aligned:
  `docs/cuprum-design.md`, `docs/users-guide.md`, and `docs/roadmap.md`.
- Keep code compatible with repository Python rules in `.rules/python-*.md`
  and existing typing/lint standards.

## Tolerances (exception triggers)

- Scope: if this requires changes to more than 9 files, stop and escalate.
- Production code: if more than 2 production Python files need edits, stop and
  escalate (this item should be primarily test and doc focused).
- Interface: if any public API signature must change, stop and escalate.
- Dependencies: if a new dependency is needed, stop and escalate.
- Iterations: if the same failing test needs more than 3 fix attempts, stop and
  escalate with options.
- Ambiguity: if "forced fallback" semantics are unclear between design doc and
  implementation, stop and request clarification before proceeding.

## Risks

- Risk: Rust availability differs by environment, which can make integration
  assertions flaky.
  Severity: medium.
  Likelihood: medium.
  Mitigation: use monkeypatch-based availability control for deterministic
  scenarios; keep explicit skip markers only where runtime native code is truly
  required.

- Risk: backend caches can hide pathway-selection regressions.
  Severity: high.
  Likelihood: medium.
  Mitigation: clear dispatcher caches in fixtures or per-test setup before each
  scenario.

- Risk: forced fallback scenario may require patching private helpers in
  `cuprum/_pipeline_streams.py`, which can be brittle.
  Severity: medium.
  Likelihood: medium.
  Mitigation: patch only stable, narrow helper boundaries and assert behaviour,
  not implementation details.

## Progress

- [x] (2026-02-20) Draft ExecPlan for roadmap item 4.3.3.
- [ ] Stage A: Confirm acceptance criteria against existing test coverage.
- [ ] Stage B: Add failing behavioural and unit tests for pathway selection.
- [ ] Stage C: Implement minimal fixes (if required) and make tests pass.
- [ ] Stage D: Update design doc and users guide with confirmed behaviour.
- [ ] Stage E: Mark roadmap item 4.3.3 done.
- [ ] Stage F: Run full quality gates and record evidence.

## Surprises & Discoveries

- Observation: `cuprum/unittests/test_backend.py` already covers dispatcher
  logic in isolation, but not pipeline-level selection and fallback execution.
  Evidence: current tests call `get_stream_backend()` directly.
  Impact: this task should focus on integration-level behaviour, not duplicate
  dispatcher unit assertions.

- Observation: `conftest.py` already includes an autouse cache-clearing fixture.
  Evidence: `_clear_backend_cache` clears both caches before each test.
  Impact: new tests should rely on this fixture where possible and only add
  explicit cache clears in self-contained BDD steps.

## Decision Log

- Decision: create dedicated pathway-selection integration coverage at the
  pipeline dispatch layer, not by extending only dispatcher tests.
  Rationale: roadmap item 4.3.3 explicitly targets integration behaviour.
  Date/Author: 2026-02-20 / Codex.

- Decision: include both pytest unit tests and pytest-bdd behavioural scenarios
  for the same feature area.
  Rationale: project policy requires both test styles for new functionality.
  Date/Author: 2026-02-20 / Codex.

- Decision: treat roadmap numbering and filename mismatch as intentional for
  this task (`4.3.3` content in file
  `4-3-2-pathway-selection-integration-tests.md`).
  Rationale: user explicitly requested this filename.
  Date/Author: 2026-02-20 / Codex.

## Outcomes & Retrospective

Not started yet. This section will be completed after implementation with:

- final file list and net scope;
- quality-gate evidence;
- any divergences discovered between design intent and runtime behaviour;
- lessons for remaining roadmap items (`4.3.4+`).

## Context and orientation

Relevant current files and behaviours:

- `cuprum/_backend.py` resolves backend from `CUPRUM_STREAM_BACKEND` and raises
  `ImportError` for forced Rust when unavailable.
- `cuprum/_pipeline_streams.py` dispatches inter-stage pumping through
  `_pump_stream_dispatch`, using Rust when selected and file descriptors are
  extractable, otherwise falling back to Python pump logic.
- `conftest.py` provides `stream_backend` and autouse cache clearing.
- `cuprum/unittests/test_backend.py` validates selection logic at dispatcher
  unit level.
- `tests/behaviour/test_backend_dispatcher_behaviour.py` validates dispatcher
  behaviour via BDD but does not execute pipeline pumping.
- `tests/behaviour/test_stream_backend_pipeline.py` validates baseline pipeline
  output across backend values but does not assert forced fallback path or
  unavailable-Rust failure semantics at pipeline execution boundary.

Gap to close for 4.3.3:

- integration tests that explicitly validate environment overrides, forced
  fallback behaviour, and `ImportError` propagation when Rust is forced but not
  available.

## Plan of work

Stage A: confirm target behaviour and identify exact insertion points.

Review existing dispatcher, pipeline dispatch, and test fixtures to avoid
redundant coverage. Write acceptance assertions before changing any production
code. Go/no-go: proceed only when each required scenario has a concrete test
home and deterministic setup strategy.

Stage B: add failing tests first.

Add behavioural scenarios in a dedicated feature and BDD test module for
pipeline-level pathway selection integration. Add pytest unit tests for
`_pump_stream_dispatch` selection paths where direct control is needed for
forced fallback assertions. Run targeted tests and confirm failures before any
code changes.

Stage C: implement minimal code updates only if tests reveal gaps.

Prefer fixture and test-step adjustments over production edits. If production
changes are required, keep them minimal and local to dispatch integration logic.
Re-run targeted tests until all newly added tests pass.

Stage D: harden documentation and close roadmap item.

Update `docs/cuprum-design.md` with any design decisions clarified by tests,
update `docs/users-guide.md` with consumer-visible pathway-selection behaviour,
and mark roadmap item `4.3.3` as done only after all checks pass.

Each stage ends with validation; do not proceed if the current stage fails.

## Concrete steps

1. Create behavioural integration coverage.

   Files:
   `tests/features/stream_pathway_selection.feature`
   `tests/behaviour/test_stream_pathway_selection_behaviour.py`

   Scenarios to add:
   - environment override to `python` routes pipeline execution through Python
     pathway;
   - forced fallback from requested Rust path when dispatch cannot extract file
     descriptors;
   - forced Rust with unavailable extension raises `ImportError` during pipeline
     execution.

2. Create unit integration coverage for dispatch branch behaviour.

   File:
   `cuprum/unittests/test_pipeline_stream_backend_selection.py`

   Focus assertions:
   - `StreamBackend.PYTHON` path always uses `_pump_stream`;
   - `StreamBackend.RUST` plus missing FDs falls back to `_pump_stream`;
   - `StreamBackend.RUST` plus unavailable extension surfaces `ImportError`
     (no silent fallback in forced-rust mode).

3. Run targeted tests and confirm fail-first behaviour.

       set -o pipefail
       pytest cuprum/unittests/test_pipeline_stream_backend_selection.py -q \
         2>&1 | tee /tmp/4-3-3-targeted-unit-pre.log
       pytest tests/behaviour/test_stream_pathway_selection_behaviour.py -q \
         2>&1 | tee /tmp/4-3-3-targeted-bdd-pre.log

   Expected before implementation:

       FAILED ...

4. Implement minimal fixes (if tests fail for real behavioural gaps).

   Possible edit targets (only if needed):
   `cuprum/_pipeline_streams.py`, `conftest.py`,
   `tests/helpers/catalogue.py`.

5. Re-run targeted tests until green.

       set -o pipefail
       pytest cuprum/unittests/test_pipeline_stream_backend_selection.py -q \
         2>&1 | tee /tmp/4-3-3-targeted-unit-post.log
       pytest tests/behaviour/test_stream_pathway_selection_behaviour.py -q \
         2>&1 | tee /tmp/4-3-3-targeted-bdd-post.log

   Expected after implementation:

       ... passed

6. Update documentation for design decisions and user behaviour.

   Files:
   `docs/cuprum-design.md`
   `docs/users-guide.md`

   Required content:
   - explicit statement of forced fallback semantics at pipeline dispatch;
   - explicit statement that forced `rust` with unavailable extension raises
     `ImportError`.

7. Mark roadmap item complete.

   File:
   `docs/roadmap.md`

   Change:
   - switch `4.3.3` from `[ ]` to `[x]`.

8. Run required quality gates and documentation gates with `tee` logs.

       set -o pipefail
       make check-fmt 2>&1 | tee /tmp/4-3-3-check-fmt.log
       make typecheck 2>&1 | tee /tmp/4-3-3-typecheck.log
       make lint 2>&1 | tee /tmp/4-3-3-lint.log
       make test 2>&1 | tee /tmp/4-3-3-test.log
       make markdownlint 2>&1 | tee /tmp/4-3-3-markdownlint.log
       make nixie 2>&1 | tee /tmp/4-3-3-nixie.log

9. Record concise evidence in this ExecPlan under `Outcomes & Retrospective`.

## Validation and acceptance

Behavioural acceptance:

- Running a pipeline with backend override `python` succeeds and shows expected
  output with successful stage exits.
- For a forced fallback scenario (Rust requested but dispatch cannot use Rust
  path), pipeline execution succeeds via Python pump path.
- For `CUPRUM_STREAM_BACKEND=rust` with Rust unavailable, pipeline execution
  raises `ImportError` with message referencing missing Rust backend.

Quality criteria (what done means):

- Tests: new unit and behavioural tests pass; full `make test` passes.
- Formatting: `make check-fmt` passes.
- Typing: `make typecheck` passes.
- Linting: `make lint` passes.
- Markdown: `make markdownlint` and `make nixie` pass after doc updates.
- Documentation: `docs/cuprum-design.md` and `docs/users-guide.md` describe
  pathway-selection behaviour and failure semantics.
- Roadmap: item `4.3.3` is marked done in `docs/roadmap.md` only after all
  checks above pass.

## Idempotence and recovery

- All test commands are re-runnable and isolated by pytest fixtures.
- If cache-related flakes appear, clear caches at test setup boundaries before
  rerunning.
- If a targeted test fails intermittently, rerun with `-vv -k <test_name>` and
  keep logs in `/tmp/4-3-3-*.log` for diagnosis.
- If a documentation lint fails, run `make fmt` and rerun markdown checks.

## Artifacts and notes

Expected files to create or modify:

- `docs/execplans/4-3-2-pathway-selection-integration-tests.md`
- `tests/features/stream_pathway_selection.feature`
- `tests/behaviour/test_stream_pathway_selection_behaviour.py`
- `cuprum/unittests/test_pipeline_stream_backend_selection.py`
- `docs/cuprum-design.md`
- `docs/users-guide.md`
- `docs/roadmap.md`

Evidence to preserve in logs:

- targeted pre-change failing test runs;
- targeted post-change passing test runs;
- full quality gate run outputs.

## Interfaces and dependencies

Use existing interfaces and modules only:

- `cuprum._backend.StreamBackend`
- `cuprum._backend.get_stream_backend`
- `cuprum._pipeline_streams._pump_stream_dispatch`
- `cuprum._streams._pump_stream`
- existing pytest fixtures in `conftest.py`
- `pytest-bdd` scenario framework already used in `tests/behaviour/`.

Do not introduce new dependencies or new public APIs for this roadmap item.
