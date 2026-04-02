# Document build prerequisites and troubleshooting (4.5.3 + 4.5.4)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: DRAFT

Roadmap references: `docs/roadmap.md` items `4.5.3` and `4.5.4`.

## Purpose / big picture

Roadmap items `4.5.3` and `4.5.4` require contributor-facing documentation that
covers two complementary topics:

1. **Build prerequisites** (4.5.3): what a contributor needs to install to
   build Cuprum's optional Rust extension from source, including exact
   toolchain versions, the maturin build tool, and platform-specific notes.

2. **Troubleshooting** (4.5.4): a dedicated section in the users' guide
   addressing common operational issues, specifically missing wheels on exotic
   platforms, forced fallback behaviour via `CUPRUM_STREAM_BACKEND`, and
   benchmark result interpretation.

These two items are combined into a single ExecPlan because they are tightly
related, touch the same documentation files, and share the same test strategy
(documentation-contract tests that assert the guide exposes the promised
content).

After this work, a contributor should be able to:

1. Read one clear section in `docs/users-guide.md` and know exactly which
   tools to install (Rust toolchain, cargo, maturin) and what minimum versions
   are required.
2. Follow step-by-step instructions for `maturin develop` to build the
   native extension locally.
3. Find a troubleshooting section that addresses the most common failure
   modes: missing native wheels, forced fallback, and benchmark interpretation.
4. Trust that the documented prerequisites match the actual build
   configuration in `pyproject.toml` and `rust/cuprum-rust/Cargo.toml`.

This task is complete only when:

- `docs/users-guide.md` contains explicit build prerequisite documentation
  and a troubleshooting section;
- `docs/cuprum-design.md` records any design clarifications made during
  this work (specifically the minimum Rust toolchain version);
- `docs/roadmap.md` marks items `4.5.3` and `4.5.4` as done;
- unit tests (`pytest`) and behavioural tests (`pytest-bdd`) validate the
  documented content through documentation-contract assertions;
- quality gates pass:
  `make check-fmt`, `make typecheck`, `make lint`, `make test`,
  `make markdownlint`, and `make nixie`.

## Constraints

- Keep scope bounded to roadmap items `4.5.3` and `4.5.4`. This is a
  documentation-led task. Do not modify build system configuration, Rust source
  code, or CI workflows.
- Treat the shipped `pyproject.toml` and `rust/cuprum-rust/Cargo.toml` as
  the source of truth for version requirements. Documentation must match the
  actual build configuration.
- Follow repository test-first policy: add or modify tests first, confirm a
  failing state, then change the docs.
- Provide both test styles:
  - unit tests in `cuprum/unittests/` with `pytest`;
  - behavioural tests in `tests/behaviour/` plus Gherkin in
    `tests/features/` using `pytest-bdd`.
- Do not introduce new runtime or documentation tooling dependencies.
- Keep documentation aligned across:
  `docs/users-guide.md`, `docs/cuprum-design.md`, and `docs/roadmap.md`.
- Follow `docs/documentation-style-guide.md`, especially: British English,
  80-column paragraph wrapping, explicit code block language tags, and
  sentence-case headings.
- Keep any Python changes compliant with `.rules/python-*.md`, Ruff, and
  type checks.
- Do not give test module files (unit and behavioural) the same basename;
  pytest will raise an import file mismatch. Use a `_behaviour` suffix for the
  behavioural test module.

## Tolerances (exception triggers)

- Scope: if implementation requires edits to more than 10 files, including
  this ExecPlan, stop and escalate.
- Runtime behaviour: if completing these roadmap items requires substantive
  production code changes outside test hooks or documentation, stop and
  escalate. The expected change should be mostly docs and tests.
- Toolchain version: if the minimum Rust version implied by the Cargo.toml
  `edition = "2024"` field cannot be determined from Rust release history, stop
  and escalate rather than guessing.
- Documentation conflict: if the existing users' guide or design document
  contradicts the build configuration in a way that cannot be resolved by
  reading the source, stop and escalate.
- Iterations: if the same failing test persists after 3 fix attempts, stop
  and escalate with the observed failure and candidate fixes.

## Risks

- Risk: the roadmap says "Rust toolchain 1.70+" but the design document
  says "rustc 1.74+", while the actual `Cargo.toml` uses `edition = "2024"`
  which requires Rust 1.85+. The documented minimum must match the real build
  requirement. Severity: high. Likelihood: certain (the discrepancy already
  exists). Mitigation: use the `edition = "2024"` requirement as the
  authoritative source and document Rust 1.85+ as the minimum. Update the
  design document to match. Note that the roadmap text "1.70+" was aspirational
  and is superseded by the actual Cargo configuration.

- Risk: the troubleshooting section could become stale if future changes
  alter fallback behaviour or benchmark infrastructure. Severity: medium.
  Likelihood: medium. Mitigation: write troubleshooting content that references
  existing mechanisms (`CUPRUM_STREAM_BACKEND`, `is_rust_available()`,
  `make benchmark-e2e`) rather than hard-coding transient details.

- Risk: documentation-contract tests may be brittle if they assert exact
  prose wording rather than the presence of key concepts. Severity: medium.
  Likelihood: medium. Mitigation: assert the presence of key terms and section
  headings rather than exact sentences. Use substring and pattern matching,
  following the approach established in
  `cuprum/unittests/test_performance_guidance_docs.py`.

- Risk: the troubleshooting section for benchmark interpretation overlaps
  with the existing benchmark suite documentation in the users' guide.
  Severity: low. Likelihood: high. Mitigation: the troubleshooting section
  should cross-reference the existing benchmark documentation rather than
  duplicating it, and focus specifically on common misinterpretations and
  failure modes.

## Progress

- [ ] Reviewed roadmap items, existing documentation, and build
  configuration.
- [ ] Drafted this ExecPlan.
- [ ] Stage A: added fail-first documentation-contract tests.
- [ ] Stage B: updated `docs/users-guide.md` with build prerequisites
  section.
- [ ] Stage C: updated `docs/users-guide.md` with troubleshooting section.
- [ ] Stage D: reconciled `docs/cuprum-design.md` minimum Rust version.
- [ ] Stage E: marked roadmap items `4.5.3` and `4.5.4` done.
- [ ] Stage F: final validation and retrospective.

## Surprises & discoveries

(To be populated during implementation.)

## Decision log

- Decision: combine roadmap items `4.5.3` and `4.5.4` into a single
  ExecPlan. Rationale: the two items touch the same files, share the same test
  strategy, and are small enough that splitting them would create unnecessary
  overhead. Date/Author: 2026-04-02 / Codex.

- Decision: document Rust 1.85+ as the minimum toolchain version, not
  1.70+ or 1.74+. Rationale: `rust/cuprum-rust/Cargo.toml` specifies
  `edition = "2024"`, which requires Rust 1.85 or later. The roadmap text
  "1.70+" and the design document text "1.74+" are both stale. The Cargo
  configuration is the authoritative source. Date/Author: 2026-04-02 / Codex.

- Decision: use documentation-contract tests (asserting the presence of key
  terms in the markdown files) rather than runtime tests. Rationale: these
  roadmap items are documentation-led, not runtime features. The precedent set
  by `4.5.2` (`cuprum/unittests/test_performance_guidance_docs.py`) establishes
  this pattern. Date/Author: 2026-04-02 / Codex.

## Outcomes & retrospective

(To be populated on completion.)

## Context and orientation

### Repository layout

Cuprum is a Python library with an optional Rust extension for stream
performance. The project uses `uv` as the Python package manager and `maturin`
as the Rust-to-Python build bridge.

Key files for this task:

- `docs/users-guide.md` — the primary consumer-facing documentation.
  Currently contains a "Building from source" subsection (around line 1139) and
  a "Performance extensions (optional Rust)" section (around line 938). The
  existing "Building from source" content is brief: it mentions
  `maturin develop` and notes that contributors need "rustc and cargo, version
  1.74 or newer". This needs expansion and correction.

- `docs/cuprum-design.md` — the design document. Section 13.8 states
  "Contributors building from source require a Rust toolchain (rustc 1.74+,
  cargo)." This must be updated to match the actual Cargo edition requirement.

- `docs/roadmap.md` — tracks task completion. Items `4.5.3` and `4.5.4`
  are currently unchecked.

- `pyproject.toml` — build configuration. Uses `uv_build` as the primary
  build backend. Maturin 1.6.0 is pinned as a dev dependency. The
  `[tool.maturin]` section configures PyO3 bindings with manifest path
  `rust/cuprum-rust/Cargo.toml` and module name `cuprum._rust_backend_native`.

- `rust/cuprum-rust/Cargo.toml` — the Rust crate configuration. Uses
  `edition = "2024"` (requiring Rust 1.85+), PyO3 0.27.2, and `libc 0.2` on
  Linux.

- `rust/Makefile` — Rust-specific build targets including `make build`,
  `make test`, `make lint`, `make fmt`.

### Existing test patterns

The `4.5.2` implementation established a documentation-contract test pattern
using:

- `cuprum/unittests/test_performance_guidance_docs.py` — unit tests that
  read `docs/users-guide.md` and assert the presence of key guidance terms.
- `tests/features/performance_guidance_docs.feature` — Gherkin feature file
  describing the expected documentation content.
- `tests/behaviour/test_performance_guidance_docs_behaviour.py` —
  pytest-bdd step implementations.

This ExecPlan follows the same pattern with new test files:

- `cuprum/unittests/test_prerequisite_docs.py`
- `tests/features/prerequisite_docs.feature`
- `tests/behaviour/test_prerequisite_docs_behaviour.py`

### Build prerequisites (ground truth)

From the actual build configuration:

- Python: 3.12+ (from `pyproject.toml` `requires-python`)
- Rust toolchain: 1.85+ (from `edition = "2024"` in Cargo.toml)
- cargo: ships with the Rust toolchain via `rustup`
- maturin: 1.6.0 (pinned in `pyproject.toml` dev dependencies)
- uv: used as the Python package manager (not strictly a build
  prerequisite, but needed for the development workflow)

Platform-specific notes:

- Linux: the `libc` crate is an additional dependency, used for `splice()`
  system call support. This is handled automatically by cargo.
- macOS: no platform-specific Rust dependencies.
- Windows: no platform-specific Rust dependencies, but file descriptor
  handling differs internally.

### Troubleshooting topics (from roadmap)

The roadmap specifies three troubleshooting areas:

1. **Missing wheels on exotic platforms**: when no pre-built native wheel
   exists, `pip install cuprum` falls back to the pure Python wheel.
   Contributors on unsupported platforms can build from source with
   `maturin develop`.

2. **Forced fallback behaviour**: `CUPRUM_STREAM_BACKEND=rust` raises
   `ImportError` when the extension is unavailable. `auto` (the default)
   silently falls back. `python` forces the pure Python pathway.

3. **Benchmark result interpretation**: the existing benchmark
   documentation in `docs/users-guide.md` already covers scenario naming and
   metric definitions. The troubleshooting section should address common
   misunderstandings such as why Rust shows no improvement for small payloads,
   why `splice()` is Linux-only, and how to read the CI comparison summary
   table.

## Plan of work

### Stage A: add fail-first documentation-contract tests

Before changing the guide, create tests that assert the documentation contains
the required prerequisite and troubleshooting content. These tests should fail
initially because the content does not yet exist.

Create three test files:

1. `cuprum/unittests/test_prerequisite_docs.py` — unit tests that read
   `docs/users-guide.md` and assert the presence of:
   - a section heading containing "prerequisites" or "build prerequisites"
     (case-insensitive);
   - mention of "Rust" and a version number (1.85 or later);
   - mention of "maturin";
   - mention of "cargo";
   - mention of `maturin develop`;
   - a section heading containing "troubleshooting" (case-insensitive);
   - mention of "missing wheels" or "exotic platforms";
   - mention of "fallback" in the troubleshooting context;
   - mention of benchmark interpretation or benchmark results.

2. `tests/features/prerequisite_docs.feature` — Gherkin scenarios:
   - Scenario: users' guide documents Rust build prerequisites.
   - Scenario: users' guide includes troubleshooting guidance.

3. `tests/behaviour/test_prerequisite_docs_behaviour.py` — pytest-bdd
   step implementations for the feature file.

Go/no-go: proceed only after the new tests fail, demonstrating the
documentation gap.

### Stage B: update `docs/users-guide.md` with build prerequisites

Expand the existing "Building from source" subsection in the "Performance
extensions (optional Rust)" section. The updated content should include:

- A clear heading: "### Build prerequisites for native extensions"
- Minimum Rust toolchain version: 1.85+ (matching `edition = "2024"`)
- How to install: `rustup` with a link to the official installer
- cargo: bundled with rustup
- maturin: installed automatically as a dev dependency via
  `uv sync --group dev`, or manually via `pip install maturin==1.6.0`
- Step-by-step: `maturin develop` from the project root
- How to verify: `python -c "import cuprum; print(cuprum.is_rust_available())"`
  should print `True`
- Pure Python builds: `uv build --wheel` requires no Rust toolchain

Go/no-go: the prerequisite-related unit tests should now pass.

### Stage C: update `docs/users-guide.md` with troubleshooting section

Add a new subsection: "### Troubleshooting" within the "Performance extensions
(optional Rust)" section. Cover three topics:

1. **Missing wheels on exotic platforms**: explain that pre-built native
   wheels are published for common platforms (Linux x86_64/aarch64, macOS
   x86_64/arm64, Windows x86_64/arm64). On other platforms, the pure Python
   wheel is installed automatically. Contributors who want Rust acceleration on
   unsupported platforms can build from source using the prerequisite
   instructions above.

2. **Forced fallback behaviour**: explain the three modes of
   `CUPRUM_STREAM_BACKEND` (`auto`, `python`, `rust`) and what happens when
   `rust` is forced but the extension is unavailable (`ImportError`). Reference
   the existing "Choosing a stream backend" section for full details. Provide a
   diagnostic command:
   `python -c "import cuprum; print(cuprum.is_rust_available())"`.

3. **Benchmark result interpretation**: explain common sources of
   confusion. Small payloads show negligible difference because the overhead
   being avoided is per-chunk, and small payloads have few chunks. The
   `splice()` optimization is Linux-only; macOS and Windows use read/write
   loops. The CI comparison summary reports speedup as
   `python_mean / rust_mean`, so values above `1.0x` mean Rust was faster.
   Reference `make benchmark-e2e` for local measurement.

Go/no-go: all documentation-contract tests should now pass.

### Stage D: reconcile `docs/cuprum-design.md`

Update section 13.8 to change "rustc 1.74+" to "rustc 1.85+" to match the
actual `edition = "2024"` requirement in `rust/cuprum-rust/Cargo.toml`.

Go/no-go: the design document and users' guide should state the same minimum
Rust version.

### Stage E: mark roadmap items done

Update `docs/roadmap.md`:

- Change `- [ ] 4.5.3.` to `- [x] 4.5.3.`
- Change `- [ ] 4.5.4.` to `- [x] 4.5.4.`

### Stage F: final validation and retrospective

Run the full quality gate suite and update this ExecPlan with outcomes.

## Concrete steps

1. Create `cuprum/unittests/test_prerequisite_docs.py` with
   documentation-contract unit tests.

2. Create `tests/features/prerequisite_docs.feature` with Gherkin
   scenarios.

3. Create `tests/behaviour/test_prerequisite_docs_behaviour.py` with
   pytest-bdd step implementations.

4. Run targeted tests to confirm the red phase:

   ```bash
   set -o pipefail
   uv run pytest -q \
     cuprum/unittests/test_prerequisite_docs.py \
     tests/behaviour/test_prerequisite_docs_behaviour.py \
     2>&1 | tee /tmp/4-5-3-targeted-pre.log
   ```

   Expected: failures because the documentation content does not yet exist.

5. Update `docs/users-guide.md` with the build prerequisites subsection.

6. Update `docs/users-guide.md` with the troubleshooting subsection.

7. Run targeted tests to confirm the green phase:

   ```bash
   set -o pipefail
   uv run pytest -q \
     cuprum/unittests/test_prerequisite_docs.py \
     tests/behaviour/test_prerequisite_docs_behaviour.py \
     2>&1 | tee /tmp/4-5-3-targeted-post.log
   ```

   Expected: all tests pass.

8. Update `docs/cuprum-design.md` section 13.8 (minimum Rust version).

9. Update `docs/roadmap.md` (mark 4.5.3 and 4.5.4 done).

10. Run full validation:

    ```bash
    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/4-5-3-check-fmt.log
    ```

    ```bash
    set -o pipefail
    make typecheck 2>&1 | tee /tmp/4-5-3-typecheck.log
    ```

    ```bash
    set -o pipefail
    make lint 2>&1 | tee /tmp/4-5-3-lint.log
    ```

    ```bash
    set -o pipefail
    make test 2>&1 | tee /tmp/4-5-3-test.log
    ```

    ```bash
    set -o pipefail
    MDLINT=/root/.bun/bin/markdownlint-cli2 make markdownlint \
      2>&1 | tee /tmp/4-5-3-markdownlint.log
    ```

    ```bash
    set -o pipefail
    make nixie 2>&1 | tee /tmp/4-5-3-nixie.log
    ```

11. Update this ExecPlan with outcomes and retrospective.

## Validation and acceptance

Quality criteria (what "done" means):

- Tests: `make test` passes with the new documentation-contract tests
  included. The new tests in `test_prerequisite_docs.py` and
  `test_prerequisite_docs_behaviour.py` fail before the documentation updates
  and pass after.
- Lint/typecheck: `make check-fmt`, `make typecheck`, and `make lint` all
  pass.
- Markdown: `make markdownlint` and `make nixie` pass.
- Documentation: `docs/users-guide.md` contains a build prerequisites
  section and a troubleshooting section. `docs/cuprum-design.md` states the
  correct minimum Rust version. `docs/roadmap.md` marks `4.5.3` and `4.5.4` as
  done.

Quality method (how we check):

- Run the full validation suite as specified in the concrete steps.
- Visually confirm that the new sections are well-structured and follow
  the documentation style guide.

## Idempotence and recovery

All steps are idempotent. Test files can be recreated. Documentation edits can
be re-applied. The `make` targets are safe to re-run.

If a step fails partway through, fix the issue and re-run the same step. No
rollback is needed because this task only adds documentation and tests.

## Artifacts and notes

Expected new files:

- `cuprum/unittests/test_prerequisite_docs.py`
- `tests/features/prerequisite_docs.feature`
- `tests/behaviour/test_prerequisite_docs_behaviour.py`

Expected modified files:

- `docs/users-guide.md` (build prerequisites and troubleshooting sections)
- `docs/cuprum-design.md` (minimum Rust version in section 13.8)
- `docs/roadmap.md` (mark 4.5.3 and 4.5.4 done)
- `docs/execplans/4-5-3-prerequisite-and-troubleshooting-documentation.md`
  (this file, living updates)

## Interfaces and dependencies

No new code interfaces are introduced. This task creates only documentation and
documentation-contract tests.

Test dependencies (already available in the project):

- `pytest` for unit tests
- `pytest-bdd` for behavioural tests
- `pathlib.Path` for reading documentation files in tests
