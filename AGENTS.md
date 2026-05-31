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
- **Small, meaningful functions.** Functions should have a clear purpose, single
  responsibility, and obey command/query segregation.
- **Clear commit messages.** Commit messages should be descriptive, explaining
  what was changed and why.
- **Use consistent spelling and grammar.** Comments must use en-GB-oxendict
  ("-ize" / "-yse" / "-our") spelling and grammar, with the exception of
  references to external APIs.
- **Illustrate with clear examples.** Function documentation must include clear
  examples demonstrating usage and outcome. Test documentation should omit
  examples that only restate the test logic.
- **Keep file size manageable.** No single code file should be longer than 400
  lines. Long switch statements or dispatch tables should be broken up by
  feature and constituents colocated with targets. Large blocks of test data
  should be moved to external data files.
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
  relevant file(s) in the `docs/` directory to reflect the latest state.
- **Design decisions:** Record substantive decisions in the relevant design
  document. For major decisions, capture an architectural decision record (ADR)
  and reference it from the design document.
- **User-facing behaviour:** Update [users' guide](docs/users-guide.md) for
  behaviour or user-interface changes that users should know about.
- **Internal interfaces:** Document internally facing interfaces in the relevant
  component architecture document. Record internally facing conventions and
  practices in [developers' guide](docs/developers-guide.md).
- **Style:** All documentation must adhere to the
  [documentation style guide](docs/documentation-style-guide.md).

## Change quality and committing

- **Atomicity:** Aim for small, focused, atomic changes. Each change (and
  subsequent commit) should represent a single logical unit of work.
- **Quality gates:** Before considering a change complete or proposing a
  commit, ensure all of the following are met:
  - New functionality or behaviour changes are fully validated by relevant unit
    and behavioural tests.
  - Bug fixes include a failing test before the fix and a passing test
    afterward.
  - Code passes lint checks.
  - Formatting is correct and validated.
- **For Python files:**
  - **Testing:** Passes all relevant unit and behavioural tests (`make test`).
  - **Linting:** Passes lint checks (`make lint`).
  - **Formatting:** Adheres to formatting standards (`make check-fmt`; use
    `make fmt` to apply fixes).
  - **Typechecking:** Passes type checking (`make typecheck`).
  - For Rust files:
    - **Testing:** Passes relevant unit and behavioural tests (`make test`).
    - **Linting:** Passes lint checks (`make lint`).
    - **Formatting:** Adheres to formatting standards (`make check-fmt`; use
      `make fmt` to apply fixes).
  - **Markdown files (`.md` only):**
    - **Linting:** Passes markdown lint checks (`make markdownlint`).
    - **Mermaid diagrams:** Passes validation using nixie (`make nixie`).
- **Committing:**
  - Only changes that meet all quality gates should be committed.
  - Write clear, descriptive commit messages that summarize the change,
    following:
    - **Imperative mood** in the subject line (for example, "Fix bug", "Add feature").
    - **Subject line length:** around 50 characters or fewer.
    - **Body:** Separate subject from body with a blank line. Explain *what* and
      *why* in wrapped lines (approximately 72 columns).
    - **Formatting:** Use Markdown for formatted text inside the message body.
  - Do not commit changes that fail any quality gate.

## Refactoring heuristics and workflow

- **Recognizing refactoring needs:** regularly assess the codebase for potential
  refactoring opportunities. Consider refactoring when you observe:
  - **Long methods/functions:** functions that are excessively long or try to do
    too many things.
  - **Duplicated code:** identical or very similar code blocks appearing in
    multiple places.
  - **Complex conditionals:** deeply nested or overly complex `if`/`else` or
    `switch` statements.
  - **Large code blocks for single values:** significant logic blocks dedicated
    to calculating or deriving one value.
  - **Primitive obsession / data clumps:** groups of simple variables that are
    frequently passed together, which may indicate a missing abstraction.
  - **Excessive parameters:** functions or methods requiring too many
    parameters.
  - **Feature envy:** methods that focus more on other data than their own.
  - **Shotgun surgery:** one change that forces many files to be edited.
- **Abstraction / port / helper policy:** before adding a new abstraction, port,
  or helper:
  - Sweep the repository to confirm there is no existing equivalent helper,
    port, or abstraction.
  - Document the new abstraction's intended scope and re-use policy.
  - Record the decision in architecture, design, or developers-guide docs using
    `docs/contents.md` as the index.
- **Post-commit review:** after functional changes or bug fixes that meet
  quality gates, review changed code and adjacent areas using these heuristics.
- **Separate atomic refactors:** if refactoring is required, implement it in a
  separate atomic commit after the functional change and ensure it passes all
  relevant gates.

## Python verification and testing

- For Python work, use `pytest` for unit tests and `pytest-bdd` for behavioural
  tests. Cover happy paths, unhappy paths, and relevant edge cases.
- Snapshot tests (using `syrupy`) should be provided where multivariant output
  format consistency is relevant to the requirements.
- Add end-to-end tests where a change affects externally observable workflows,
  integration contracts, persistence, command-line behaviour, network
  boundaries, user interface flows, or other system-level behaviour.
- Use property tests with `hypothesis` or `CrossHair` when a change introduces
  an invariant over a range of inputs, states, orderings, or transitions.
- Run relevant unit, behavioural, property, and end-to-end suites before and
  after each change.

## Rust specific guidance

This repository is written in Rust and uses Cargo for building and dependency
management. Contributors should follow these best practices when working on the
project:

- Run `make check-fmt`, `make lint`, and `make test` before committing. These
  targets wrap the following commands, so contributors understand the exact
  behaviour and policy enforced:
  - `make check-fmt` executes:

    ```sh
    cargo fmt --workspace -- --check
    ```

    validating formatting across the entire workspace without modifying files.
  - `make lint` executes:

    ```sh
    cargo clippy --workspace --all-targets --all-features -- -D warnings
    ```

    linting every target with all features enabled and denying all Clippy
    warnings.
  - `make test` executes:

    ```sh
    cargo test --workspace
    ```

    running the full workspace test suite. Use `make fmt`
    (`cargo fmt --workspace`) to apply formatting fixes reported by the
    formatter check.
- Clippy warnings MUST be disallowed.
- Fix any warnings emitted during tests in code instead of silencing them.
- Where a function is too long, extract meaningfully named helper functions
  adhering to separation of concerns and CQRS.
- Where a function has too many parameters, group related parameters in
  meaningfully named structs.
- Where a function is returning a large error, consider using `Arc` to reduce
  the amount of data returned.
- Ensure that new features are validated with unit and behavioural tests before
  release using `rstest` and `rstest-bdd`. Cover happy paths, unhappy paths,
  and relevant edge cases.
- For Rust: snapshot tests (using `insta`) should be provided where multivariant
  output format consistency is relevant to the requirements.
- Add end-to-end tests where a change affects externally observable workflows,
  integration contracts, persistence, command-line behaviour, network
  boundaries, user interface flows, or other system-level behaviour.
- Use property tests with `proptest` or a bounded model checker such as `kani`
  when a change introduces an invariant over ranges of inputs, states,
  orderings, or transitions and a Rust extension is in scope.
- For Rust extensions, use exhaustive proof with `verus` for introduced lemmas
  or contractual business logic. Proofs should be substantive, rigorous, and
  well-founded.
- Run relevant unit, behavioural, property, model-checking, proof, and
  end-to-end suites before and after each change.
- Every module **must** begin with a module level (`//!`) comment explaining the
  module's purpose and utility.
- Document public APIs using Rustdoc comments (`///`) so documentation can be
  generated with `cargo doc`.
- Prefer immutable data and avoid unnecessary `mut` bindings.
- Use explicit version ranges in `Cargo.toml` and keep dependencies up-to-date.
- Avoid `unsafe` code unless absolutely necessary, and document any usage
  clearly with a `SAFETY` comment.
- Place function attributes **after** doc comments.
- Do not use `return` in single-line functions.
- Use predicate functions for conditional criteria with more than two branches.
- Lints must not be silenced except as a **last resort**.
- Lint rule suppressions must be tightly scoped and include a clear reason.
- Use `concat!()` to combine long string literals rather than escaping
  newlines with a backslash.
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
  `newt-hype` when introducing many homogeneous wrappers that share behaviour.
  Add shims such as `From<&str>` and `AsRef<str>` for string-backed wrappers.
  For path-centric wrappers implement `AsRef<Path>` with `into_inner()` and
  `to_path_buf()`, and avoid `impl From<Wrapper> for PathBuf` due to the orphan
  rule. Prefer explicit tuple structs whenever bespoke validation or trait
  tailoring is needed. Combine approaches: use `newt-hype` for the common case,
  tuple structs for outliers, and `the-newtype` to unify behaviour when
  defining traits across wrappers.
- Use `cap_std` and `cap_std::fs_utf8` / `camino` in place of `std::fs` and
  `std::path` for enhanced cross-platform support and capability oriented
  filesystem access.

### Testing

- Use `pytest` fixtures for shared setup in Python work. Use `rstest` fixtures
  for shared setup in Rust work.
- Replace duplicated tests with `#[rstest(...)]` parameterized cases in Rust and
  equivalent fixture-driven parameterisation in Python.
- Prefer `mockall` for ad hoc mocks and stubs.
- For testing reliant on environment variables, use dependency injection and the
  `mockable` crate where feasible. If mockable cannot be used, environment
  mutations in tests must be wrapped in shared guards in `test_utils` or
  `test_helpers`. Use shared mutexes and avoid direct environment mutation.

### Dependency management

- **Mandate caret requirements for all dependencies.** All crate versions in
  `Cargo.toml` must use semver-compatible caret requirements (for example,
  `some-crate = "1.2.3"`, which is equivalent to `^1.2.3`). This enables safe
  minor and patch updates.
- **Prohibit unstable version specifiers.** Wildcards (`*`) and open-ended
  inequalities (`>=`) are forbidden. Tilde requirements (`~`) may only be used
  with explicit rationale.

### Error handling

- **Prefer semantic error enums.** Derive `std::error::Error` (via `thiserror`)
  for any condition the caller might inspect, retry, or map to an HTTP status.
- **Use an opaque error only at the application boundary.** Use
  `eyre::Report` for human-readable logs; do not expose opaque types in public
  APIs.
- **Do not export the opaque type from libraries.** Convert to domain enums at
  API boundaries and use `eyre` only in the top-level `main` or async
  entrypoint.
- In tests, prefer `.expect(...)` over `.unwrap()` for clearer failure context.
- In production code and shared fixtures, avoid `.expect()` and return `Result`
  with `?` where possible.
- Keep `expect_used` strict; do not suppress the lint.
- `allow-expect-in-tests = true` does not cover helpers outside
  `#[cfg(test)]`/`#[test]`; avoid `expect` in fixtures.
- Use `anyhow`/`eyre` with `.context(...)` to preserve backtraces and create
  clear failure paths.
- Update helpers such as `set_dir` to return errors rather than panicking.
- Consume fallible fixtures in `rstest` by returning `Result` and using `?` in
  fixtures.

### Observability

- Use `tracing` for logging and diagnostics. Prefer structured
  `tracing::{trace, debug, info, warn, error}` events and spans over `println!`,
  `eprintln!`, or direct `log` macros.
- Add `tracing` fields for identifiers, state, and error context to aid
  correlation.
- Use `#[tracing::instrument]` or explicit spans around meaningful units of
  work, such as request handling, command execution, retries, background jobs,
  and similar.
- Do not hold `Span::enter()` guards across `.await`; use
  `Instrument::instrument` or explicit synchronous spans instead.
- Use the `metrics` crate where usage, uptake, failure, or mitigation metrics
  are needed. Prefer `counter!`, `gauge!`, and `histogram!` according to metric
  semantics.
- Describe metrics with `describe_counter!`, `describe_gauge!`, or
  `describe_histogram!` where purpose is not obvious.
- Keep metric names stable and labels low-cardinality; avoid user input, request
  IDs, unbounded paths, or raw error strings.
- Libraries may emit `metrics` and `tracing` instrumentation, but should not
  install global recorders or subscribers.
- Applications should initialize exporters and subscribers early.

## Markdown guidance

- Validate Markdown files using `make markdownlint`.
- Run `make fmt` after documentation changes to format Markdown and fix table
  markup.
- Validate Mermaid diagrams in Markdown by running `make nixie`.
- Markdown paragraphs and bullet points should be wrapped at 80 columns.
- Code blocks should be wrapped at 120 columns.
- Tables and headings should not be wrapped.
- Use dashes (`-`) for list bullets.
- Use GitHub-flavoured Markdown footnotes (`[^1]`) for references and footnotes.

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

## Additional tooling

The following tooling is available in this environment:

- `mbake` — A Makefile validator. Run using `mbake validate Makefile`.
- `strace` — Traces system calls and signals made by a process; useful for
  debugging runtime behaviour and syscalls.
- `gdb` — The GNU Debugger, for inspecting and controlling programs as they
  execute (or post-mortem via core dumps).
- `ripgrep` — Fast, recursive text search tool (`grep` alternative) that
  respects `.gitignore` files.
- `ltrace` — Traces calls to dynamic library functions made by a process.
- `valgrind` — Suite for detecting memory leaks, profiling, and debugging
  low-level memory errors.
- `bpftrace` — High-level tracing tool for eBPF, using a custom scripting
  language for kernel and application tracing.
- `lsof` — Lists open files and the processes using them.
- `htop` — Interactive process viewer (visual upgrade to `top`).
- `iotop` — Displays and monitors I/O usage by processes.
- `ncdu` — NCurses-based disk usage viewer for finding large files/folders.
- `tree` — Displays directory structure as a tree.
- `bat` — `cat` clone with syntax highlighting, Git integration, and paging.
- `delta` — Syntax-highlighted pager for Git and diff output.
- `tcpdump` — Captures and analyses network traffic at the packet level.
- `nmap` — Network scanner for host discovery, port scanning, and service
  identification.
- `lldb` — LLVM debugger, alternative to `gdb`.
- `eza` — Modern `ls` replacement with more features and better defaults.
- `fzf` — Interactive fuzzy finder for selecting files and commands.
- `hyperfine` — Command-line benchmarking tool with statistical output.
- `shellcheck` — Linter for shell scripts.
- `fd` — Fast user-friendly `find` alternative with sensible defaults.
- `checkmake` — Linter for `Makefile`s, ensuring best practices.
- `srgn` — Structural grep and syntax-tree pattern editing.
- `difft` **(Difftastic)** — Semantic diff tool that compares code structure.

## Key takeaway

These practices help maintain a high-quality codebase and facilitate
collaboration.
