# Performance extensions foundation (4.1 + 4.5)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

PLANS.md is not present in this repository.

## Purpose / Big Picture

Deliver the build-system and documentation foundation for optional Rust-backed
stream operations. After this change, contributors can build native wheels with
maturin directly, build the pure-Python wheel with `uv_build`, and publish the
full wheel set with `uv publish` in a single command. Python can import a
minimal Rust extension that exposes `is_available()` to prove the binding
works. Users can read updated documentation explaining the Rust architecture,
API boundary, fallback strategy, and performance traits. Success is visible
when both wheel types build and install, the Rust module imports and returns a
stub value, metadata matches across wheel families, and the CI matrix includes
the requested platforms.

## Constraints

- Keep pure Python as a first-class pathway; no feature may require Rust at
  runtime.
- Use `uv_build` as the primary build backend; the hatchling mention in the
  prompt is outdated and must not drive changes.
- Follow the documentation style guide in
  `docs/documentation-style-guide.md`, including 80-column wrapping.
- Use Makefile targets for tests, linting, formatting, and type checking.
- When running commands with long output, use `set -o pipefail` and `tee` to
  capture logs.
- Do not introduce public API changes beyond the optional Rust extension
  import surface without explicit approval.
- Rust lints in Appendix 1 must be enforced in the new Cargo workspace.
- Cuprum does not use cibuildwheel; native wheels must be built with maturin
  directly.
- CI must continue to pass on existing workflows; new jobs must not break
  existing ones.

## Tolerances (Exception Triggers)

- Scope: if implementation requires touching more than 25 files or more than
  1,500 net lines, stop and escalate.
- Interfaces: if a public Python API signature must change, stop and
  escalate.
- Dependencies: if additional external dependencies beyond `uv_build`,
  maturin, PyO3, and Rust tooling are required, stop and escalate.
- CI: if adding a new workflow or reworking more than one existing workflow is
  required, stop and escalate.
- Tests: if tests still fail after two full fix attempts, stop and escalate.
- Ambiguity: if the mismatch between the roadmap numbering (4.1.4 vs 4.1.5 in
  the prompt) would require adding new roadmap items, stop and escalate with
  options.

## Risks

- Risk: The plan must keep `uv_build` as the primary backend while adding
  maturin support for native wheels, which may require careful `pyproject.toml`
  configuration. Severity: medium Likelihood: medium Mitigation: document how
  `uv_build` remains the default and ensure pure Python builds work end-to-end
  before adding native-wheel steps.

- Risk: CI wheel matrix and Rust toolchain setup might not be compatible with
  the current GitHub Actions environment or uv-based build. Severity: medium
  Likelihood: medium Mitigation: follow the updated maturin build workflow,
  keep manylinux containers explicit, and validate locally where possible.

- Risk: `docs/cuprum-design.md` already contains Section 13, so the requested
  update may be a revision rather than an addition. Severity: low Likelihood:
  high Mitigation: update Section 13 to explicitly cover architecture, API
  boundary, fallback strategy, and performance characteristics, noting changes
  in the Decision Log.

## Progress

- [x] (2026-01-19 00:00Z) Plan approved and implementation started.
- [x] (2026-01-19 00:10Z) Reviewed `pyproject.toml`, CI workflows, and Rust
  ADR/design documentation.
- [x] (2026-01-19 00:20Z) Drafted tests for the Rust extension availability
  probe (unit + behavioural).
- [x] (2026-01-19 00:30Z) Defined Rust workspace layout and implemented PyO3
  `is_available()` binding plus Python fallback.
- [x] (2026-01-19 00:40Z) Planned and applied `pyproject.toml` updates for
  `uv_build` plus maturin support.
- [x] (2026-01-19 00:50Z) Planned and applied CI changes for wheel matrix,
  Rust setup, and pure-Python wheel verification.
- [x] (2026-01-19 01:00Z) Updated documentation and roadmap entries.
- [x] (2026-01-19 01:20Z) Ran formatting, linting, type checking, tests, and
  Markdown validation per acceptance criteria.
- [x] (2026-01-19 02:10Z) Removed cibuildwheel from workflows and rebuilt the
  wheel pipeline using `uv build` + `maturin build` with metadata checks.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Treat this work as Phase 4.1 build-system integration plus Phase
  4.5 documentation updates, aligning to the roadmap while noting the prompt's
  4.1.5 numbering mismatch. Rationale: Keeps changes scoped to requested
  foundation work and aligns with the existing roadmap structure. Date/Author:
  2026-01-18 / Codex
- Decision: Keep `uv_build` as the primary build backend; treat the hatchling
  mention as outdated per user instruction. Rationale: Matches the current
  `pyproject.toml` and avoids unnecessary build migration risk. Date/Author:
  2026-01-18 / Codex
- Decision: Expose the native module as `cuprum._rust_backend_native` with a
  Python shim `cuprum._rust_backend` for safe availability probing. Rationale:
  Avoids name collisions between the Python shim and the compiled extension
  while keeping the import path stable for future backend dispatch.
  Date/Author: 2026-01-19 / Codex
- Decision: Replace cibuildwheel with direct maturin builds and enforce a
  two-route wheel strategy (`uv build` + `maturin build`) with metadata drift
  checks. Rationale: Aligns CI and release workflows with the updated policy
  and makes the manylinux strategy explicit. Date/Author: 2026-01-19 / Codex

## Outcomes & Retrospective

The optional Rust extension foundation is now in place. Native wheel builds use
`maturin build` directly, pure Python wheels continue to use `uv_build`, and
the Rust availability probe is exercised by new unit and behavioural tests.
Documentation updates capture the architecture, API boundary, fallback
strategy, and build prerequisites. CI now verifies that native and pure Python
wheels install in the same environment in sequence and validates metadata
consistency across wheel families. Release automation publishes the full wheel
set with `uv publish` in a single step.

## Context and Orientation

Relevant files and directories:

- `pyproject.toml` currently uses `uv_build` as the build backend.
- `docs/roadmap.md` defines Phase 4.1 tasks (4.1.1 to 4.1.4).
- `docs/adr-001-rust-extension.md` records the Rust extension decision and
  runtime selection model.
- `docs/cuprum-design.md` contains Section 13 on performance-optimised stream
  operations (needs revision to meet the prompt's Section 13 requirements).
- `docs/users-guide.md` documents user-facing behaviour and must include any
  new configuration or build guidance.
- `cuprum/_streams.py` is the existing Python stream implementation that the
  Rust extension will complement.
- `.github/workflows/build-wheels.yml` and
  `.github/actions/build-wheels/action.yml` define the current wheel build
  pipeline using direct maturin builds.
- `.github/workflows/ci.yml` is the primary lint/test workflow.

Terminology:

- "Pure Python wheel" means a wheel built without native Rust code and no
  dependency on a Rust toolchain at install time.
- "Native wheel" means a wheel built with the Rust extension embedded.
- "Backend" refers to a PEP 517 build backend specified in `pyproject.toml`.

## Plan of Work

Stage A: discovery and decisions (no code changes).

- Re-read `docs/roadmap.md`, `docs/adr-001-rust-extension.md`, and
  `docs/cuprum-design.md` Section 13 to confirm intended architecture and
  ensure the plan aligns with existing decisions.
- Inspect `pyproject.toml` and existing CI workflows to decide how to support
  `uv_build` alongside maturin without breaking the current uv workflow.
- Document the build-system decision in `docs/cuprum-design.md` Section 13 and
  in this plan's Decision Log (for example: keep `uv_build` as the default
  backend and use maturin via CI for native wheels).
- Decide the Python import surface for the Rust extension (for example,
  `cuprum._rust` or `cuprum._rust_backend`) and document the intended
  `is_available()` signature.

Stage B: scaffolding and tests (small, verifiable diffs).

- Add unit tests under `cuprum/unittests/` that validate the new import path
  and `is_available()` behaviour. These tests should use a fallback path so
  they pass when Rust is unavailable (e.g., skip or assert fallback to Python
  stub).
- Add a pytest-bdd feature under `tests/features/` and a corresponding
  behavioural test under `tests/behaviour/` that exercises installation and
  import expectations at a user level (for example, importing the module and
  calling `is_available()` returns `True` when the Rust extension is present
  and `False` otherwise).
- Ensure the tests are written before implementation so they fail without the
  new module and pass after it is wired in.

Stage C: implementation (minimal code and build integration).

- Create `rust/` with a Cargo workspace. Add `rust/Cargo.toml` and a crate for
  the PyO3 extension. Use the Rust lint configuration from Appendix 1 at the
  workspace level. Include crate-level docs to satisfy missing_docs lints.
- Implement minimal PyO3 binding exporting `is_available()` returning `True`
  from Rust. Provide Python-side fallback module (if needed) that returns
  `False` when the Rust extension is missing.
- Update `pyproject.toml` to support `uv_build` plus maturin for optional
  native builds. Ensure pure-Python builds still succeed without Rust. Document
  the chosen backend approach (including any required `tool.maturin` settings).
- Add a Rust-specific `Makefile` under `rust/` using Appendix 2 targets (or
  merge compatible targets into the root Makefile if that is the project
  preference). Ensure `make check-fmt` and `make lint` remain applicable for
  Rust and Python.
- Extend CI wheel builds:
  - Add stable Rust setup and a pinned maturin version in the wheel build
    action.
  - Build native wheels with `maturin build` directly; use a manylinux
    container on Linux and document the aarch64 strategy explicitly.
  - Add a pure-Python wheel job that excludes native builds, and verify both
    wheel types can be installed in the same environment with metadata drift
    checks.

Stage D: documentation, roadmap, and validation.

- Update `docs/cuprum-design.md` Section 13 to explicitly cover:
  - Rust extension architecture (crate layout, PyO3 boundary).
  - API boundary between Python and Rust (what crosses the FFI boundary).
  - Fallback strategy (auto/rust/python selection and behaviour).
  - Performance characteristics and limitations.
- Update `docs/users-guide.md` with user-facing guidance on the Rust extension,
  including build prerequisites, environment selection, and expected behaviour
  for missing native wheels.
- Update `docs/roadmap.md` to mark the relevant Phase 4.1 and 4.5 items as
  done. Note the numbering mismatch in the Decision Log and keep the roadmap
  consistent with its existing numbering.
- Run formatting, linting, type checking, and tests using Makefile targets.

## Concrete Steps

All commands run from `/root/repo` unless noted. Use `set -o pipefail` and
`tee` for long outputs.

1) Inspect existing docs and build configuration.

    ```bash
    rg -n "build-backend" pyproject.toml
    rg -n "maturin|uv_build" pyproject.toml docs
    rg -n "wheel" .github/workflows -g "*.yml"
    ```

2) Build wheels using the CI commands.

    ```bash
    uv build --wheel --out-dir dist
    maturin build --release --compatibility pypi --out wheelhouse \
      --manifest-path rust/cuprum-rust/Cargo.toml
    ```

3) Write tests first.

    ```bash
    set -o pipefail
    uv run pytest cuprum/unittests/test_rust_extension.py \
      | tee /tmp/test-rust-unit.txt

    set -o pipefail
    uv run pytest tests/behaviour/test_rust_extension_behaviour.py \
      tests/features/rust_extension.feature \
      | tee /tmp/test-rust-behaviour.txt
    ```

4) Implement Rust scaffold and Python fallback, then re-run the new tests.

    ```bash
    set -o pipefail
    uv run pytest cuprum/unittests/test_rust_extension.py \
      | tee /tmp/test-rust-unit.txt

    set -o pipefail
    uv run pytest tests/behaviour/test_rust_extension_behaviour.py \
      tests/features/rust_extension.feature \
      | tee /tmp/test-rust-behaviour.txt
    ```

5) Run formatting, linting, type checking, and full test suite.

    ```bash
    set -o pipefail
    make check-fmt | tee /tmp/make-check-fmt.txt

    set -o pipefail
    make lint | tee /tmp/make-lint.txt

    set -o pipefail
    make typecheck | tee /tmp/make-typecheck.txt

    set -o pipefail
    make test | tee /tmp/make-test.txt
    ```

6) Run Markdown linting and Mermaid validation after doc changes.

    ```bash
    set -o pipefail
    MDLINT=/root/.bun/bin/markdownlint-cli2 make markdownlint \
      | tee /tmp/make-markdownlint.txt

    set -o pipefail
    make nixie | tee /tmp/make-nixie.txt
    ```

## Validation and Acceptance

Acceptance is satisfied when all of the following are true:

- Running the new unit tests and behavioural tests fails before implementation
  and passes after the Rust extension scaffold is added.
- Python can import the Rust extension module on native-wheel builds and
  `is_available()` returns `True` from Rust.
- When the Rust extension is absent, the Python fallback is available and
  `is_available()` returns `False` without breaking existing behaviour.
- CI wheel matrix includes Linux (x86_64, aarch64), macOS (x86_64, arm64), and
  Windows (x86_64, arm64) native wheel builds using maturin directly.
- A pure-Python wheel job builds successfully and coexists with native wheels
  in the same environment, verified by installing pure then native wheels and
  checking `_rust_backend.is_available()` plus metadata parity.
- Release automation collects all wheels into a single directory and publishes
  them via `uv publish` in a single command.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`
  reflect the new architecture and user guidance.
- `docs/roadmap.md` marks the relevant 4.1 and 4.5 items as done.
- Quality gates succeed:
  - `make check-fmt`
  - `make lint`
  - `make typecheck`
  - `make test`
  - `make markdownlint`
  - `make nixie`

## Idempotence and Recovery

- All steps are repeatable; re-running the commands should be safe.
- If a step fails, fix the reported issue and re-run the same command to
  confirm the fix. Keep the temporary logs in `/tmp` for inspection.
- If the build backend change breaks packaging, revert the build-system section
  in `pyproject.toml`, document the failure in the Decision Log, and
  re-evaluate the backend approach before continuing.

## Artifacts and Notes

Expected artifacts after CI configuration:

- New Rust workspace files under `rust/` (Cargo workspace and crate).
- Updated `pyproject.toml` build-system configuration supporting `uv_build`
  and maturin.
- Updated CI workflows or actions to build native and pure-Python wheels.
- Updated documentation in `docs/cuprum-design.md`, `docs/users-guide.md`, and
  roadmap changes in `docs/roadmap.md`.

## Interfaces and Dependencies

Python interface:

- New internal module path for the Rust extension, such as
  `cuprum._rust_backend` with a function:

    def is_available() -> bool: …

- Optional Python fallback module (same import path) returning `False` when the
  Rust extension is missing.

Rust interface:

- Workspace path: `rust/` with a crate that builds a Python extension module
  (PyO3). The Rust module must export `is_available()` returning `true`.
- Enforce Rust lints from Appendix 1 via workspace `Cargo.toml`.

Dependencies:

- Python build tooling: `uv_build` (default) and maturin (native builds).
- Rust: PyO3, cargo, rustfmt, clippy, and optional `cargo nextest` if used by
  the Rust Makefile.
- CI: maturin for wheel builds; stable Rust toolchain setup in workflows.

## Revision note (required when editing an ExecPlan)

Initial draft authored on 2026-01-18. Revised to lock in `uv_build` as the
primary backend per user instruction. Updated status to IN PROGRESS and
recorded approval to begin implementation. Recorded execution progress and the
module naming decision for the Rust availability probe. Updated the plan to
remove cibuildwheel, document direct maturin builds, and add the uv publish
release flow. Marked the plan complete after validation steps succeeded.
