# Cuprum roadmap

This roadmap translates the design into actionable increments without calendar
commitments. Phases are ordered and build on one another. Steps group related
workstreams. Tasks are measurable and must meet acceptance criteria to count as
done.

## 0.2.0 — Direct stdin input (issue `#30`)

- [x] `StdinInput` parameter object with mutual-exclusion enforcement in
  `__post_init__` and `resolve(ctx)` encoding helper.
- [x] `_SubprocessExecution.stdin_data` field; `_spawn_subprocess` opens
  `stdin=asyncio.subprocess.PIPE` only when data is present, falling back to
  `None` to inherit parent stdin.
- [x] Concurrent stdin writer task (`_spawn_stdin_writer` / `_write_stdin`)
  with `stdin_error` event emission, `cuprum.stdin` logging, and
  `cuprum_stdin_bytes_total` / `cuprum_stdin_errors_total` metrics for
  throughput and failure tracking.
- [x] `_build_stream_config` and `_handle_stream_timeout` helpers extracted to
  reduce `_run_subprocess_with_streams` complexity.
- [x] `_execute_with_hooks` helper extracted from `SafeCmd.run`.
- [x] `RunOutputOptions` parameter object replacing flat `capture` / `echo`
  kwargs on `SafeCmd.run` and `run_sync`.
- [x] `IOOptions` retained as a deprecated alias for `RunOutputOptions`.

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

- [x] 3.1.1. Publish a core builder set for common tools (git, rsync, tar) using
  typed argument helpers such as `SafePath` and `GitRef`, with validation.
  - [x] Add unit tests that exercise validation and builder output.
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

- [x] 3.4.1. Add `timeout` parameters to `SafeCmd.run` / `run_sync` and
  `Pipeline.run` / `run_sync`, matching `subprocess.run` semantics and
  surfacing a `TimeoutExpired` exception with partial output when captured.
- [x] 3.4.2. Introduce scoped runtime defaults via `CuprumContext` so a timeout
  can be set once per scope (for example, `sh.scoped(ScopeConfig(timeout=…))`).
  Precedence should be explicit timeout > `ExecutionContext.timeout` >
  `ScopeConfig.timeout` (scoped default).
- [x] 3.4.3. Add unit and behavioural tests for single-command and pipeline
  timeouts, including cleanup behaviour and partial output capture. Ensure a
  test covers `ExecutionContext(timeout=None)` falling through to the scoped
  default (rather than disabling timeouts). Document usage and semantics in
  `docs/users-guide.md`.

## 4. Performance extensions

Focus: Provide optional Rust-based stream operations for high-throughput
scenarios whilst maintaining pure Python as a first-class pathway.

### 4.1. Build system integration

- [x] 4.1.1. Add maturin as an optional build backend alongside `uv_build`;
  configure `pyproject.toml` to support both pure Python and native wheel
  builds.
- [x] 4.1.2. Create a `rust/` directory with a Cargo workspace; implement
  minimal PyO3 bindings exposing `is_available()` stub and verify import from
  Python.
- [x] 4.1.3. Extend the CI matrix to build native wheels for Linux (x86_64,
  aarch64), macOS (x86_64, arm64), and Windows (x86_64, arm64) using maturin.
- [x] 4.1.4. Add a pure Python fallback wheel job that excludes native code;
  verify both wheel types install correctly and coexist in the same environment.

### 4.2. Core pump extension

- [x] 4.2.1. Implement `rust_pump_stream()` with GIL release, configurable
  buffer size (default 64 KB), and proper error propagation to Python
  exceptions.
  - [x] Add unit tests covering normal operation and error paths.
- [x] 4.2.2. Implement `rust_consume_stream()` with incremental UTF-8 decoding
  matching Python pathway behaviour for `errors="replace"` semantics; verify
  parity with edge-case tests.
- [x] 4.2.3. Add Linux-specific `splice()` code path with runtime detection;
  fall back to read/write loop on unsupported platforms or file descriptor
  types.
- [x] 4.2.4. Create `cuprum/_backend.py` dispatcher with
  `CUPRUM_STREAM_BACKEND` environment variable support (`auto`, `rust`,
  `python`); cache availability check results for performance.
  - [x] Issue `#128`: route `cuprum.is_rust_available()` through the same
    cached resolver used by dispatch to eliminate divergent availability
    results.

### 4.3. Test infrastructure

- [x] 4.3.1. Parametrize existing stream unit tests (`test_pipeline.py`) to run
  against both Python and Rust pathways using a `stream_backend` fixture; skip
  Rust tests when the extension is unavailable.
- [x] 4.3.2. Add behavioural parity tests verifying identical output for edge
  cases: empty streams, partial UTF-8 sequences at chunk boundaries, broken
  pipes, and backpressure scenarios.
- [x] 4.3.3. Create integration tests for pathway selection logic including
  environment variable overrides, forced fallback, and error handling when Rust
  is requested but unavailable.
- [x] 4.3.4. Add property-based tests using Hypothesis for stream content
  preservation across random payloads and chunk boundaries.

### 4.4. Benchmarking CI

- [x] 4.4.1. Create a benchmark suite using `pytest-benchmark` for
  microbenchmarks (pump latency, consume throughput) and `hyperfine` for
  end-to-end pipeline throughput measurement.
- [x] 4.4.2. Define benchmark scenarios: small (1 KB), medium (1 MB), large
  (100 MB) payloads; single-stage and multi-stage pipelines; with and without
  line callbacks.
- [x] 4.4.3. Add a CI job that runs benchmarks on pull requests and main branch
  pushes; store results as JSON artefacts and fail if the Rust pathway
  regresses beyond a 10% threshold.
- [x] 4.4.4. Generate benchmark comparison report (Python vs Rust) and publish a
  summary table to the GitHub Actions workflow summary.
- [x] 4.4.5. Complete issue `#58` by guarding parent-side tee profiling backend
  selection with a process-wide lock plus same-thread reentrancy rejection, and
  document the selector contract for benchmark workers.

### 4.5. Documentation

- [x] 4.5.1. Extend `docs/cuprum-design.md` with Section 13 covering Rust
  extension architecture, API boundary, fallback strategy, and performance
  characteristics.
- [x] 4.5.2. Add performance guidance to `docs/users-guide.md` explaining when
  to use each pathway, how to configure selection via environment variable, and
  expected throughput improvements.
- [x] 4.5.3. Document build prerequisites (Rust toolchain 1.85+, cargo,
  maturin) for contributors building from source with native extensions.
- [x] 4.5.4. Add a troubleshooting section for common issues: missing wheels on
  exotic platforms, forced fallback behaviour, and benchmark result
  interpretation.

## 5. Reclaim the pure-Python consume hot path

Idea: if the cheap, Rust-free tuning that the tee profiling baseline identified
— a larger read size and a lean per-line event path — lands before any consume
dispatcher, the Rust bridge must prove itself against an already-tuned Python
baseline. This keeps the 20% acceptance gate honest rather than crediting Rust
for wins a one-line Python change already captures.

This phase turns the baseline's two pure-Python findings into shipped
improvements: the read-size plateau (about a 20% win on every parent-side
consume path) and the per-line observe-hook dispatch cost (the dominant tee-path
cost when line callbacks are active). Both are independent of the Rust extension
and deliver value on their own.

### 5.1. Bank the read-size win on every parent-side consume path

This step answers how much consume overhead a buffer-size bump alone reclaims
and where it plateaus. Its outcome sets the tuned Python baseline that the
consume dispatcher in phase 6 must beat. See
tee-hotpath-profiling-baseline-2026-06-12.md §1 (Table 3) and
adr-002-additional-rust-components.md Proposal 3.

- [ ] 5.1.1. Raise `_READ_SIZE` (`cuprum/_streams.py:14`) from 4 KiB to the
  profiled plateau in the 16-64 KiB range, choosing the value from a fresh
  read-size sweep rather than assuming the baseline number.
  - Update `test_stream_preserves_random_payloads_around_python_read_size_boundary`
    (`cuprum/unittests/test_stream_property_based.py:120`) to exercise chunk
    boundaries around the new size.
  - Success: the `tee-devnull-nocb-s1` scenario median wall time is at least 20%
    lower than tee-hotpath-profiling-baseline-2026-06-12.md §1 (Table 3), with
    no parity regressions, and the chosen value plus its benchmark artefact are
    recorded alongside the baseline document.

### 5.2. Make per-line event emission cheap for line-callback workloads

This step answers whether the roughly 46x line-callback slowdown can be cut by
removing per-line work from the observe-hook path without changing observable
event semantics. Its outcome informs whether per-line events ever need to become
opt-in. See tee-hotpath-profiling-baseline-2026-06-12.md §5 (Table 4).

- [ ] 5.2.1. Hoist the invariant `ExecEvent` / `_EventDetails`
  (`cuprum/events.py:31`, `cuprum/_pipeline_types.py:40`) fields out of the
  per-line path in `_StageObservation.emit` (`cuprum/_pipeline_types.py:62`),
  precomputing programme, argv (`SafeCmd.argv_with_program`, `cuprum/sh.py:363`),
  cwd, env, and pid once per stream rather than per line.
  - Success: per-line emission no longer reconstructs invariant fields, the
    callback scenario's dataclass-construction share falls from the 39% baseline
    to no more than 10% in a committed profiler artefact, and emitted event
    payloads are unchanged.
- [ ] 5.2.2. Remove the per-hook `inspect.isawaitable` call from the per-line
  path in `_emit_exec_event` (`cuprum/_observability.py:35`) by classifying each
  hook as sync or async once at registration.
  - Requires 5.2.1.
  - Success: known-sync hooks dispatch without per-line awaitable detection,
    async hooks retain identical scheduling behaviour, and a committed profiler
    artefact shows `inspect.isawaitable` contributes 0 sampled frames in the
    per-line hot path.
- [ ] 5.2.3. Add a combinatorial event-parity suite proving the optimized path is
  behaviour-preserving across the hook-type and stream-mode matrix.
  - Requires 5.2.1 and 5.2.2.
  - Cover sync and async hooks crossed with capture, echo, and line-callback
    modes.
  - Success: emitted event payloads (programme, argv, cwd, env, pid, line, and
    timestamp semantics) are equivalent before and after the optimization for
    every combination in the matrix.

## 6. Trustworthy, accelerated capture-only consume (issues `#90`, `#127`)

Idea: if a narrowly scoped capture-only consume dispatcher clears the 20%
wall-time gate the baseline projects from the roughly 51% capture double-touch —
measured against the tuned phase 5 baseline — Cuprum can route the measured
hotspot through Rust while keeping Python as the behavioural reference. If it
cannot clear the gate, `rust_consume_stream` stays inert and the negative result
is recorded.

Issue `#127` deliberately took the annotate-first fork: `rust_consume_stream` is
shipped, tested, and documented as "implemented but not integrated", guarded by
tests that fail if production code routes through it. This phase resolves that
ambiguity by either wiring the dispatcher symmetric to `_pump_stream_dispatch`
or recording why the gate was not met. Issue `#90` (proptest at the PyO3
boundary) is folded in as the correctness de-risking step.

### 6.1. Harden the PyO3 stream boundary so Rust and Python are interchangeable

This step answers whether `rust_pump_stream` and `rust_consume_stream` map errors
and decode identically to Python across the full input domain, not just scenario
fixtures. Its outcome is the correctness foundation the dispatcher relies on. See
issue `#90`, tee-hotpath-profiling-baseline-2026-06-12.md §§3-4, and
adr-002-additional-rust-components.md (decoding-drift and FD-ownership risks).

- [ ] 6.1.1. Introduce a `RustStreamError` enum in `rust/cuprum-rust/src/lib.rs`
  and convert it to PyO3 errors at a single boundary point.
  - Success: invalid `buffer_size` raises `ValueError` and I/O failures raise
    `OSError`, with the conversion centralized rather than scattered across call
    sites.
- [ ] 6.1.2. Add proptest parity and error-mapping coverage at the boundary.
  - Requires 6.1.1.
  - Cover empty, ASCII, and 2-, 3-, and 4-byte UTF-8 payloads, invalid bytes
    replaced with U+FFFD, and payloads around 1 MiB, plus default-versus-explicit
    `buffer_size` equivalence.
  - Success: proptest proves Rust-versus-Python consume parity across the
    generated domain with shrinking enabled and regression seeds committed.

### 6.2. Establish the wall-time acceptance gate against the tuned baseline

This step answers whether native read-and-decode beats the tuned Python consume
by the 20% the ADR requires, on the one eligible scenario. Its outcome decides
whether wiring proceeds at all. See
adr-002-additional-rust-components.md Phase 2,
tee-hotpath-profiling-baseline-2026-06-12.md §"Implications" item 1, and roadmap
step 4.4.

- [ ] 6.2.1. Add a Rust-versus-Python consume dispatcher benchmark for the
  fd-backed, UTF-8/replace, capture-only, no-echo, no-callback scenario, measured
  against the phase 5 tuned baseline.
  - Requires phase 5, 6.1.1, and 6.1.2.
  - Success: the benchmark reports median wall time for both paths and publishes
    it to the workflow summary; the gate passes only when the Rust median is at
    least 20% lower on the eligible path, small-output fallback scenarios stay
    within a 5% median wall-time regression budget, Python/Rust parity is
    confirmed before any Rust routing, and the verdict (pass or fail) is recorded
    in adr-002-additional-rust-components.md.

### 6.3. Wire `_consume_stream_dispatch` and route capture through it

This step answers whether consume dispatch can reuse the pump-dispatch shape with
a strict eligibility predicate and transparent fallback. It proceeds only if the
gate in 6.2 passed. See `cuprum/_pipeline_streams.py:290`
(`_pump_stream_dispatch`) and the `_StreamConfig` fields in `cuprum/_streams.py`.

- [ ] 6.3.1. Implement `_consume_stream_dispatch` mirroring
  `_pump_stream_dispatch`, with an eligibility predicate over `_StreamConfig`:
  `capture_output and not echo_output`, UTF-8 `encoding`, `errors == "replace"`,
  no line callback (`on_line is None`), and an extractable reader FD; otherwise
  fall back to the Python `_consume_stream`.
  - Requires 6.1.1 and 6.2.1.
  - Route the consume call sites at `cuprum/_subprocess_execution.py:229,236` and
    `cuprum/_pipeline_stage_streams.py:71,94` through the dispatcher.
  - Success: a backend-selection test proves Rust is engaged only when the
    predicate holds and `get_stream_backend()` is `RUST`, and that dispatch falls
    back to Python when the extension is absent, the FD is unavailable, or the
    config is ineligible (verified with a monkeypatched fake and call-count
    assertions).
- [ ] 6.3.2. Remove the inert-status markers and invert the guards now that
  production routes through the symbol.
  - Requires 6.3.1.
  - Update the `rust_consume_stream` docstring in `cuprum/_streams_rs.py`, and
    flip `test_rust_consume_stream_not_referenced_in_production` and
    `test_rust_consume_stream_docstring_not_integrated`
    (`cuprum/unittests/test_rust_streams.py`) to assert the wired state.
  - Success: `make check-fmt lint test` is green with the guards asserting
    production routing rather than its absence.
- [ ] 6.3.3. Add a combinatorial fallback E2E suite across the eligibility matrix.
  - Requires 6.3.1.
  - Cover backend (`auto`, `rust`, `python`) crossed with capture, echo,
    line-callback, encoding (UTF-8 and non-UTF-8), error policy (`replace` and
    `strict`), and reader kind (fd-backed versus in-memory).
  - Success: every ineligible combination produces byte-identical output to the
    Python reference, and only the single eligible combination engages Rust.

### 6.4. Reflect the outcome in the ADR and the guides

This step answers how readers learn the symbol's true status, whichever way the
gate fell. See adr-002-additional-rust-components.md Phase 2, cuprum-design.md
§13, and docs/users-guide.md.

- [ ] 6.4.1. Record the phase 6 outcome in the documentation set.
  - Requires 6.2.1 in all cases. The passed-gate branch also requires 6.3.2.
  - If the gate passed: mark ADR-002 Phase 2 as integrated, update the design-doc
    §13 diagrams and prose, and describe the consume eligibility conditions and
    transparent fallback in the users' guide.
  - If the gate failed: record the measured shortfall, keep the inert annotation,
    and note the negative result so the symbol is not mistaken for a wired bridge.
  - Success: no documentation describes `rust_consume_stream` in a way that
    contradicts its actual production status.

## 7. Raw-sink echo and tee acceleration (ADR-002 Proposal 2)

Idea: if a Rust raw-sink echo and tee helper removes the Python read-write loop
for fd-backed sinks, the echo and tee paths gain the same native benefit as the
pump. The baseline predicts PTY echo cost is kernel-side, so this slice must
prove where the helper actually helps and refuse to promise wins it cannot
deliver.

This phase extends the established Rust pathway to the echo and tee directions
rather than building a parallel path, reusing the boundary hardening from step
6.1. See adr-002-additional-rust-components.md Phase 3 and Proposal 2.

### 7.1. Profile and bound the raw-sink opportunity before building

This step answers which sink types a native echo and tee loop actually
accelerates, given that PTY cost is kernel-side. Its outcome scopes the helper
and prevents building dispatch for sinks it cannot help. See
tee-hotpath-profiling-baseline-2026-06-12.md §"Implications" item 4 and Table 2
(`echo-pty-nocb-s1` at 33 s).

- [ ] 7.1.1. Extend the profiling harness to compare the Python loop with a
  prototype native raw-sink loop across devnull, text-blackhole, and PTY sinks.
  - Success: a documented verdict names which sink kinds clear a 20% gate, with
    PTY explicitly characterized as kernel-bound and excluded from dispatch if it
    does not clear the gate.

### 7.2. Ship the raw-sink helper behind capability checks for qualifying sinks

This step answers whether the native helper can accelerate qualifying sinks while
falling back transparently for the rest. See
adr-002-additional-rust-components.md Proposal 2 and cuprum-design.md §13.

- [ ] 7.2.1. Implement the Rust raw-sink echo and tee helper with FD capability
  detection and Python fallback, routing only the sink kinds qualified by 7.1.1.
  - Requires 6.1.1 and 7.1.1.
  - Success: qualifying fd-backed sinks route through Rust with parity to Python;
    non-qualifying sinks (including PTY unless 7.1.1 qualified it) fall back
    transparently; and the open question of which raw sink types qualify is
    resolved in adr-002-additional-rust-components.md.

## 8. Deferred reliability and native-orchestration extensions

Idea: if the consume and echo acceleration paths are already trustworthy and
boring to operate, the remaining native-orchestration bets and the latent
reliability issues the baseline surfaced can be evaluated on their own merits
rather than gating the acceleration work.

### 8.1. Close the Rust pump file-descriptor close race

This step removes a silent failure the baseline observed on the shipped pump
path. See tee-hotpath-profiling-baseline-2026-06-12.md §"Incidental findings"
item 1, adr-002-additional-rust-components.md (FD ownership risk), and issues
`#124` / `#125`.

- [ ] 8.1.1. Fix the silent `OSError: [Errno 9] Bad file descriptor` raised from
  `_UnixWritePipeTransport._call_connection_lost` during Rust pump shutdown.
  - Success: the `echo-devnull-nocb-s4-rust` scenario logs no bad-FD errors
    across repeated runs, FD ownership at pump teardown is documented, and a
    regression test reproduces the close race and asserts clean shutdown.

### 8.2. Restore Python-frame attribution in perf captures

This step removes a tooling gap that weakens the evidence base future phases rely
on. See tee-hotpath-profiling-baseline-2026-06-12.md §"Incidental findings" item
2.

- [ ] 8.2.1. Investigate why distro perf 6.12 drops `py::` trampoline frames in
  `perf script` while resolving them in `perf report`.
  - Success: `stacks.folded` and `summary.json` carry Python-level frames, or the
    limitation is documented and the py-spy corroboration workaround is promoted
    to a first-class harness default.

### 8.3. Decide on native subprocess and pipeline orchestration

This step resolves the two heaviest ADR-002 bets, which the design explicitly
defers. See adr-002-additional-rust-components.md Proposals 4 and 5 (Phases 4 and
5).

- [ ] 8.3.1. Prototype the synchronous Rust subprocess fast path and record a
  go/no-go decision.
  - Requires phase 6.
  - Success: a benchmark-backed decision is captured in
    adr-002-additional-rust-components.md.
- [ ] 8.3.2. Decide whether full native pipeline orchestration graduates from
  deferral.
  - Requires 8.3.1.
  - Success: the decision and its rationale (likely continued deferral) are
    recorded in adr-002-additional-rust-components.md.
