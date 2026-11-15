# Assistant instructions

## Code style and structure

- **Code is for humans.** Write code with clarity and empathy—assume a
  tired teammate will need to debug it at 3 a.m.
- **Comment *why*, not *what*.** Explain assumptions, edge cases, trade-offs,
  or complexity. Don't echo the obvious.
- **Clarity over cleverness.** Be concise, but favour explicit over terse or
  obscure idioms. Prefer code that's easy to follow.
- **Use functions and composition.** Avoid repetition by extracting reusable
  logic. Prefer generators or comprehensions, and declarative code to
  imperative repetition when readable.
- **Small, meaningful functions.** Functions must be small, clear in purpose,
  single responsibility, and obey command/query segregation.
- **Clear commit messages.** Commit messages should be descriptive, explaining
  what was changed and why.
- **Use consistent spelling and grammar.** Code identifiers, comments,
  docstrings, and prose must use en-GB-oxendict ("-ize" / "-yse" / "-our")
  spelling and grammar, except where an external API requires another spelling.
  The spelling gate, which `make lint` and `make markdownlint` both run, checks
  every tracked file. See [Spelling](#spelling).
- **Document public APIs comprehensively.** Public functions, classes, and
  methods must have comprehensive NumPy-style docstrings, including clear
  examples that demonstrate usage and outcome where appropriate.
- **Keep private helper docstrings concise.** Prefer single-line docstrings for
  private helpers. When a private helper needs an explanatory paragraph,
  inspect whether that need exposes conflated responsibilities, an unclear
  command/query boundary, or another CQRS or cohesion failure:
  - Split the helper when it performs distinct query and command
    responsibilities or combines unrelated concerns.
  - Extract a focused helper when doing so makes the invariant or boundary
    local and simpler.
  - Keep the helper intact when its responsibility is cohesive and the
    explanation documents an unavoidable local constraint; retain the
    paragraph in that case.
- **Structure private helper docstrings selectively.** Use structured
  NumPy-style sections for private helpers only when they describe non-obvious
  behaviour.
- **Keep test documentation meaningful.** Test documentation should omit
  examples that only restate the test logic.
- **Keep file size manageable.** No single code file may be longer than 400
  lines. Long switch statements or dispatch tables should be broken up by
  feature and constituents colocated with targets. Large blocks of test data
  should be moved to external data files. The Pylint gate enforces the limit
  for production and benchmark sources; the test-only trees listed in
  `PYLINT_TEST_TARGETS` in the `Makefile` are exempt from its `too-many-lines`
  check.
- **Name things precisely.** Use clear, descriptive variable and function names.
  For booleans, prefer names with `is`, `has`, or `should`.
- **Structure logically.** Each file should encapsulate a coherent module. Group
  related code (for example, models + utilities + fixtures) close together.
- **Group by feature, not layer.** Colocate views, logic, fixtures, and helpers
  related to a domain concept rather than splitting by type.
- **Use clear file boundaries.** Each module, component, and package should have
  an obvious responsibility and avoid accidental coupling.

## Documentation maintenance

- **Reference:** Use the markdown files within the `docs/` directory as a
  knowledge base and source of truth for project requirements, dependency
  choices, and architectural decisions. Start with
  [documentation contents](docs/contents.md) and
  [repository layout](docs/repository-layout.md) when orienting within the
  project.
- **Update:** When new decisions are made, requirements change, libraries are
  added/removed, or architectural patterns evolve, **proactively update** the
  relevant file(s) in the `docs/` directory to reflect the latest state. Ensure
  the documentation remains accurate and current.
- **Design decisions:** Record design decisions in the relevant design
  document. When a decision is substantive, capture it in an Architectural
  Decision Record (ADR) following the documentation style guide, and reference
  that ADR from the design document.
- **User-facing behaviour:** Update [users' guide](docs/users-guide.md) for
  behaviour or user-interface changes that users should know about. Add
  migration guidance for compatibility or usage changes.
- **Internal interfaces:** Document internally facing interfaces in the
  relevant component architecture document. Document internally facing
  conventions and practices in [developers' guide](docs/developers-guide.md).
- **Build tooling:** When changing build tooling, document the effective
  toolchain, inherited flags, backend and linker selection, and deliberate
  differences between validation commands.
- **Style:** All documentation must adhere to the
  [documentation style guide](docs/documentation-style-guide.md).
- **Accuracy:** Verify changed examples, commands, imports, links, paths, and
  stated guarantees against the implementation. Update maintained copies and
  indexes when names or locations change. Keep transient validation results in
  the PR or execution record.
- **Decision history:** Preserve accepted ADR decisions; record subsequent
  changes through the
  [ADR amendment and supersession process](docs/documentation-style-guide.md#architectural-decision-records-adrs).
  Capture proof-driven API, representation, and invariant decisions in the
  design documentation.
- **Completed execplans:** Completed execplans are historical documents and
  should reflect the state of the repository at time of implementation. Do not
  retroactively update completed execplan documents to reflect subsequent
  changes in API, structures, or behaviour.

## Planning and verification

- For non-trivial work, prepare or update a self-contained ExecPlan using the
  available planning guidance. Identify the outcome, relevant roadmap and
  design references, prerequisites, scope exclusions, and observable acceptance
  criteria. Signpost relevant documentation and skills.
- Identify affected behavioural contracts, invariants, and failure modes before
  choosing the implementation. Use these to pressure-test the proposed API and
  verification strategy.
- Plan the smallest useful increment. Preserve decision gates and stopping
  conditions; defer abstractions, dependencies, macros, or published components
  until the preceding evidence justifies them.
- Where formal verification is selected, first sketch the independent model,
  premises, invariants, representation relationships, and principal proof
  obligations. Treat proposed lemmas as provisional. Defer detailed proof
  engineering until implementation exposes the verification conditions; do not
  add assumptions, admissions, or trusted proof bodies merely to make
  incomplete proofs pass. Unproved statements may remain in the plan until that
  milestone.
- Give each major milestone explicit acceptance commands, including applicable
  proof checks and the formatting, linting, and test gates. Keep commits
  coherent and useful as rollback points.
- When plan approval is required, deliver the plan for review and wait for
  approval before implementation. Keep the active plan current, and mark
  relevant roadmap entries complete only after their acceptance criteria pass.

## Change quality & committing

- **Atomicity:** Aim for small, focused, atomic changes. Each change (and
  subsequent commit) should represent a single logical unit of work.
- **Quality Gates:** Before considering a change complete or proposing a commit,
  ensure it meets the following criteria:
  - New functionality or changes in behaviour are fully validated by relevant
    unit tests and behavioural tests.
  - Where a bug is being fixed, a unittest has been provided demonstrating the
    behaviour being corrected both to validate the fix and to guard against
    regression.
  - Passes all relevant unit and behavioural tests according to the guidelines
    above.
  - Passes lint checks.
  - Adheres to formatting standards tested using a formatting validator.
- **Before review:** Inspect the complete diff, including tests, fixtures,
  scripts, examples, and documentation. Check file-size limits, module
  documentation, doc-comment placement, assertion diagnostics, duplicated test
  setup, and applicable spelling and formatting gates. Link relevant roadmap
  entries, issues, and the ExecPlan in the PR description.
- **For Python files:**
  - **Testing:** Passes all relevant unit and behavioural tests (`make test`).
  - **Linting:** Passes lint checks (`make lint`), including the blocking
    Skylos production dead-code scan. Investigate every finding and remove
    genuine dead code. For a verified false positive, first model an implicit
    runtime caller with a precise, typed entry-point rule in
    `[tool.skylos.dead_code]`; use `type = "method"` for methods and include
    the fully qualified symbol plus a caller-specific reason. Only when an
    entry-point rule cannot describe the boundary may a named allow-list
    exception be added with
    `make skylos-allow SYMBOL=handler REASON="Loaded by plugin registry"`.
    Both `SYMBOL` and `REASON` are required and reject empty or
    whitespace-only values. Use `SYMBOL` rather than `NAME` because WSL may
    inject `NAME` with the hostname.
    The installed `df12-skylos allow` command serializes allow-list updates
    with the ignored `.pyproject.toml.df12.lock` file, so concurrent
    invocations do not overwrite one another.
  - **Duplication:** Passes the blocking nose code-duplication gate
    (`make duplication`), which `make lint` runs. Treat every new finding as
    copy-paste until proven otherwise: prefer extracting the smallest coherent
    shared implementation at the correct architectural layer over suppressing
    the report. Never manufacture a generic utility, an inheritance hierarchy,
    a boolean-mode helper, or a cross-layer dependency to satisfy the detector,
    and never lower the enforced surface or raise the size floor to obtain a
    green run. Where the parallel structure is deliberate, record a
    per-location reasoned exception with
    `make duplication-allow FIRST='path[::name]' [MEMBERS='path[::name] ...'] REASON='...'`.
    A single exception must cover *every* location of the family; a
    repository-wide wildcard, a mass-generated reason, or automatic allowlisting
    of a new scan's findings is not acceptable. `FIRST`, `MEMBERS`, and `REASON`
    are read only from the Make command line, never from the ambient
    environment. Never delete a stale entry merely because no family in the
    scan matched it: the scan is untruncated, but absence from any report is not
    proof that the duplication is gone, and a stale finding must be confirmed
    against the source first.
  - **Formatting:** Adheres to formatting standards (`make check-fmt`; use
    `make fmt` to apply fixes).
  - **Typechecking:** Passes type checking (`make typecheck`).
- For Rust files:
  - **Testing:** Passes relevant unit and behavioural tests (`make test`).
  - **Linting:** Passes lint checks (`make lint`), including the blocking
    Skylos production dead-code scan.
  - **Formatting:** Adheres to formatting standards (`make check-fmt`; use
    `make fmt` to apply fixes).
- For Markdown files (`.md` only):
  - **Linting:** Passes markdown lint checks (`make markdownlint`).
  - **Spelling:** Passes the en-GB-oxendict spelling gate, which
    `make markdownlint` and `make lint` both run.
  - **Mermaid diagrams:** Passes validation using nixie (`make nixie`).
- **Committing:**
  - Only changes that meet all the quality gates above should be committed.
  - Write clear, descriptive commit messages summarizing the change, following
    these formatting guidelines:
    - **Imperative mood:** Use the imperative mood in the subject line (for
      example, "Fix bug", "Add feature" instead of "Fixed bug", "Added feature").
    - **Subject line:** The first line should be a concise summary of the change
      (ideally 50 characters or fewer).
    - **Body:** Separate the subject from the body with a blank line. Subsequent
      lines should explain the *what* and *why* of the change in more detail,
      including rationale, goals, and scope. Wrap the body at 72 characters.
    - **Formatting:** Use Markdown for any formatted text (like bullet points or
      code snippets) within the commit message body.
  - Do not commit changes that fail any of the quality gates.

## Refactoring heuristics & workflow

- **Recognizing refactoring needs:** Regularly assess the codebase for potential
  refactoring opportunities. Perform refactoring when observing:
  - **Long methods/functions:** Functions or methods that are excessively long
    or try to do too many things.
  - **Duplicated code:** Identical or very similar code blocks appearing in
    multiple places. The nose duplication gate in `make lint` reports these
    mechanically; see the duplication bullet under "Change quality & committing"
    for how to respond to a finding.
  - **Complex conditionals:** Deeply nested or overly complex `if`/`else` or
    `switch` statements (high cyclomatic complexity).
  - **Large code blocks for single values:** Significant chunks of logic
    dedicated solely to calculating or deriving a single value.
  - **Primitive obsession / data clumps:** Groups of simple variables (strings,
    numbers, booleans) that are frequently passed around together, often
    indicating a missing class or object structure.
  - **Excessive parameters:** Functions or methods requiring a very long list of
    parameters.
  - **Feature envy:** Methods that seem more interested in the data of another
    class/object than their own.
  - **Shotgun surgery:** A single change requiring small modifications in many
    different classes or functions.
- **Abstraction / port / helper policy:** Before implementing an abstraction,
  port, or extracted helper, the agent must:
  1. Sweep the repository and approved dependencies for equivalent behaviour.
     Reuse it unless demonstrably unsuitable. New abstractions must simplify
     the current change or have a concrete reuse case.
  2. Document the new abstraction's intended scope and re-use policy
     (ownership boundaries, permitted call-sites, and composition rules).
  3. Record the decision in the appropriate architecture, design, or
     developers-guide document using `docs/contents.md` as the index to select
     the correct location.
- **Post-commit review:** After committing a functional change or bug fix (that
  meets all quality gates), review the changed code and surrounding areas using
  the heuristics above.
- **Separate atomic refactors:** If refactoring is deemed necessary:
  - Perform the refactoring as a **separate, atomic commit** *after* the
    functional change commit.
  - Ensure refactoring adheres to the testing guidelines (behavioural tests
    pass before and after, unit tests added for new units).
  - Ensure the refactoring commit itself passes all quality gates.

## Architecture and operational boundaries

- Keep domain policy separate from filesystem, process, network, framework,
  and telemetry adapters. Inject dependencies where needed through existing
  interfaces or narrow closures; avoid speculative layers and dependency cycles.
- Make fallibility, mutation, and side-effects explicit in APIs. Preserve I/O,
  parsing, and permission errors rather than silently converting them into
  absence, `false`, or empty success. Compose side-effects at orchestration
  boundaries; queries must not hide writes or unrelated workflow policy.
- For streams, subprocesses, retries, caches, queues, and background tasks,
  define applicable size limits, deadlines, ownership, cancellation, and
  cleanup. A timeout does not bound accumulated bytes. Include waiting and
  cleanup in the operation's budget. Define partial-failure and retry behaviour
  where updates span multiple effects; keep locks narrow and ordered, avoiding
  holds across awaits or external calls unless explicitly justified.
- Validate untrusted inputs before privileged effects, use structured APIs
  instead of executable string construction, and grant only required
  permissions.
- When performance materially motivates a change, measure realistic inputs and
  distinguish build, execution, queueing, and cache state. Avoid repeated I/O
  or unnecessary allocation in per-item loops and blocking work on async
  executors.

## Python verification and testing

- For Python work, use `pytest` for unit tests and `pytest-bdd` for behavioural
  tests. Cover happy paths, unhappy paths, and relevant edge cases.
- Use `pytest` fixtures for shared setup in Python work, and fixture-driven
  parameterization in place of duplicated Python tests.
- Snapshot tests (using `syrupy`) should be provided where multivariant output
  format consistency is relevant to the requirements.
- Snapshot tests must be meaningful, focused, and paired with semantic
  assertions for behaviour that should not be hidden inside a blob comparison.
  Avoid generic snapshots that only record "whatever happened"; redact
  nondeterministic fields such as paths, timestamps, random IDs, durations, and
  toolchain-specific noise; and keep compiler or UI snapshots narrow enough
  that legitimate formatting or dependency changes do not create brittle
  failures.
- Add end-to-end tests where a change affects externally observable workflows,
  integration contracts, persistence, command-line behaviour, network
  boundaries, user interface flows, or other system-level behaviour.
- Use property tests with `hypothesis` or `CrossHair` when a change introduces
  an invariant over a range of inputs, states, orderings, or transitions.
- Run relevant unit, behavioural, property, and end-to-end suites before and
  after each change.

## Rust specific guidance

This repository includes Rust components and uses Cargo for Rust building and
dependency management. Contributors should follow these best practices when
working on the Rust portions of the project:

- Run `make check-fmt`, `make lint`, and `make test` before committing Rust
  changes. These unified root targets run the Python gates and the following
  Rust commands, so contributors understand the exact behaviour and policy
  enforced:
  - `make check-fmt` runs `ruff format --check` and `ruff check --select I`
    for Python, `mdtablefix --check` for Markdown, and the Rust formatter check
    below. The Rust check runs from the `rust` directory without modifying
    files:

    ```sh
    cargo +nightly-2026-05-28 fmt --all -- --check
    ```

  - `make lint` runs `python-lint`, then `rust-lint`, then
    `github-actions-lint`. `rust-lint` runs `lint-clippy`, `lint-whitaker`, and
    the spelling gate in sequence. The Rustdoc and Clippy leaf runs through
    `RUST_DEBUG_CARGO`; Whitaker is a separate fragment-free leaf:

    ```sh
    cd rust && RUSTDOCFLAGS="$(RUSTDOC_FLAGS)" \
      $(RUST_DEBUG_CARGO) doc --no-deps && \
      $(RUST_DEBUG_CARGO) clippy $(CLIPPY_FLAGS)
    cd rust && $(LOCAL_TOOL_ENV) \
      RUSTFLAGS="$(WHITAKER_RUSTFLAGS)" $(WHITAKER) --all -- \
      $(WHITAKER_CARGO_FLAGS)
    yamllint --strict --config-file .yamllint.yml .github/workflows .github/actions
    actionlint -config-file .github/actionlint.yaml -shellcheck=
    ```

    On Linux, `RUST_DEBUG_CARGO` selects `nightly-2026-08-23` with the explicit
    `tools/dev-fast/config.toml` fragment; elsewhere it runs plain `cargo`.
    Whitaker never receives that fragment. The protected coverage, release,
    verification, and MSRV paths also retain their separate toolchain and
    linker policy. The target lints every target with all features enabled,
    denies all Clippy warnings, and validates GitHub Actions workflows and the
    composite actions they call. Keep `.yamllint.yml` compatible with GitHub's
    unquoted `on` trigger key and require each workflow and composite action to
    begin with `---`. CI must install yamllint with `uv tool` and use the
    pinned, checksum-verified actionlint binary before invoking `make lint`.
    Pass `-shellcheck=` so the gate needs no shellcheck binary and cannot reach
    the v1.7.12 stdin deadlock (rhysd/actionlint#702, #704, #712); a clean
    `main` can hang the same way.
  - `make test` runs the Python suite first (`test-selection` and
    `test-python`), then `test-rust`. The Rust suite runs `cargo nextest run`
    when `cargo-nextest` is available and fails if that tool is older than
    0.9.100. Otherwise it falls back to `cargo test`. Both run with
    `--all-targets --all-features`, `--frozen`, and one test thread by default
    (`TEST_JOBS`):

    ```sh
    cargo nextest run --all-targets --all-features --test-threads 1 --frozen
    cargo test --all-targets --all-features --frozen -- --test-threads=1
    ```

    The Rust suite sets `RUSTFLAGS="-D warnings -C codegen-units=1"`. On Linux,
    the dev-fast route also uses the dev-fast nightly and adds
    `-Zthreads=8 -Clink-arg=-fuse-ld=mold`; `make dev-build` sets no `RUSTFLAGS`
    of its own. The suite then runs Cargo doctests
    separately with all features, because Nextest does not execute doctests:
    `cargo test --workspace --doc --all-features --frozen`, with
    `RUSTDOCFLAGS="--cfg docsrs -D warnings"` and, on Linux, the extra
    doctest-warning options. Use `make fmt`
    (`cargo +nightly-2026-05-28 fmt --all`) to apply formatting fixes reported
    by the formatter check alongside the Python and Markdown formatters.
- The project toolchain stays pinned to Rust `1.87.0` and declares `rustfmt`,
  `clippy`, and `rust-analyzer`. CI also provisions `nightly-2026-05-28` for
  the maintenance formatter, then restores the project toolchain. The current
  `fmt` and `check-fmt` recipes explicitly select the pinned nightly only for
  formatting. Linux-only debug Rust work additionally uses the separately
  pinned dev-fast nightly through `tools/dev-fast/config.toml`. The Makefile
  runs `make dev-fast-check` as a prerequisite of those routes, and that check
  requires the `clippy` component. LLVM is the default backend. Cranelift is
  disabled and opt-in only: its `[profile.dev]` selection is commented out in
  the fragment, and one invocation can opt in with
  `CARGO_PROFILE_DEV_CODEGEN_BACKEND=cranelift` (see
  [concordat ADR-0004](https://github.com/leynos/concordat/blob/main/docs/adrs/adr-0004-cranelift-is-not-part-of-the-rust-build-standard.md)).
  Cranelift builds do not unwind. Release, coverage, Whitaker, MSRV, and
  formal-verification builds keep the LLVM backend. The stable toolchain
  remains mandatory for release, coverage, Whitaker, the explicit
  `make msrv-check` verification target, and the macOS and Windows platform
  alternatives. All Rust packages inherit Cargo's `rust-version = "1.87.0"`
  contract, which also keeps MSRV-aware Clippy findings valid when debug
  linting uses the dev-fast nightly.
  - `make modern-stable` is a separate, explicitly selected validation route
    pinned to Rust/Cargo `1.99.1`; it does not change the Rust 1.87.0 source
    contract or MSRV check. Its check, Clippy, test, and Maturin commands use
    `CARGO_BUILD_WARNINGS=deny` on Cargo 1.97 or later with a dedicated
    `rust/target/modern-stable` directory. Rustdoc and doctests retain
    `RUSTDOC_FLAGS`, and the older-Cargo, dev-fast, release, Whitaker,
    verification, and wheel routes retain their existing warning policy. In
    particular, dev-fast continues to use `-D warnings`. Keep the Makefile,
    CI, developer guide, toolchain contract, and compiler-cache pin in sync
    when updating the modern toolchain.
- Clippy warnings MUST be disallowed.
- Fix any warnings emitted during tests in the code itself rather than
  silencing them.
- Every `assert!`, `assert_eq!`, and `assert_ne!` invocation must include a
  custom diagnostic message that states the failed expectation and, where
  useful, the relevant values.
- Where a function is too long, extract meaningfully named helper functions
  adhering to separation of concerns and CQRS.
- Where a function has too many parameters, group related parameters in
  meaningfully named structs.
- Keep returned error types semantically focused and small. If a necessary
  payload makes an error variant large, place that payload behind `Box`; use
  `Arc` only when shared ownership or cheap cloning is required.
- Ensure that new features are validated with unit tests using `rstest` and
  behavioural tests using `rstest-bdd` where applicable. Cover happy paths,
  unhappy paths, and relevant edge cases.
- Add snapshot tests using `insta` where structured output, user-interface
  output, diagnostics, or other multivariant output formats need meaningful,
  stable assertions. Keep snapshots focused on stable output boundaries, pair
  them with semantic assertions for business rules and schema contracts, and
  normalize nondeterministic fields before snapshotting. Do not accept brittle
  snapshots that churn after unrelated changes; update snapshots only after
  confirming a snapshot failure identifies a real contract change.
- Add compile-time behaviour tests where macro expansion, trait bounds,
  feature-gated APIs, or other compile-fail and compile-pass contracts are
  relevant to the requirements. Do not use `trybuild`:
  [ADR-0027](docs/adrs/adr-0027-compile-contracts-in-the-build-graph.md)
  removed it because each case builds a scratch crate that cannot reuse the
  workspace's build. Express compile-pass cases as ordinary doctests, and
  compile-fail cases as `compile_fail` doctests that name an error code and
  share their doc comment with a positive control. Keep compile-time fixtures
  free of unrelated runtime work.
- Add end-to-end tests where a change affects externally observable workflows,
  integration contracts, persistence, command-line behaviour, network
  boundaries, UI flows, or other system-level behaviour.
- Select verification proportionately: named examples or `rstest` tables for
  finite contracts, `proptest` for broad input spaces or operation sequences,
  and `kani` for exhaustive exploration within explicit bounds. Use `verus` for
  selected lemmas or contractual invariants requiring unbounded proof over a
  suitable semantic model. Proofs must establish the property from appropriate
  premises, not restate or assume it. Straightforward integration and
  presentation work must not acquire artificial proof obligations. Keep
  executable integration and behavioural tests alongside proofs.
- Run relevant unit, behavioural, property, model-checking, proof, and
  end-to-end suites before and after making any change.
- Every module **must** begin with a module level (`//!`) comment explaining the
  module's purpose and utility, including test modules and fixture crate roots.
- Document public APIs using Rustdoc comments (`///`) so documentation can be
  generated with cargo doc.
- Prefer immutable data and avoid unnecessary `mut` bindings.
- Clone to express intentional ownership, sharing, or snapshot semantics;
  prefer borrowing or ownership transfer when a new owned value is unnecessary.
- Use explicit version ranges in `Cargo.toml` and keep dependencies up-to-date.
- Avoid `unsafe` code unless absolutely necessary, and document any usage
  clearly with a "SAFETY" comment.
- Place function attributes **after** doc comments.
- Do not use `return` in single-line functions.
- Use predicate functions for conditional criteria with more than two branches.
- Lints must not be silenced except as a **last resort**.
- Lint rule suppressions must be tightly scoped and include a clear reason.
- Use item-scoped `#[expect(lint, reason = "...")]`; blanket allowances and
  artificial references that hide unused code are forbidden. Narrow feature and
  test-support compilation boundaries instead. Delete unused helpers unless
  concrete tracked work justifies a temporary expectation linked to that work.
- Use `concat!()` to combine long string literals rather than escaping newlines
  with a backslash.
- Prefer single line versions of functions where appropriate. For example:

  ```rust
  pub fn new(id: u64) -> Self { Self(id) }
  ```

  Instead of:

  ```rust
  pub fn new(id: u64) -> Self {
      Self(id)
  }
  ```

- Use NewTypes to model domain values and eliminate "integer soup". Reach for
  `newt-hype` when introducing many homogeneous wrappers that share behaviour;
  add small shims such as `From<&str>` and `AsRef<str>` for string-backed
  wrappers. For path-centric wrappers implement `AsRef<Path>` alongside
  `into_inner()` and `to_path_buf()`. Prefer explicit tuple structs whenever
  bespoke validation or tailored trait surfaces are required, customizing
  `Deref`, `AsRef`, and `TryFrom` per type. Use `the-newtype` when defining
  traits and needing blanket implementations that apply across wrappers
  satisfying `Newtype + AsRef/AsMut<Inner>`, or when establishing a coherent
  internal convention that keeps trait forwarding consistent without per-type
  boilerplate. Combine approaches: lean on `newt-hype` for the common case,
  tuple structs for outliers, and `the-newtype` to unify behaviour when owning
  the trait definitions.
- Use `cap_std` and `cap_std::fs_utf8` / `camino` in place of `std::fs` and
  `std::path` for enhanced cross-platform support and capabilities oriented
  filesystem access.

### Testing

- For each changed behaviour, identify a plausible incorrect implementation
  and ensure the test rejects it. Assert observable results, errors, and
  side-effects; mock calls or configuration text alone do not prove execution.
  Negative tests must establish the precondition that would expose the defect.
- Exercise the boundary that owns the promise: public API, executable, or
  protocol adapter. Private-helper tests complement this coverage. Fakes must
  support relevant failures and verify their propagation through orchestration.
  Test local integration contracts rather than third-party tools themselves.
- Test relevant partial failures, cancellation, shutdown, and cleanup with
  deterministic coordination where practical. Avoid relying solely on sleeps or
  exact deltas from shared global counters. Restore fixture resources even when
  assertions fail, and account for host capabilities and privileges.
- Use `rstest` fixtures for shared setup.
- Replace duplicated tests with `#[rstest(...)]` parameterized cases.
- Prefer `mockall` for ad hoc mocks/stubs.
- **Inject the environment instead.** Any behaviour depending upon environment
  variables must accept an injected environment: `mockable::Env`, with
  `mockable::DefaultEnv` supplied at the production composition root and
  `mockable::MockEnv` in tests. A narrow closure seam of the same shape is
  acceptable where a trait object would be disproportionate. Production code
  must not reach for `std::env::var`/`var_os` outside that composition root.
  The Loom harness in `rust/cuprum-rust/tests/loom.rs` is the local exception
  that the repository accepts: it only reads `LOOM_MAX_THREADS`, which the Loom
  driver sets for each run, and never mutates the environment.
- **Process-wide locks are not an escape hatch.** Serializing environment
  mutation behind a shared `Mutex`, `OnceLock`, or `serial_test` attribute is
  not an accepted fallback. It serializes the suite, defeats property-based
  tests that run many cases within one process, and leaves the ambient
  dependency embedded in the production signature. Where injection looks
  impossible the signature is wrong; correct the signature.
- **Subprocess isolation is the sole exemption.** End-to-end behavioural tests
  invoking the generated application binary through `assert_cmd` may configure
  the environment via `Command::env`/`Command::env_clear`, because the mutation
  is confined to the child process. A test manipulating the environment
  in-process does not qualify, whatever layer it claims to occupy.

### Dependency management

- **Mandate caret requirements for all dependencies.** All crate versions
  specified in `Cargo.toml` must use SemVer-compatible caret requirements (e.g.,
  `some-crate = "1.2.3"`, equivalent to `^1.2.3`). This is Cargo's default and
  allows for safe, non-breaking updates to minor and patch versions while
  preventing breaking changes from new major versions. This approach is
  critical for ensuring build stability and reproducibility.
- **Prohibit unstable version specifiers.** The use of wildcard (`*`) or
  open-ended inequality (`>=`) version requirements is strictly forbidden, as
  they introduce unacceptable risk and unpredictability. Tilde requirements
  (`~`) should only be used where a dependency must be locked to patch-level
  updates for a specific, documented reason.

### Error handling

- **Prefer semantic error enums**. Derive `std::error::Error` (via the
  `thiserror` crate) for any condition the caller might inspect, retry, or map
  to an HTTP status.
- **Use an *opaque* error only at the app boundary**. Use `eyre::Report` for
  human-readable logs; these should not be exposed in public APIs.
- **Never export the opaque type from a library**. Convert to domain enums at
  API boundaries, and to `eyre` only in the main `main()` entrypoint or
  top-level async task.
- In tests, prefer `.expect(...)` over `.unwrap()` to surface clearer failure
  diagnostics.
- In production code and shared fixtures, avoid `.expect()` entirely: return
  `Result` and use `?` to propagate errors instead of panicking.
- Keep `expect_used` **strict**; do not suppress the lint.
- Recognize that `allow-expect-in-tests = true` **doesn’t cover** helpers
  outside `#[cfg(test)]` or `#[test]`; avoid `expect` in such fixtures.
- Use `anyhow`/`eyre` with `.context(...)` to **preserve backtraces** and
  provide clear, typed failure paths.
- Update helpers (e.g., `set_dir`) to **return errors** rather than panicking.
- Consume fallible fixtures in `rstest` by **making the test return `Result`**
  and applying `?` to the fixture.

### Observability

- Use `tracing` for logging and diagnostics. Prefer structured
  `tracing::{trace, debug, info, warn, error}` events and spans over `println!`,
  `eprintln!`, or direct `log` macros. Add fields for identifiers, state, and
  error context so downstream subscribers can filter and correlate events
  without parsing message text.
- Use `#[tracing::instrument]` or explicit spans around request handling,
  command execution, retries, background jobs, and other meaningful units of
  work. Do not hold `Span::enter()` guards across `.await`; use
  `Instrument::instrument` or scoped synchronous spans instead.
- Use the `metrics` crate for metric emission where usage, uptake, failure,
  or mitigation metrics are required. Prefer `counter!` for cumulative events,
  `gauge!` for values that rise and fall, and `histogram!` for distributions
  such as latency or payload size.
- Describe emitted metrics with `describe_counter!`, `describe_gauge!`, or
  `describe_histogram!` where the unit or purpose is not obvious from the
  metric name. Keep metric names stable and labels low-cardinality; do not put
  user input, request IDs, paths with unbounded parameters, or raw error
  strings into labels.
- Libraries may emit `metrics` and `tracing` instrumentation, but must not
  install global recorders or subscribers. Applications should initialize
  exporters/subscribers once, as early as practical in startup.
- Keep secrets, credentials, raw payloads, and sensitive paths out of logs,
  errors, traces, and snapshots. Prefer stable error categories and bounded
  contextual fields over arbitrary error text.
- For new operational telemetry, document and exercise the application's
  collection path. Verify a relevant event produces a non-zero signal through
  that path; helper-only assertions or comparisons of zero counters do not
  establish useful instrumentation.

## Markdown guidance

- Validate Markdown files using `make markdownlint`; this also runs the
  spelling gate across every tracked file.
- Run `make fmt` after any documentation changes to format all Markdown
  files and fix table markup.
- Validate Mermaid diagrams in Markdown files by running `make nixie`.
- Markdown paragraphs and bullet points must be wrapped at 80 columns.
- Code blocks must be wrapped at 120 columns.
- Tables and headings must not be wrapped.
- Use dashes (`-`) for list bullets.
- Use GitHub-flavoured Markdown footnotes (`[^1]`) for references and
  footnotes.
- Avoid first-person prose outside README and BDD feature files; allow
  second-person prose in README, AGENTS, BDD feature files, and agent skill
  directories. These exceptions qualify the documentation style guide.
- Quoted API names and identifiers retain upstream spelling. Mark them as
  code using backticks or fenced blocks rather than adding spelling exceptions.
- Enforce spelling through the spelling gate. It regenerates the generated
  spelling configuration from the live shared estate dictionary and the
  `typos.local.toml` overlay on every run, so that generated file is never
  drift checked in Continuous Integration (CI). Put narrow repository-only
  exceptions for unavoidable external contracts in `typos.local.toml`, document
  the upstream contract beside each exception, and never edit generated entries
  by hand.
- Masks that must relax documentation without relaxing source, such as fenced
  code blocks, belong under `[patterns] markdown_only` in `typos.local.toml`.
  Architecture decision record (ADR) 009 keeps Oxford spelling in source
  identifiers, so that mask is rendered under a Markdown-scoped table rather
  than the default one. Inline code spans stay checked everywhere; give a
  backticked identifier its own narrow `ignore` entry instead.
- Shared `[phrases.corrections]` policy is enforced by the gate's phrase stage
  before Typos. Correct genuine maintained prose rather than masking it.

## Project documentation

Record design decisions in the design document. Where a decision is
substantive, record it in an ADR document following the documentation style
guide, then reference that ADR from the design document.

Update `docs/users-guide.md` for any change to application behaviour or user
interface that users should know about. Document internally facing interfaces
or practices in the relevant component architecture document. Document
internally facing conventions or practices in `docs/developers-guide.md`.

## Python development guidelines

For Python development, refer to the detailed guidelines in the `.rules/`
directory:

- [Python code style guidelines](.rules/python-00.md) - Core Python 3.13 style
  conventions.
- [Python context managers](.rules/python-context-managers.md) - Best practices
  for context managers.
- [Python exceptions and logging][python-exceptions] -
  Raising and handling exceptions and logging.
- [Python generators](.rules/python-generators.md) - Generator and iterator
  patterns.
- [Python project configuration](.rules/python-pyproject.md) -
  `pyproject.toml` and packaging.
- [Python return patterns](.rules/python-return.md) - Function return
  conventions.
- [Python typing](.rules/python-typing.md) - Type annotation best practices.

[python-exceptions]: .rules/python-exception-design-raising-handling-and-logging.md

Additional docs:

- [Scripting standards](docs/scripting-standards.md) - Guidance for writing
  robust scripts, including secure command execution via `cuprum`, catalogue
  allowlisting, and command mocking patterns with `cmd-mox`.
- Before adding or updating helper scripts, read the scripting standards guide
  and follow its `Cyclopts`, `cuprum`, `pathlib`, and `cmd-mox` conventions.

<!-- typos-config-builder:agents-md:start -->

## Spelling

- `make spelling` runs the pinned `typos-config-builder gate`, which
  regenerates `typos.toml` from the shared en-GB-oxendict dictionary and
  `typos.local.toml`, then checks spelling and the shared phrase corrections.
- `typos.toml` is generated: never edit it by hand. Put narrow
  repository-specific exceptions in `typos.local.toml`, as exact or full-line
  patterns rather than bare accepted words.
- When `make spelling` changes `typos.toml`, commit the regenerated file. If
  the change is unrelated to your work, commit it in a separate base pull
  request and stack your branch on it, so each review diff stays focused.

<!-- typos-config-builder:agents-md:end -->
