# Parametrise existing stream unit tests (4.3.1)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

## Purpose / big picture

Cuprum provides optional Rust-backed stream operations (`rust_pump_stream`,
`rust_consume_stream`) alongside pure Python equivalents (`_pump_stream`,
`_consume_stream`). The backend dispatcher (`cuprum/_backend.py`, built in
4.2.4) resolves which backend to use at runtime via the
`CUPRUM_STREAM_BACKEND` environment variable, but is not yet wired into the
pipeline execution code. After this change:

1. The dispatcher is wired into inter-stage stream pumping
   (`_pipeline_streams.py`) so that `_create_pipe_tasks` routes through a new
   `_pump_stream_dispatch` function that selects the Rust or Python pump
   implementation at runtime.
2. Existing end-to-end pipeline tests in `test_pipeline.py` are parametrised
   via a `stream_backend` fixture to run against both Python and Rust
   pathways, with Rust tests skipped when the extension is unavailable.
3. BDD scenarios verify pipeline correctness under explicit backend selection.

A user can observe success by:

1. Running `make test` — parametrised tests execute twice (once per backend),
   with Rust variants skipped when the extension is not built.
2. Setting `CUPRUM_STREAM_BACKEND=python` — the pipeline uses the pure Python
   pump for inter-stage data transfer.
3. Setting `CUPRUM_STREAM_BACKEND=rust` (with the extension built) — the
   pipeline uses the Rust pump via `loop.run_in_executor()`.
4. Leaving the variable unset (`auto`) — the dispatcher selects Rust when
   available, otherwise Python.

## Constraints

Hard invariants that must hold throughout implementation:

- **Pump dispatch only**: This task wires the dispatcher into `_pump_stream`
  only. `_consume_stream` dispatch is deferred — the Rust consume path does
  not support `on_line` callbacks or `echo_output`, per the design doc
  Section 13.5.
- **Graceful fallback**: When file descriptor extraction from asyncio
  transports fails (e.g. mock transports in tests), the dispatcher must fall
  back to the Python pathway silently. No `RuntimeError` or `OSError` may
  escape from FD extraction.
- **No changes to `_streams.py`**: The pure Python `_pump_stream` and
  `_consume_stream` functions must not be modified.
- **No changes to `_backend.py`**: The dispatcher module is used as-is.
- **No changes to Rust code**: The Rust extension (`_streams_rs.py`,
  `_rust_backend.py`, native module) must not be modified.
- **Python 3.12+ compatibility**: Use `match` statements, `StrEnum`, and
  other 3.12+ features as appropriate.
- **Strict linting**: All code must satisfy the Ruff rule set in
  `pyproject.toml`, Pyright strict mode, and `make check-fmt`.
- **NumPy docstrings**: All public and private functions require NumPy-style
  docstrings per project convention.
- **Test conventions**: Unit tests in `cuprum/unittests/`, behavioural tests
  in `tests/behaviour/` with feature files in `tests/features/`.

## Tolerances (exception triggers)

- **Scope**: If implementation requires changes to more than 10 files, stop
  and escalate.
- **Interface**: If any existing public API signature must change, stop and
  escalate.
- **Dependencies**: No new runtime or dev dependencies allowed.
- **Iterations**: If tests still fail after 3 fix attempts per failure, stop
  and escalate.
- **Ambiguity**: If the FD extraction approach does not work reliably on the
  CI platform, stop and present options.

## Risks

- **Risk**: Extracting file descriptors from asyncio subprocess transports
  may rely on CPython internals (`_transport`, `get_extra_info('pipe')`).
  - Severity: medium
  - Likelihood: low (CPython is the only supported runtime)
  - Mitigation: Use `getattr` with fallback to `None`; fall back to Python
    pathway when extraction fails.

- **Risk**: The Rust pump takes ownership-like semantics of FDs whilst asyncio
  still owns the underlying transport objects.
  - Severity: medium
  - Likelihood: certain (by design)
  - Mitigation: The Rust pump only reads/writes; it does not close FDs. After
    the pump returns, close the writer via the asyncio `_close_stream_writer`
    helper, maintaining proper transport lifecycle.

- **Risk**: The `_clear_backend_cache` autouse fixture must clear caches
  before the `stream_backend` fixture sets the env var.
  - Severity: low
  - Likelihood: low (autouse fixtures run before parametrised fixtures)
  - Mitigation: Verified fixture ordering; the autouse fixture clears caches
    at the start of each test, and the parametrised fixture sets the env var
    afterwards. The dispatcher re-reads the env var on next call since the
    cache was cleared.

## Progress

- [x] (2026-02-14) Stage A: Wire `_pump_stream_dispatch` into
  `_pipeline_streams.py`
- [x] (2026-02-14) Stage B: Create `stream_backend` pytest fixture in
  `conftest.py`
- [x] (2026-02-14) Stage C: Parametrise end-to-end pipeline tests in
  `test_pipeline.py`
- [x] (2026-02-14) Stage D: Add BDD feature file and step implementations
- [x] (2026-02-14) Stage E: Update `cuprum/_testing.py` with re-exports
- [x] (2026-02-14) Stage F: Update documentation (`docs/users-guide.md`,
  `docs/roadmap.md`)
- [x] (2026-02-14) Stage G: Run full quality gate suite and verify

## Surprises & discoveries

- Observation: Ruff PLR0911 limits return statements to 6 per function. The
  initial `_extract_reader_fd` had 7 returns due to multiple guard-clause
  early exits. Evidence: `ruff check` reported PLR0911. Impact: Extracted a
  shared `_fd_from_transport` helper that walks the
  `transport.get_extra_info('pipe').fileno()` chain, reducing both FD
  extractors to two returns each.

- Observation: The markdown linter enforces indented code blocks (MD046) in
  execution plan documents, not fenced code blocks. Evidence:
  `make markdownlint` flagged four fenced blocks. Impact: Converted all
  fenced blocks to indented style.

## Decision log

- **Decision**: Wire dispatcher into production code rather than
  monkeypatching in tests
  - Rationale: The task requires tests to run against *both pathways*. The
    dispatcher was built in 4.2.4 specifically for this wiring. The design
    doc Section 13.6 provides the integration pseudocode. Monkeypatching
    would not exercise the real dispatch logic.
  - Date: 2026-02-14

- **Decision**: Only wire `_pump_stream` dispatch, not `_consume_stream`
  - Rationale: The Rust consume path does not support `on_line` callbacks or
    echo (design doc Section 13.5). The pump dispatch is the clean case for
    inter-stage byte transfer. `_consume_stream` dispatch can follow in a
    later task.
  - Date: 2026-02-14

- **Decision**: Keep stub-based pump tests Python-only
  - Rationale: `_StubPumpReader`/`_StubPumpWriter` implement asyncio
    `StreamReader`/`StreamWriter` interfaces. The Rust pathway operates on
    raw file descriptors. Parametrising these tests would require entirely
    different stubs (OS pipes), which already exist in `test_rust_streams.py`.
    The end-to-end parametrised tests provide Rust coverage.
  - Date: 2026-02-14

- **Decision**: Use `pytest.mark.skipif` on the Rust fixture parameter
  - Rationale: Follows the existing `rust_streams` fixture pattern from
    `conftest.py:43`. Provides clear skip output
    ("Rust extension is not installed") rather than runtime failures.
  - Date: 2026-02-14

- **Decision**: Use `loop.run_in_executor()` for Rust pump integration
  - Rationale: Per design doc Section 13.6. The blocking Rust call releases
    the GIL; running in the default executor keeps the asyncio event loop
    responsive for concurrent tasks (stderr capture, process wait, etc.).
  - Date: 2026-02-14

- **Decision**: Fall back to Python when FD extraction fails
  - Rationale: Not all `StreamReader`/`StreamWriter` objects have extractable
    file descriptors (e.g. mock transports in tests, custom transports).
    Graceful fallback prevents regressions and maintains backward
    compatibility.
  - Date: 2026-02-14

## Outcomes & retrospective

Implementation completed successfully:

- Wired `_pump_stream_dispatch` into `cuprum/_pipeline_streams.py` with
  `_fd_from_transport`, `_extract_reader_fd`, and `_extract_writer_fd`
  helpers following the design doc Section 13.6 integration pattern
- Added `stream_backend` parametrised fixture to `conftest.py` with
  `pytest.mark.skipif` for Rust availability
- Parametrised 3 end-to-end pipeline tests (6 test variants: 3 Python +
  3 Rust, with Rust skipped when unavailable)
- Added 3 BDD scenarios verifying pipeline output under Python, Rust, and
  auto backend selection
- Updated `cuprum/_testing.py` with `_pump_stream_dispatch` re-export
- Updated `docs/users-guide.md` with dispatcher wiring note
- Marked roadmap item 4.3.1 as complete

All quality gates passed:

- `make check-fmt`: 66 files formatted
- `make typecheck`: All checks passed
- `make lint`: All checks passed
- `make test`: 281 passed, 26 skipped (Rust tests skipped when extension
  not built, which is expected)
- `make markdownlint`: 0 errors

Files modified (8 total, within tolerance of 10):

1. `cuprum/_pipeline_streams.py` — Added dispatch function and FD extractors
2. `cuprum/unittests/test_pipeline.py` — Parametrised 3 tests with
   `stream_backend`
3. `conftest.py` — Added `stream_backend` parametrised fixture
4. `cuprum/_testing.py` — Added `_pump_stream_dispatch` re-export
5. `docs/users-guide.md` — Added dispatcher wiring note
6. `docs/roadmap.md` — Marked 4.3.1 complete
7. `tests/features/stream_backend_pipeline.feature` — New BDD feature file
8. `tests/behaviour/test_stream_backend_pipeline.py` — New BDD steps

Lessons learned:

- Ruff PLR0911 (max return statements) catches guard-clause-heavy helper
  functions — extract shared traversal logic to stay under the limit
- The `_fd_from_transport` pattern (walking
  `transport → get_extra_info('pipe') → fileno()`) is robust against
  missing attributes at any level when using `getattr` with `None` defaults
- Markdown linter for this project requires indented code blocks, not fenced

## Context and orientation

### Project structure

Cuprum is a typed, async command-execution library for Python 3.12+ with
optional Rust extensions for stream performance. Key directories:

- `cuprum/` — Python package (source)
- `cuprum/unittests/` — Unit tests (pytest)
- `tests/behaviour/` — BDD step implementations (pytest-bdd)
- `tests/features/` — Gherkin feature files
- `tests/helpers/` — Shared test utilities
- `docs/` — Design docs, users guide, roadmap, execplans

### Existing stream code

The Python stream functions live in `cuprum/_streams.py`:

- `_pump_stream(reader, writer)` — async, uses asyncio `StreamReader` /
  `StreamWriter` with backpressure via `drain()`
- `_consume_stream(stream, config, *, on_line=None)` — async, reads and
  decodes with optional line callbacks and echo

The Rust shim lives in `cuprum/_streams_rs.py`:

- `rust_pump_stream(reader_fd, writer_fd, *, buffer_size=65536)` — blocking,
  operates on raw file descriptors, releases GIL
- `rust_consume_stream(reader_fd, *, buffer_size=65536)` — blocking, raw FDs

The dispatcher lives in `cuprum/_backend.py`:

- `get_stream_backend()` — resolves to `StreamBackend.RUST` or
  `StreamBackend.PYTHON` based on env var and availability

### Pipeline stream integration point

`cuprum/_pipeline_streams.py` coordinates stream tasks for pipeline execution:

- `_create_pipe_tasks(processes)` (line 158) creates asyncio tasks calling
  `_pump_stream()` between adjacent stages
- `_create_stage_capture_tasks(process, config, ...)` creates tasks calling
  `_consume_stream()` for stderr/stdout

### Design specification

The design doc (`docs/cuprum-design.md`) specifies:

- Section 13.5 (lines 1771–1791): Rust pathway limitations — no `on_line`,
  no echo, UTF-8 only
- Section 13.6 (lines 1793–1817): Integration pattern using
  `_pump_stream_dispatch` with `loop.run_in_executor()` and FD extraction

### Quality gates

All four must pass before committing:

    make check-fmt   # ruff format --check
    make typecheck   # pyright via ty
    make lint        # ruff check
    make test        # pytest (parallel)

## Plan of work

### Stage A: Wire `_pump_stream_dispatch` into `_pipeline_streams.py`

Add three new functions to `cuprum/_pipeline_streams.py`:

1. **`_extract_reader_fd(reader) -> int | None`** — extract the raw file
   descriptor from an `asyncio.StreamReader` by accessing the underlying
   transport's pipe object. Returns `None` if extraction fails for any
   reason.

2. **`_extract_writer_fd(writer) -> int | None`** — extract the raw file
   descriptor from an `asyncio.StreamWriter` via its transport. Returns
   `None` if extraction fails.

3. **`_pump_stream_dispatch(reader, writer) -> None`** — async dispatch
   function following the design doc Section 13.6 pseudocode:
   - If `reader` is `None`, delegate to `_pump_stream(reader, writer)`
   - Call `get_stream_backend()` from `cuprum/_backend.py`
   - If `RUST` and both FDs are extractable: run `rust_pump_stream` via
     `loop.run_in_executor()`, then close writer via `_close_stream_writer()`
   - Otherwise: delegate to `_pump_stream(reader, writer)`

Modify `_create_pipe_tasks()` to call `_pump_stream_dispatch` instead of
`_pump_stream` directly.

### Stage B: Create `stream_backend` pytest fixture

Add a parametrised fixture to the root `conftest.py`:

    @pytest.fixture(
        params=[
            pytest.param("python", id="python-backend"),
            pytest.param(
                "rust",
                id="rust-backend",
                marks=pytest.mark.skipif(
                    not _rust_backend.is_available(),
                    reason="Rust extension is not installed",
                ),
            ),
        ],
    )
    def stream_backend(
        request: pytest.FixtureRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> str:
        backend: str = request.param
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend)
        return backend

The existing `_clear_backend_cache` autouse fixture already clears the
dispatcher's LRU caches between tests.

### Stage C: Parametrise end-to-end pipeline tests

Add the `stream_backend` fixture parameter to these three tests in
`cuprum/unittests/test_pipeline.py`:

1. `test_pipeline_run_streams_stdout_between_stages(stream_backend)`
2. `test_pipeline_timeout_raises_timeout_expired(stream_backend)`
3. `test_pipeline_run_sync_failure_semantics(stream_backend, ...)`

Test bodies remain identical. The fixture sets the env var; the dispatcher
inside `_pump_stream_dispatch` picks it up at execution time.

Tests left unchanged:

- `test_pump_stream_drains_per_chunk` — stub-based, asyncio interfaces
- `test_pump_stream_handles_downstream_close_without_hanging` — stub-based
- Composition tests — no stream operations
- Process management tests — no stream operations

### Stage D: Add BDD scenarios

Create `tests/features/stream_backend_pipeline.feature` with scenarios
verifying pipeline correctness under explicit backend selection:

- Pipeline streams output between stages using the Python backend
- Pipeline streams output between stages using the Rust backend (skipped
  when unavailable)
- Pipeline streams output between stages using auto backend

Create `tests/behaviour/test_stream_backend_pipeline.py` with step
implementations reusing the existing pipeline test infrastructure.

### Stage E: Update `cuprum/_testing.py`

Add re-exports for `_pump_stream_dispatch` so that future tests can access
the dispatch function through the test-only surface.

### Stage F: Update documentation

1. Update `docs/users-guide.md` — note that `CUPRUM_STREAM_BACKEND` now
   controls inter-stage stream pumping in pipelines.
2. Update `docs/roadmap.md` — mark 4.3.1 as `[x]`.

### Stage G: Quality verification

Run the full quality gate suite:

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/check-fmt.log
    make typecheck 2>&1 | tee /tmp/typecheck.log
    make lint 2>&1 | tee /tmp/lint.log
    make test 2>&1 | tee /tmp/test.log

All four must pass.

## Concrete steps

All commands run from `/home/user/project` unless noted.

**Step A: Wire the dispatch function.** Add `_extract_reader_fd`,
`_extract_writer_fd`, and `_pump_stream_dispatch` to
`cuprum/_pipeline_streams.py`. Modify `_create_pipe_tasks` to use the
dispatch function.

Verify with:

    python -c "from cuprum._pipeline_streams import _pump_stream_dispatch; print('ok')"

**Step B: Create the fixture.** Add `stream_backend` to
`conftest.py`.

Verify with:

    make test

**Step C: Parametrise tests.** Add `stream_backend` parameter to 3 tests
in `test_pipeline.py`.

Verify with:

    make test

**Step D: Add BDD scenarios.** Write feature file and step implementations.

Verify with:

    make test

**Step E: Update `_testing.py`.** Add re-exports.

Verify with:

    make typecheck

**Step F: Update docs.** Edit `docs/users-guide.md` and `docs/roadmap.md`.

Verify with:

    make markdownlint

**Step G: Full verification.**

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/check-fmt.log
    make typecheck 2>&1 | tee /tmp/typecheck.log
    make lint 2>&1 | tee /tmp/lint.log
    make test 2>&1 | tee /tmp/test.log

## Validation and acceptance

**Quality criteria (what "done" means):**

- Tests: All existing tests pass. Parametrised tests run both backends (Rust
  skipped when unavailable). New BDD scenarios pass. Run with `make test`.
- Lint/typecheck: `make lint` and `make typecheck` report no errors.
- Format: `make check-fmt` reports no differences.
- Documentation: Roadmap item 4.3.1 is marked done.

**Quality method (how verification is performed):**

1. Run `make check-fmt && make typecheck && make lint && make test`
2. Verify parametrised test output shows `[python-backend]` and
   `[rust-backend]` variants (Rust skipped if unavailable)
3. Verify `CUPRUM_STREAM_BACKEND=python` forces Python pathway
4. Verify BDD scenarios pass

**Behavioural acceptance:**

- Pipeline produces correct `stdout` with `CUPRUM_STREAM_BACKEND=python`
- Pipeline produces correct `stdout` with `CUPRUM_STREAM_BACKEND=rust`
  (when available)
- Pipeline produces correct `stdout` with `CUPRUM_STREAM_BACKEND=auto`
- Stub-based pump tests continue to pass unchanged
- All non-stream tests continue to pass unchanged

## Idempotence and recovery

All steps can be repeated safely:

- Writing files is idempotent (overwrite with same content)
- Tests can be re-run at any time
- Documentation edits are idempotent
- Cache clearing in test fixtures prevents cross-test pollution

If a step fails, review the error output, fix the issue, and re-run from the
failed step. No special rollback needed; git provides recovery.

## Artifacts and notes

### Key files created

1. `tests/features/stream_backend_pipeline.feature` — BDD feature file
2. `tests/behaviour/test_stream_backend_pipeline.py` — BDD steps

### Key files modified

1. `cuprum/_pipeline_streams.py` — Added dispatch function and FD extractors
2. `cuprum/unittests/test_pipeline.py` — Parametrised 3 tests
3. `conftest.py` — Added `stream_backend` fixture
4. `cuprum/_testing.py` — Added re-exports
5. `docs/users-guide.md` — Noted dispatcher wiring
6. `docs/roadmap.md` — Marked 4.3.1 done

### Existing patterns reused

- `get_stream_backend()` at `cuprum/_backend.py:90`
- `StreamBackend` enum at `cuprum/_backend.py:27`
- `_pump_stream()` at `cuprum/_streams.py:112`
- `_close_stream_writer()` at `cuprum/_streams.py:152`
- `rust_pump_stream()` at `cuprum/_streams_rs.py:47`
- `rust_streams` fixture pattern from `conftest.py:25-47`
- `_clear_backend_cache` autouse fixture from `conftest.py:50-58`
- BDD scenario pattern from
  `tests/behaviour/test_backend_dispatcher_behaviour.py`

### Files that must remain unchanged

- `cuprum/_streams.py` — Pure Python stream functions
- `cuprum/_backend.py` — Dispatcher module
- `cuprum/_rust_backend.py` — Availability probe
- `cuprum/_streams_rs.py` — Rust shim
- `cuprum/rust.py` — Public Rust API

## Interfaces and dependencies

### New functions in `cuprum/_pipeline_streams.py`

    def _extract_reader_fd(reader: asyncio.StreamReader | None) -> int | None:
        """Extract the raw file descriptor from an asyncio StreamReader."""

    def _extract_writer_fd(writer: asyncio.StreamWriter | None) -> int | None:
        """Extract the raw file descriptor from an asyncio StreamWriter."""

    async def _pump_stream_dispatch(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Route inter-stage pump to the Rust or Python implementation."""

### New fixture in `conftest.py`

    @pytest.fixture(params=[...])
    def stream_backend(
        request: pytest.FixtureRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> str:
        """Parametrise tests to run against both stream backends."""

### Dependencies

- `asyncio` (stdlib) — for `get_running_loop()`, `run_in_executor()`
- `cuprum._backend` (internal) — for `get_stream_backend()`, `StreamBackend`
- `cuprum._streams_rs` (internal) — for `rust_pump_stream()`
- No new external dependencies
