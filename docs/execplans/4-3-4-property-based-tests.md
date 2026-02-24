# Add hypothesis property-based stream preservation tests (4.3.4)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

Roadmap reference: `docs/roadmap.md` item `4.3.4`.

`PLANS.md` is not present in this repository, so this plan follows the
`execplans` skill template and repository instructions in `AGENTS.md`.

## Purpose / big picture

Roadmap item `4.3.4` requires property-based coverage for stream content
preservation across random payloads and chunk boundaries. The value is stronger
assurance that both the Python and optional Rust pump pathways preserve byte
content under many randomly generated input patterns, not only hand-picked
fixtures.

After this work, maintainers can run tests and observe that:

- Hypothesis-driven unit tests generate random payloads and random chunk
  boundaries, then verify output preservation through real pipeline execution
  under both `python` and `rust` backends.
- Behavioural tests (`pytest-bdd`) demonstrate consumer-visible parity for
  deterministic random scenarios at chunk-boundary stress points.
- Documentation explains the property tested, and the roadmap item is marked
  done only after all quality gates pass.

## Constraints

Hard invariants that must hold throughout implementation:

- Keep pure Python as a first-class pathway. New tests must pass with
  `CUPRUM_STREAM_BACKEND=python`, and Rust variants must skip cleanly when the
  extension is unavailable.
- Follow test-first workflow:
  - add or modify tests first;
  - run targeted tests and capture an initial failure;
  - then implement supporting code changes.
- Provide both test types:
  - unit tests using `pytest` in `cuprum/unittests/`;
  - behavioural tests using `pytest-bdd` in `tests/behaviour/` and
    `tests/features/`.
- Limit production-code changes unless tests reveal a real bug. This task is
  expected to be test- and documentation-heavy.
- Keep public APIs stable (`cuprum/__init__.py` exports and user-facing
  command/pipeline interfaces unchanged).
- Do not add runtime dependencies. A dev dependency on `hypothesis` is allowed
  because this roadmap item explicitly requires it.
- Keep docs in sync:
  `docs/cuprum-design.md`, `docs/users-guide.md`, and `docs/roadmap.md`.
- Keep all Python changes compliant with `.rules/python-*.md`, Ruff, and
  type-checking requirements.

## Tolerances (exception triggers)

- Scope: if implementation requires changes to more than 10 files, stop and
  escalate.
- Production code: if more than 2 production Python files need changes, stop
  and escalate before proceeding.
- Dependencies: if any dependency beyond `hypothesis` is required, stop and
  escalate.
- Interface: if any public API signature must change, stop and escalate.
- Iterations: if the same failing test needs more than 3 fix attempts, stop
  and escalate with options.
- Ambiguity: if chunk-boundary expectations are unclear (byte-preservation vs
  decoded-text preservation), stop and resolve the expected invariant before
  continuing.

## Risks

- Risk: Property-based tests can be flaky or slow if strategies are too broad.
  Severity: medium Likelihood: medium Mitigation: bound payload sizes and
  hypothesis settings (`max_examples`, health checks, and deadline controls) to
  keep runtime deterministic.

- Risk: Command-line argument length limits may be exceeded if random payloads
  are embedded directly in Python `-c` strings. Severity: medium Likelihood:
  medium Mitigation: encode payloads safely (for example, base64) and cap
  payload size; where needed, generate large data inside subprocess code.

- Risk: Random chunk boundaries might not actually cross meaningful boundary
  points when generated naively. Severity: medium Likelihood: medium
  Mitigation: derive chunk-size partitions from generated cut points so every
  example intentionally splits payload bytes in varied positions.

## Progress

- [x] (2026-02-23 12:51Z) Drafted ExecPlan for roadmap item `4.3.4`.
- [x] (2026-02-23 16:50Z) Stage A: Confirmed invariant and insertion points.
- [x] (2026-02-23 16:50Z) Stage B: Added fail-first unit and behavioural tests
  and captured expected `ModuleNotFoundError` for Hypothesis.
- [x] (2026-02-23 16:50Z) Stage C: Added `hypothesis` dev dependency, lockfile
  update via `make build`, and helper support code.
- [x] (2026-02-23 16:50Z) Stage D: Updated design docs, users guide, and
  roadmap entry `4.3.4`.
- [x] (2026-02-23 16:52Z) Stage E: Full quality gates passed and evidence
  captured in `/tmp/4-3-4-*.log`.

## Surprises & discoveries

- Observation: `docs/cuprum-design.md` Section 13.9 already states that
  property-based tests verify stream content preservation, but no Hypothesis
  tests currently exist in the codebase. Evidence:
  `rg -n "hypothesis|from hypothesis|strategies as st" .` returns no Hypothesis
  imports in test modules. Impact: this task closes a design/implementation
  drift and should update the design doc with concrete test scope and file
  references.

- Observation: Hypothesis raised `FailedHealthCheck` for the
  function-scoped `stream_backend` fixture in property tests. Evidence:
  targeted post-change run failed with `HealthCheck.function_scoped_fixture`.
  Impact: property tests now explicitly suppress that health check in
  `@settings(...)` because backend parameterization is intentional for this
  suite.

- Observation: the generated writer script was invalid when the loop was
  encoded as a single inline statement chain. Evidence:
  `compile(chunked_writer_script(), "<script>", "exec")` raised `SyntaxError`
  and BDD scenarios produced empty output with pipeline stage termination.
  Impact: `chunked_writer_script()` now emits newline-joined Python code with a
  valid `for` block.

## Decision log

- Decision: use byte-preservation as the primary invariant for property-based
  tests, with pipeline output normalized through a deterministic representation
  (for example, hex output). Rationale: byte-preservation avoids false
  negatives from text-decoding behaviour and directly validates transport
  integrity across chunk boundaries. Date/Author: 2026-02-23 / Codex.

- Decision: include a separate deterministic BDD layer in addition to
  Hypothesis-driven unit tests. Rationale: project policy requires behavioural
  tests for new functionality, while BDD scenarios should remain stable and
  readable rather than fully stochastic. Date/Author: 2026-02-23 / Codex.

- Decision: encode payload bytes as base64 input to subprocess scripts and
  assert deterministic lowercase hex output at the sink stage. Rationale: this
  avoids command-line argument encoding ambiguity and validates
  byte-preservation directly, independent of stdout text decoding. Date/Author:
  2026-02-23 / Codex.

- Decision: suppress `HealthCheck.function_scoped_fixture` for Hypothesis tests
  using the `stream_backend` fixture. Rationale: backend parameterization is
  required by roadmap scope and stable in this suite; fixtures intentionally do
  not reset between generated examples. Date/Author: 2026-02-23 / Codex.

## Outcomes & retrospective

Completed implementation for roadmap item `4.3.4`.

What shipped:

- New property-based unit tests in
  `cuprum/unittests/test_stream_property_based.py` using Hypothesis strategies
  for randomized payloads and randomized chunk boundaries.
- New behavioural coverage in
  `tests/features/stream_property_preservation.feature` and
  `tests/behaviour/test_stream_property_preservation_behaviour.py` using
  deterministic randomized cases for consumer-visible pipeline verification.
- Shared helper extensions in `tests/helpers/parity.py` for chunk partitioning,
  deterministic case generation, and reusable script generation.
- Dev dependency update in `pyproject.toml` with lockfile refresh in `uv.lock`.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`.
- Roadmap item marked complete in `docs/roadmap.md`.

Quality-gate evidence:

- `make check-fmt`: pass.
- `make typecheck`: pass.
- `make lint`: pass.
- `make test`: pass (`304 passed, 42 skipped`).
- `make markdownlint`: pass.
- `make nixie`: pass.

Lessons learned:

- Hypothesis plus function-scoped backend fixtures requires explicit
  `HealthCheck.function_scoped_fixture` suppression when fixture reuse is
  intentional.
- Generated Python snippets should be newline-based for control-flow blocks;
  inline semicolon chaining is error-prone for loops.

## Context and orientation

Current relevant state:

- `conftest.py` provides `stream_backend` parameterization and cache clearing
  for
  backend resolution.
- `cuprum/unittests/test_stream_parity.py` and
  `tests/behaviour/test_stream_parity_behaviour.py` provide example-based
  parity coverage, not property-based generation.
- `tests/helpers/parity.py` already contains reusable parity utilities and is
  the natural place for chunk-partition helpers shared by unit and BDD tests.
- `pyproject.toml` dev dependencies currently do not include `hypothesis`.
- `docs/cuprum-design.md` Section 13.9 claims property-based coverage exists.
- `docs/users-guide.md` documents backend parity but does not describe
  randomized property-validation scope.

Working definition for this task:

- Stream content preservation means that bytes emitted by an upstream stage are
  identical to bytes observed downstream, even when writes are split across
  random chunk boundaries.

## Plan of work

Stage A: confirm test shape and invariants.

Identify exact insertion points and decide final strategy bounds. Keep this
task bounded to test and docs updates unless failures reveal a genuine stream
bug. Go/no-go: proceed only once the invariant and file map are explicit.

Stage B: add tests first and confirm fail-first behaviour.

Add a new property-based unit test module, plus BDD scenarios and step
definitions for deterministic randomized cases. Run targeted tests before any
supporting implementation updates and record initial failures.

Stage C: add minimal supporting implementation.

Add Hypothesis dev dependency and any helper functions required to construct
chunk-boundary write plans safely. If property tests expose a production
defect, apply the smallest possible fix and document why in `Decision Log`.

Stage D: documentation and roadmap updates.

Update design and user documentation with the final property coverage contract,
then mark roadmap item `4.3.4` as done only after all quality gates pass.

Stage E: full validation.

Run required quality gates and capture log evidence.

## Concrete steps

1. Add test scaffolding first (fail-first workflow).

   Planned files: `cuprum/unittests/test_stream_property_based.py`
   `tests/features/stream_property_preservation.feature`
   `tests/behaviour/test_stream_property_preservation_behaviour.py`
   `tests/helpers/parity.py` (helper additions only)

   Unit scope:
   - Hypothesis strategies for random payload bytes and random chunk boundaries.
   - Backend-parametrised pipeline execution via `stream_backend`.
   - Assertions that downstream-observed bytes match original bytes.

   Behavioural scope:
   - Deterministic seeded random scenarios that exercise chunk-boundary stress
     and verify preserved output via user-visible pipeline results.

2. Run targeted tests and capture initial failure.

       set -o pipefail
       UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
         cuprum/unittests/test_stream_property_based.py \
         tests/behaviour/test_stream_property_preservation_behaviour.py \
         2>&1 | tee /tmp/4-3-4-targeted-pre.log

   Expected before dependency/support updates:

       ERROR … ModuleNotFoundError: No module named 'hypothesis'

   If dependency is present already, expected pre-run should still fail because
   new assertions are intentionally written before helper/implementation
   updates.

3. Add or update support code with minimal scope.

   Planned files: `pyproject.toml` (dev dependency: `hypothesis`) `uv.lock`
   (lockfile refresh) `tests/helpers/parity.py` (shared chunk-plan/script
   helpers)

4. Re-run targeted tests until green.

       set -o pipefail
       UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run pytest -q \
         cuprum/unittests/test_stream_property_based.py \
         tests/behaviour/test_stream_property_preservation_behaviour.py \
         2>&1 | tee /tmp/4-3-4-targeted-post.log

5. Update documentation and roadmap.

   Planned files: `docs/cuprum-design.md` `docs/users-guide.md`
   `docs/roadmap.md`

   Documentation updates must include:
   - design-level rationale for byte-preservation invariant and strategy bounds;
   - users-guide note describing backend parity assurance from randomized tests;
   - roadmap item `4.3.4` switched from `[ ]` to `[x]` only after successful
     full validation.

6. Run full quality gates with log capture.

       set -o pipefail
       make check-fmt 2>&1 | tee /tmp/4-3-4-check-fmt.log
       make typecheck 2>&1 | tee /tmp/4-3-4-typecheck.log
       make lint 2>&1 | tee /tmp/4-3-4-lint.log
       make test 2>&1 | tee /tmp/4-3-4-test.log

7. Run markdown validation gates for documentation changes.

       set -o pipefail
       make markdownlint 2>&1 | tee /tmp/4-3-4-markdownlint.log
       make nixie 2>&1 | tee /tmp/4-3-4-nixie.log

8. Record final evidence and close this ExecPlan.

## Validation and acceptance

Behavioural acceptance:

- Randomized chunk-boundary scenarios in `pytest-bdd` complete successfully
  under both backends (with Rust cases skipped when unavailable).
- Property-based unit tests demonstrate content preservation for generated
  payloads and chunk boundaries without flaky failures.

Quality criteria:

- `make check-fmt` passes.
- `make typecheck` passes.
- `make lint` passes.
- `make test` passes, including new property-based and behavioural tests.
- `make markdownlint` passes after documentation updates.
- `make nixie` passes after documentation updates.
- `docs/roadmap.md` marks `4.3.4` as done only after all checks above pass.

## Idempotence and recovery

- All test commands are safe to rerun.
- If Hypothesis finds a failing example, preserve the falsifying seed/example in
  this document and convert it into a stable regression test where appropriate.
- If runtime is too high, reduce `max_examples` and document the trade-off in
  `Decision Log`.
- If command-line size failures occur, shift payload transport to encoded input
  in subprocess code and rerun targeted tests.

## Artifacts and notes

Expected artefacts:

- Targeted pre-change and post-change logs in `/tmp/4-3-4-targeted-*.log`.
- Full quality-gate logs in `/tmp/4-3-4-*.log`.
- Updated docs and roadmap entries tied to roadmap item `4.3.4`.

## Interfaces and dependencies

Existing interfaces to use:

- `cuprum._backend.StreamBackend` and `get_stream_backend()` behaviour via the
  existing `stream_backend` fixture.
- Pipeline execution through `sh.make(...)`, pipeline composition (`|`), and
  `run_sync()` inside scoped allowlists.
- Shared helpers in `tests/helpers/parity.py`.

Dependency policy for this task:

- Add `hypothesis` as a dev dependency only.
- No runtime dependency changes.

Revision note (2026-02-23):

- Updated status from `DRAFT` to `IN PROGRESS` and then `COMPLETE`.
- Recorded implementation progress, discovered issues, decisions, and final
  outcomes with quality-gate results.
- Captured targeted fail-first evidence (`ModuleNotFoundError`) and final
  passing evidence (`304 passed, 42 skipped`).
