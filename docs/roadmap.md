# Cuprum roadmap

This roadmap translates the design into actionable increments without calendar
commitments. Phases are ordered and build on one another. Steps group related
workstreams. Tasks are measurable and must meet acceptance criteria to count as
done.

## 1. Foundation

Focus: Establish the typed core, scoped safety model, and reliable
single-command execution.

### 1.1. Typed command core

- [x] 1.1.1. Introduce the `Program` NewType and a curated catalogue module with
  a default allowlist; block unknown executables by default.
  - [x] Cover allowlist defaults and rejection paths with unit tests.
- [x] 1.1.2. Add `sh.make` to construct `SafeCmd` instances with typed argv
  handling and minimal builder examples.
  - [x] Document the expected builder pattern in `docs/users-guide.md`.

### 1.2. Execution runtime

- [x] 1.2.1. Implement async `SafeCmd.run` with capture/echo toggles, env/cwd
  overrides, structured result object, and cancellation that sends terminate
  then kill after a grace period.
  - [x] Add integration tests asserting cleanup on cancellation.
- [x] 1.2.2. Provide `run_sync` that mirrors async behaviour by driving the
  event loop and ensures identical error and result semantics.
  - [x] Cover parity with tests.

### 1.3. Context and hooks

- [x] 1.3.1. Add `CuprumContext` backed by a `ContextVar`, supporting allowlist
  narrowing plus before/after hooks with deterministic ordering and `.detach()`.
  - [x] Test nesting across threads and async tasks.
- [x] 1.3.2. Ship a basic logging hook emitting start/exit events compatible
  with `logging` and wire it through context registration.
  - [x] Document hook usage patterns.

## 2. Pipelines and observability depth

Focus: Provide pipeline execution, richer events, and concurrency helpers.

### 2.1. Pipeline execution

- [x] 2.1.1. Implement `Pipeline` composition via the `|` operator with
  streaming between stages and backpressure handling; expose exit metadata per
  stage.
- [x] 2.1.2. Define and implement failure policy (fail fast, terminate
  downstream, surface failing stage) and cover with tests for early, middle,
  and late stage errors.

### 2.2. Structured events and telemetry

- [x] 2.2.1. Expand execution events to include stdout/stderr line emissions,
  timings, and tag metadata; add `sh.observe` (or equivalent) registration.
- [x] 2.2.2. Provide example adapters for `logging`, Prometheus-style metrics,
  and OpenTelemetry traces, ensuring hooks remain optional and non-blocking.

### 2.3. Concurrency helpers

- [x] 2.3.1. Add helper to run multiple `SafeCmd` instances concurrently with
  optional concurrency limits, while preserving hook semantics and aggregated
  results.
  - [x] Document common patterns alongside examples.

## 3. Builders and ergonomics

Focus: Enrich the ecosystem around builders and optional ergonomics without
compromising explicitness.

### 3.1. Builder library and validation

- [ ] 3.1.1. Publish a core builder set for common tools (git, rsync, tar) using
  typed argument helpers such as `SafePath` and `GitRef`, with validation.
  - [ ] Add unit tests that exercise validation and builder output.
- [ ] 3.1.2. Provide a scaffold and guidance for project-specific builders,
  including a template module and checklist in `docs/users-guide.md`.

### 3.2. Optional sugar and compatibility

- [ ] 3.2.1. Introduce an opt-in registration API for attribute-style access
  (for example, `sh.register("ls", LS)`), preserving allowlist enforcement and
  documenting when to avoid the sugar.
- [ ] 3.2.2. Supply static-analysis support (type stubs or a mypy plugin) that
  keeps attribute-style commands type-safe; include sample configuration.

### 3.3. Configuration and policy

- [ ] 3.3.1. Add policy toggles for allowlist narrowing, unsafe namespace
  warnings, and default echo behaviour; ensure defaults remain safe and test
  policy interactions.
- [ ] 3.3.2. Document policy switches and recommended defaults in
  `docs/users-guide.md` and add release notes describing the migration path for
  existing users.

### 3.4. Execution timeouts

- [ ] 3.4.1. Add `timeout` parameters to `SafeCmd.run` / `run_sync` and
  `Pipeline.run` / `run_sync`, matching `subprocess.run` semantics and
  surfacing a `TimeoutExpired` exception with partial output when captured.
- [ ] 3.4.2. Introduce scoped runtime defaults via `CuprumContext` so a timeout
  can be set once per scope (for example, `sh.scoped(ScopeConfig(timeout=…))`).
  Precedence should be explicit timeout > `ExecutionContext.timeout` >
  `ScopeConfig.timeout` (scoped default).
- [ ] 3.4.3. Add unit and behavioural tests for single-command and pipeline
  timeouts, including cleanup behaviour and partial output capture. Ensure a
  test covers `ExecutionContext(timeout=None)` falling through to the scoped
  default (rather than disabling timeouts). Document usage and semantics in
  `docs/users-guide.md`.

## 4. Performance extensions

Focus: Provide optional Rust-based stream operations for high-throughput
scenarios whilst maintaining pure Python as a first-class pathway.

### 4.1. Build system integration

- [ ] 4.1.1. Add maturin as an optional build backend alongside hatchling;
  configure `pyproject.toml` to support both pure Python and native wheel
  builds.
- [ ] 4.1.2. Create a `rust/` directory with a Cargo workspace; implement
  minimal PyO3 bindings exposing `is_available()` stub and verify import from
  Python.
- [ ] 4.1.3. Extend the CI matrix to build native wheels for Linux (x86_64,
  aarch64), macOS (x86_64, arm64), and Windows (x86_64, arm64) using maturin
  and cibuildwheel.
- [ ] 4.1.4. Add a pure Python fallback wheel job that excludes native code;
  verify both wheel types install correctly and coexist in the same environment.

### 4.2. Core pump extension

- [ ] 4.2.1. Implement `rust_pump_stream()` with GIL release, configurable
  buffer size (default 64 KB), and proper error propagation to Python
  exceptions.
  - [ ] Add unit tests covering normal operation and error paths.
- [ ] 4.2.2. Implement `rust_consume_stream()` with incremental UTF-8 decoding
  matching Python pathway behaviour for `errors="replace"` semantics; verify
  parity with edge-case tests.
- [ ] 4.2.3. Add Linux-specific `splice()` code path with runtime detection;
  fall back to read/write loop on unsupported platforms or file descriptor
  types.
- [ ] 4.2.4. Create `cuprum/_backend.py` dispatcher with
  `CUPRUM_STREAM_BACKEND` environment variable support (`auto`, `rust`,
  `python`); cache availability check results for performance.

### 4.3. Test infrastructure

- [ ] 4.3.1. Parametrise existing stream unit tests (`test_pipeline.py`) to run
  against both Python and Rust pathways using a `stream_backend` fixture; skip
  Rust tests when the extension is unavailable.
- [ ] 4.3.2. Add behavioural parity tests verifying identical output for edge
  cases: empty streams, partial UTF-8 sequences at chunk boundaries, broken
  pipes, and backpressure scenarios.
- [ ] 4.3.3. Create integration tests for pathway selection logic including
  environment variable overrides, forced fallback, and error handling when Rust
  is requested but unavailable.
- [ ] 4.3.4. Add property-based tests using hypothesis for stream content
  preservation across random payloads and chunk boundaries.

### 4.4. Benchmarking CI

- [ ] 4.4.1. Create a benchmark suite using `pytest-benchmark` for
  microbenchmarks (pump latency, consume throughput) and `hyperfine` for
  end-to-end pipeline throughput measurement.
- [ ] 4.4.2. Define benchmark scenarios: small (1 KB), medium (1 MB), large
  (100 MB) payloads; single-stage and multi-stage pipelines; with and without
  line callbacks.
- [ ] 4.4.3. Add a CI job that runs benchmarks on pull requests and main branch
  pushes; store results as JSON artefacts and fail if the Rust pathway
  regresses beyond a 10% threshold.
- [ ] 4.4.4. Generate benchmark comparison report (Python vs Rust) and publish a
  summary table to the GitHub Actions workflow summary.

### 4.5. Documentation

- [ ] 4.5.1. Extend `docs/cuprum-design.md` with Section 13 covering Rust
  extension architecture, API boundary, fallback strategy, and performance
  characteristics.
- [ ] 4.5.2. Add performance guidance to `docs/users-guide.md` explaining when
  to use each pathway, how to configure selection via environment variable, and
  expected throughput improvements.
- [ ] 4.5.3. Document build prerequisites (Rust toolchain 1.70+, cargo, maturin)
  for contributors building from source with native extensions.
- [ ] 4.5.4. Add a troubleshooting section for common issues: missing wheels on
  exotic platforms, forced fallback behaviour, and benchmark result
  interpretation.
