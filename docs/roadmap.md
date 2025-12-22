# Cuprum roadmap

This roadmap translates the design into actionable increments without calendar
commitments. Phases are ordered and build on one another. Steps group related
workstreams. Tasks are measurable and must meet acceptance criteria to count as
done.

## Phase 1 – foundation

Focus: establish the typed core, scoped safety model, and reliable single
command execution.

### Step: typed command core

- [x] Introduce the `Program` NewType and a curated catalogue module with a
      default allowlist; block unknown executables by default and cover with
      unit tests.
- [x] Add `sh.make` to construct `SafeCmd` instances with typed argv handling
      and minimal builder examples; document the expected builder pattern in
      `docs/users-guide.md`.

### Step: execution runtime

- [x] Implement async `SafeCmd.run` with capture/echo toggles, env/cwd
      overrides, structured result object, and cancellation that sends
      terminate then kill after a grace period; add integration tests that
      assert cleanup on cancellation.
- [x] Provide `run_sync` that mirrors async behaviour by driving the event loop
      and ensures identical error and result semantics; cover parity with
      tests.

### Step: context and hooks

- [x] Add `CuprumContext` backed by a `ContextVar`, supporting allowlist
      narrowing plus before/after hooks with deterministic ordering and
      `.detach()`; test nesting across threads and async tasks.
- [x] Ship a basic logging hook emitting start/exit events compatible with
      `logging` and wire it through context registration; document hook usage
      patterns.

## Phase 2 – pipelines and observability depth

Focus: provide pipeline execution, richer events, and concurrency helpers.

### Step: pipeline execution

- [x] Implement `Pipeline` composition via the `|` operator with streaming
      between stages and backpressure handling; expose exit metadata per stage.
- [x] Define and implement failure policy (fail fast, terminate downstream,
      surface failing stage) and cover with tests for early, middle, and late
      stage errors.

### Step: structured events and telemetry

- [x] Expand execution events to include stdout/stderr line emissions, timings,
      and tag metadata; add `sh.observe` (or equivalent) registration.
- [ ] Provide example adapters for `logging`, Prometheus-style metrics, and
      OpenTelemetry traces, ensuring hooks remain optional and non-blocking.

### Step: concurrency helpers

- [ ] Add helper to run multiple `SafeCmd` instances concurrently with optional
      concurrency limits, while preserving hook semantics and aggregated
      results; document common patterns alongside examples.

## Phase 3 – builders and ergonomics

Focus: enrich the ecosystem around builders and optional ergonomics without
compromising explicitness.

### Step: builder library and validation

- [ ] Publish a core builder set for common tools (git, rsync, tar) using
      typed argument helpers such as `SafePath` and `GitRef`, with validation
      and unit tests.
- [ ] Provide a scaffold and guidance for project-specific builders, including
      a template module and checklist in `docs/users-guide.md`.

### Step: optional sugar and compatibility

- [ ] Introduce an opt-in registration API for attribute-style access (for
      example, `sh.register("ls", LS)`), preserving allowlist enforcement and
      documenting when to avoid the sugar.
- [ ] Supply static-analysis support (type stubs or a mypy plugin) that keeps
      attribute-style commands type-safe; include sample configuration.

### Step: configuration and policy

- [ ] Add policy toggles for allowlist narrowing, unsafe namespace warnings,
      and default echo behaviour; ensure defaults remain safe and test policy
      interactions.
- [ ] Document policy switches and recommended defaults in
      `docs/users-guide.md` and add release notes describing the migration path
      for existing users.

## Phase 4 – performance extensions

Focus: provide optional Rust-based stream operations for high-throughput
scenarios whilst maintaining pure Python as a first-class pathway.

### Step: build system integration

- [ ] Add maturin as optional build backend alongside hatchling; configure
      `pyproject.toml` to support both pure Python and native wheel builds.
- [ ] Create `rust/` directory with Cargo workspace; implement minimal PyO3
      bindings exposing `is_available()` stub and verify import from Python.
- [ ] Extend CI matrix to build native wheels for Linux (x86_64, aarch64),
      macOS (x86_64, arm64), and Windows (x86_64, arm64) using maturin and
      cibuildwheel.
- [ ] Add pure Python fallback wheel job that excludes native code; verify both
      wheel types install correctly and coexist in the same environment.

### Step: core pump extension

- [ ] Implement `rust_pump_stream()` with GIL release, configurable buffer size
      (default 64 KB), and proper error propagation to Python exceptions; add
      unit tests covering normal operation and error paths.
- [ ] Implement `rust_consume_stream()` with incremental UTF-8 decoding matching
      Python pathway behaviour for `errors="replace"` semantics; verify parity
      with edge-case tests.
- [ ] Add Linux-specific `splice()` code path with runtime detection; fall back
      to read/write loop on unsupported platforms or file descriptor types.
- [ ] Create `cuprum/_backend.py` dispatcher with `CUPRUM_STREAM_BACKEND`
      environment variable support (`auto`, `rust`, `python`); cache
      availability check results for performance.

### Step: test infrastructure

- [ ] Parametrize existing stream unit tests (`test_pipeline.py`) to run against
      both Python and Rust pathways using a `stream_backend` fixture; skip Rust
      tests when extension is unavailable.
- [ ] Add behavioural parity tests verifying identical output for edge cases:
      empty streams, partial UTF-8 sequences at chunk boundaries, broken pipes,
      and backpressure scenarios.
- [ ] Create integration tests for pathway selection logic including environment
      variable overrides, forced fallback, and error handling when Rust is
      requested but unavailable.
- [ ] Add property-based tests using hypothesis for stream content preservation
      across random payloads and chunk boundaries.

### Step: benchmarking CI

- [ ] Create benchmark suite using `pytest-benchmark` for microbenchmarks
      (pump latency, consume throughput) and `hyperfine` for end-to-end pipeline
      throughput measurement.
- [ ] Define benchmark scenarios: small (1 KB), medium (1 MB), large (100 MB)
      payloads; single-stage and multi-stage pipelines; with and without line
      callbacks.
- [ ] Add CI job that runs benchmarks on pull requests and main branch pushes;
      store results as JSON artefacts and fail if Rust pathway regresses beyond
      10% threshold.
- [ ] Generate benchmark comparison report (Python vs Rust) and publish summary
      table to GitHub Actions workflow summary.

### Step: documentation

- [ ] Extend `docs/cuprum-design.md` with Section 13 covering Rust extension
      architecture, API boundary, fallback strategy, and performance
      characteristics.
- [ ] Add performance guidance to `docs/users-guide.md` explaining when to use
      each pathway, how to configure selection via environment variable, and
      expected throughput improvements.
- [ ] Document build prerequisites (Rust toolchain 1.70+, cargo, maturin) for
      contributors building from source with native extensions.
- [ ] Add troubleshooting section for common issues: missing wheel on exotic
      platforms, forced fallback behaviour, and benchmark result interpretation.
