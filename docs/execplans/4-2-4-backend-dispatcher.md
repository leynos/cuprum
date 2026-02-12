# Backend dispatcher with CUPRUM_STREAM_BACKEND support (4.2.4)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

PLANS.md is not present in this repository.

## Purpose / big picture

Cuprum provides optional Rust-backed stream operations (`rust_pump_stream`,
`rust_consume_stream`) alongside pure Python equivalents (`_pump_stream`,
`_consume_stream`). Currently, these two pathways exist independently with no
mechanism to select between them at runtime. After this change, a new internal
dispatcher module (`cuprum/_backend.py`) will resolve which backend to use
based on the `CUPRUM_STREAM_BACKEND` environment variable and the availability
of the Rust extension, caching the result for performance. Future work (4.3.x)
will wire this dispatcher into the actual execution pipeline; this task creates
the selection logic only.

A user can observe success by:

1. Setting `CUPRUM_STREAM_BACKEND=python` and calling the dispatcher — it
   returns `StreamBackend.PYTHON` regardless of Rust availability.
2. Setting `CUPRUM_STREAM_BACKEND=rust` without the Rust extension — the
   dispatcher raises `ImportError`.
3. Leaving the variable unset (or `auto`) — the dispatcher returns
   `StreamBackend.RUST` when the extension is available, otherwise
   `StreamBackend.PYTHON`.
4. Running `make test` — all existing tests pass and the new dispatcher tests
   pass.

## Constraints

Hard invariants that must hold throughout implementation:

- **No execution pipeline changes**: This task creates only the dispatcher
  module. It must not modify `_subprocess_execution.py`,
  `_pipeline_streams.py`, `_pipeline_internals.py`, `_process_lifecycle.py`, or
  `sh.py`. Integration is a separate task.
- **No changes to existing modules**: `_rust_backend.py`, `_streams_rs.py`,
  `_streams.py`, and `rust.py` must not be modified. The dispatcher uses
  `_rust_backend.is_available()` as-is.
- **Public API stability**: `cuprum.is_rust_available()` behaviour is
  unchanged. The new dispatcher is internal (`_backend.py` with leading
  underscore).
- **Python 3.12+ compatibility**: Use `enum.StrEnum` (available since 3.11).
- **Strict linting**: All code must satisfy the Ruff rule set in
  `pyproject.toml` (including D, ANN, N, FBT rules), Pyright strict mode, and
  `make check-fmt`.
- **NumPy docstrings**: All public and private functions require NumPy-style
  docstrings per project convention.
- **Test conventions**: Unit tests in `cuprum/unittests/`, behavioural tests
  in `tests/behaviour/` with feature files in `tests/features/`.

## Tolerances (exception triggers)

- **Scope**: If implementation requires changes to more than 10 files, stop
  and escalate. (Actual: 7 files — within tolerance.)
- **Interface**: If any existing public API signature must change, stop and
  escalate. (Not triggered.)
- **Dependencies**: No new runtime or dev dependencies allowed. The dispatcher
  uses only stdlib modules (`os`, `enum`, `functools`). (Not triggered.)
- **Iterations**: If tests still fail after 3 fix attempts per failure, stop
  and escalate. (Not triggered — 1 iteration for lint fixes.)
- **Ambiguity**: If the env var semantics described in the design doc
  (Section 13.4) conflict with the users-guide description, stop and present
  options. (Not triggered — descriptions are consistent.)

## Risks

- **Risk**: The `_rust_backend.is_available()` function does not cache its
  result. The dispatcher must cache independently without modifying that module.
  - Severity: low
  - Likelihood: certain (by design — caching is a stated requirement)
  - Mitigation: Used `functools.lru_cache(maxsize=1)` on
    `_check_rust_available()` inside `_backend.py`. ✅ Resolved.

- **Risk**: Environment variable could be set to an unexpected value (mixed
  case, whitespace, empty string).
  - Severity: low
  - Likelihood: medium
  - Mitigation: `_read_backend_env()` strips and lowercases the value; raises
    `ValueError` for unrecognised values. ✅ Resolved.

- **Risk**: Tests that monkeypatch `os.environ` may interact with cached
  results.
  - Severity: medium
  - Likelihood: high
  - Mitigation: Autouse fixture calls `_check_rust_available.cache_clear()`
    before each test. Exported via `cuprum/_testing.py`. ✅ Resolved.

## Progress

- [x] (2026-02-10) Stage A: Create `cuprum/_backend.py` with `StreamBackend`
      enum and `get_stream_backend()` dispatcher
- [x] (2026-02-10) Stage B: Add unit tests in
      `cuprum/unittests/test_backend.py`
- [x] (2026-02-10) Stage C: Add behaviour-driven development (BDD) feature
      file and behavioural tests
- [x] (2026-02-10) Stage D: Update `cuprum/_testing.py` with cache reset
      helper
- [x] (2026-02-10) Stage E: Update documentation (`docs/users-guide.md`)
- [x] (2026-02-10) Stage F: Update roadmap (`docs/roadmap.md`) — mark 4.2.4
      as done
- [x] (2026-02-10) Stage G: Run full quality gate suite and verify

## Surprises & discoveries

- Observation: Ruff D401 requires imperative mood in docstring first lines.
  The initial docstring "Cached wrapper around…" was flagged. Evidence:
  `ruff check` reported D401 error on `_check_rust_available`. Impact: Fixed by
  rewording to "Return whether the Rust extension is available, with caching."

- Observation: Ruff reformatted the multi-line error message string in
  `_read_backend_env()` to a single line. Evidence: `ruff format --check`
  reported the file would be reformatted. Impact: Adopted the single-line
  format.

## Decision log

- **Decision**: Use `StrEnum` for `StreamBackend` rather than plain strings
  - Rationale: The `.rules/python-typing.md` guide recommends `StrEnum` for
    typed enumerations. It provides type safety, IDE autocompletion, and
    natural string comparison (`StreamBackend.AUTO == "auto"` is `True`).
  - Date: 2026-02-10

- **Decision**: Cache availability via `functools.lru_cache` on a private
  function rather than a module-level variable
  - Rationale: `lru_cache` is idempotent, thread-safe, and trivially
    clearable for tests via `cache_clear()`. A module-level sentinel would
    need manual lock management. This matches the caching pattern already
    used in `_streams_rs.py:_load_native()`.
  - Date: 2026-02-10

- **Decision**: The dispatcher returns a `StreamBackend` enum value, not a
  module reference
  - Rationale: Returning the resolved backend as an enum keeps the dispatcher
    a pure selection function. Callers (in the future 4.3.x integration) will
    map the enum to the appropriate module. This makes testing simpler (mock
    the enum, not module imports) and keeps `_backend.py` free of import
    dependencies on `_streams.py` or `_streams_rs.py`.
  - Date: 2026-02-10

- **Decision**: Separate `get_stream_backend()` from env var parsing
  - Rationale: `_read_backend_env()` reads and validates the env var (pure,
    testable). `_check_rust_available()` wraps and caches the availability
    probe. `get_stream_backend()` combines them. This keeps cyclomatic
    complexity within the project limit of 8 per function.
  - Date: 2026-02-10

## Outcomes & retrospective

Implementation completed successfully:

- Created `cuprum/_backend.py` with `StreamBackend` StrEnum and
  `get_stream_backend()` dispatcher following the design doc Section 13.4
  flowchart
- Added 12 unit tests covering all modes (auto, rust, python), validation,
  caching, and enum values
- Added 3 BDD scenarios covering auto fallback, forced-rust failure, and
  forced-python selection
- Updated `cuprum/_testing.py` with `_check_rust_available` re-export for
  test cache management
- Added caching note to `docs/users-guide.md` backend selection documentation
- Marked roadmap item 4.2.4 as complete

All quality gates passed:

- `make check-fmt`: 65 files formatted
- `make typecheck`: All checks passed
- `make lint`: All checks passed
- `make test`: 274 passed, 21 skipped (Rust tests skipped when extension not
  built, which is expected)
- `make markdownlint`: 0 errors

Files modified (7 total, within tolerance of 10):

1. `cuprum/_backend.py` — New: dispatcher module
2. `cuprum/unittests/test_backend.py` — New: unit tests
3. `tests/features/backend_dispatcher.feature` — New: BDD feature file
4. `tests/behaviour/test_backend_dispatcher_behaviour.py` — New: BDD steps
5. `cuprum/_testing.py` — Added `_check_rust_available` re-export
6. `docs/users-guide.md` — Added caching note
7. `docs/roadmap.md` — Marked 4.2.4 complete

Lessons learned:

- Ruff D401 imperative mood check catches docstrings starting with past
  participles or gerunds — start with a verb in imperative form
- The `lru_cache` + autouse `cache_clear()` pattern works well for testing
  cached singletons without cross-test pollution

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

### Existing backend code

The availability probe lives in `cuprum/_rust_backend.py`:

    def is_available() -> bool:
        try:
            native = importlib.import_module("cuprum._rust_backend_native")
        except ImportError as exc:
            if isinstance(exc, ModuleNotFoundError) and exc.name == (
                "cuprum._rust_backend_native"
            ):
                return False
            raise
        return bool(native.is_available())

This does NOT cache. The Rust shim in `cuprum/_streams_rs.py` has its own
`_load_native()` with `@functools.lru_cache(maxsize=1)` for module import
caching.

The public API in `cuprum/rust.py` wraps the probe:

    def is_rust_available() -> bool:
        return _rust_backend.is_available()

The `cuprum/__init__.py` re-exports `is_rust_available`.

### Test infrastructure

- Root `conftest.py` provides a `rust_streams` fixture that skips tests when
  the Rust extension is unavailable.
- `tests/helpers/stream_pipes.py` provides `_pipe_pair()`, `_read_all()`,
  `_safe_close()` for FD-level test helpers.
- `cuprum/_testing.py` re-exports internal helpers for tests.

### Design specification

The design doc (`docs/cuprum-design.md`, Section 13.4) specifies:

1. Read `CUPRUM_STREAM_BACKEND` env var (default: `auto`)
2. `rust` → try import + `is_available()`; raise `ImportError` if fails
3. `python` → select Python backend unconditionally
4. `auto` → try Rust; fall back to Python on failure
5. Cache the availability check result

The users guide (`docs/users-guide.md`, lines 977–990) already documents the
env var semantics for end users.

### Quality gates

All four must pass before committing:

    make check-fmt   # ruff format --check
    make typecheck   # pyright via ty
    make lint        # ruff check
    make test        # pytest (parallel)

## Plan of work

### Stage A: Create `cuprum/_backend.py`

Create a new module at `cuprum/_backend.py` containing:

1. **`StreamBackend` enum** — a `StrEnum` with three members: `AUTO`,
   `RUST`, `PYTHON`. The `auto()` values will produce lowercase strings
   `"auto"`, `"rust"`, `"python"`.

2. **`_read_backend_env() -> StreamBackend`** — reads `CUPRUM_STREAM_BACKEND`
   from `os.environ`, strips whitespace, lowercases, defaults to `"auto"`.
   Raises `ValueError` for unrecognised values with a message listing valid
   options.

3. **`_check_rust_available() -> bool`** — wraps
   `_rust_backend.is_available()` with `@functools.lru_cache(maxsize=1)`. This
   is the cached availability probe.

4. **`get_stream_backend() -> StreamBackend`** — the main dispatcher function.
   Reads the env var via `_read_backend_env()`, then:
   - If `PYTHON`: return `StreamBackend.PYTHON`
   - If `RUST`: check `_check_rust_available()`; if True return
     `StreamBackend.RUST`, if False raise `ImportError` with a descriptive
     message.
   - If `AUTO`: check `_check_rust_available()`; if True return
     `StreamBackend.RUST`, otherwise return `StreamBackend.PYTHON`.

5. **`__all__`** — exports `StreamBackend` and `get_stream_backend`.

The module has no dependency on `_streams.py` or `_streams_rs.py`. It only
imports `_rust_backend` for the availability check.

### Stage B: Add unit tests

Create `cuprum/unittests/test_backend.py` with the following test cases:

1. `test_auto_returns_rust_when_available` — monkeypatch
   `_rust_backend.is_available` to return True, unset env var, assert
   `get_stream_backend() == StreamBackend.RUST`.

2. `test_auto_returns_python_when_unavailable` — monkeypatch to return
   False, unset env var, assert result is `StreamBackend.PYTHON`.

3. `test_forced_rust_returns_rust_when_available` — set env var to `rust`,
   monkeypatch available, assert `StreamBackend.RUST`.

4. `test_forced_rust_raises_when_unavailable` — set env var to `rust`,
   monkeypatch unavailable, assert `ImportError` is raised with message
   mentioning `CUPRUM_STREAM_BACKEND`.

5. `test_forced_python_returns_python` — set env var to `python`, assert
   `StreamBackend.PYTHON` regardless of availability.

6. `test_invalid_env_var_raises_value_error` — set env var to `"turbo"`,
   assert `ValueError` is raised with valid options listed.

7. `test_env_var_case_insensitive` — set env var to `"AUTO"`, `"Rust"`,
   `"PYTHON"`, assert each resolves correctly.

8. `test_availability_is_cached` — call `get_stream_backend()` twice,
   verify `_rust_backend.is_available` is called only once (via call count on
   monkeypatch).

9. `test_cache_clear_allows_recheck` — call dispatcher, clear cache via
   `_check_rust_available.cache_clear()`, change availability, call again,
   verify new result.

10. `test_stream_backend_enum_values` — assert enum members have correct
    string values (`"auto"`, `"rust"`, `"python"`).

Each test clears the `_check_rust_available` cache in an autouse fixture to
prevent cross-test pollution.

### Stage C: Add BDD feature file and behavioural tests

Create `tests/features/backend_dispatcher.feature`:

    Feature: Stream backend dispatcher
      Cuprum selects a stream backend at runtime based on the
      CUPRUM_STREAM_BACKEND environment variable and Rust extension
      availability. The dispatcher caches the availability check for
      performance.

      Scenario: Auto mode selects Python when Rust is unavailable
        Given the Rust extension is not available
        And the stream backend environment variable is unset
        When the stream backend is resolved
        Then the resolved backend is python

      Scenario: Forced Rust mode raises when extension is unavailable
        Given the Rust extension is not available
        And the stream backend is forced to rust
        When an attempt is made to resolve the stream backend
        Then an ImportError is raised

      Scenario: Forced Python mode always selects Python
        Given the stream backend is forced to python
        When the stream backend is resolved
        Then the resolved backend is python

Note: The actual feature file retains "When I …" phrasing to match existing
project Gherkin conventions (all other feature files use first-person steps).

Create `tests/behaviour/test_backend_dispatcher_behaviour.py` with `@scenario`
decorators and step implementations. Steps monkeypatch
`_rust_backend.is_available` and `os.environ` as needed. Each scenario clears
the cache before running.

### Stage D: Update `cuprum/_testing.py`

Add an import and re-export of `_check_rust_available` from `_backend.py` so
that test code can call `_check_rust_available.cache_clear()` without reaching
into private module internals beyond `_testing`. This follows the existing
pattern where `_testing.py` re-exports internal helpers for test use.

### Stage E: Update documentation

Update `docs/users-guide.md` in the "Performance extensions" section. The
existing text (lines 977–990) already documents the env var. Added a brief note
that the selection is cached for the process lifetime (one additional sentence
after the env var description).

### Stage F: Update roadmap

Edit `docs/roadmap.md` to change the 4.2.4 checkbox from `[ ]` to `[x]`.

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

**Step A: Create the dispatcher module.** Write `cuprum/_backend.py` with the
`StreamBackend` enum, `_read_backend_env()`, `_check_rust_available()`, and
`get_stream_backend()` as described in Stage A.

Verify with:

    python -c "from cuprum._backend import StreamBackend, get_stream_backend; print(get_stream_backend())"

Expected: prints `StreamBackend.PYTHON` (assuming no Rust extension built).

**Step B: Add unit tests.** Write `cuprum/unittests/test_backend.py` as
described in Stage B.

Verify with:

    make test

Expected: new tests pass; existing tests unchanged.

**Step C: Add behavioural tests.** Write the feature file and step
implementations as described in Stage C.

Verify with:

    make test

Expected: new BDD scenarios pass.

**Step D: Update `_testing.py`.** Add the `_check_rust_available` re-export.

Verify with:

    make typecheck

Expected: type checker passes.

**Step E: Update documentation.** Edit `docs/users-guide.md` to add the caching
note.

Verify with:

    make markdownlint

Expected: no markdown errors.

**Step F: Update roadmap.** Edit `docs/roadmap.md` to mark 4.2.4 complete.

**Step G: Full verification.**

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/check-fmt.log
    make typecheck 2>&1 | tee /tmp/typecheck.log
    make lint 2>&1 | tee /tmp/lint.log
    make test 2>&1 | tee /tmp/test.log

Expected: all gates pass.

## Validation and acceptance

**Quality criteria (what "done" means):**

- Tests: All existing tests pass. New unit tests (12 cases) and new BDD
  scenarios (3 scenarios) pass. Tests run with `make test`.
- Lint/typecheck: `make lint` and `make typecheck` report no errors.
- Format: `make check-fmt` reports no differences.
- Documentation: `make markdownlint` passes. Roadmap item 4.2.4 is marked
  done.

**Quality method (how verification is performed):**

1. Run `make check-fmt && make typecheck && make lint && make test`
2. Verify `get_stream_backend()` returns `StreamBackend.PYTHON` when Rust
   is unavailable (default environment)
3. Verify `CUPRUM_STREAM_BACKEND=rust` raises `ImportError` when Rust is
   unavailable
4. Verify `CUPRUM_STREAM_BACKEND=python` returns `StreamBackend.PYTHON`

**Behavioural acceptance:**

- `get_stream_backend()` with no env var and no Rust extension returns
  `StreamBackend.PYTHON`
- `get_stream_backend()` with `CUPRUM_STREAM_BACKEND=rust` and no Rust
  extension raises `ImportError`
- `get_stream_backend()` with `CUPRUM_STREAM_BACKEND=python` returns
  `StreamBackend.PYTHON` regardless of Rust availability
- `get_stream_backend()` with `CUPRUM_STREAM_BACKEND=invalid` raises
  `ValueError`
- Availability check is called at most once across multiple
  `get_stream_backend()` calls (caching works)

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

1. `cuprum/_backend.py` — Dispatcher module
2. `cuprum/unittests/test_backend.py` — Unit tests
3. `tests/features/backend_dispatcher.feature` — BDD feature file
4. `tests/behaviour/test_backend_dispatcher_behaviour.py` — BDD steps

### Key files modified

1. `cuprum/_testing.py` — Added `_check_rust_available` re-export
2. `docs/users-guide.md` — Added caching note to backend selection docs
3. `docs/roadmap.md` — Marked 4.2.4 as done

### Existing patterns reused

- `_rust_backend.is_available()` at `cuprum/_rust_backend.py:8`
- `@functools.lru_cache(maxsize=1)` pattern from
  `cuprum/_streams_rs.py:23-26`
- `rust_streams` fixture from `conftest.py:24-46`
- Monkeypatching pattern from
  `cuprum/unittests/test_rust_extension.py:14-26`
- BDD scenario pattern from
  `tests/behaviour/test_rust_extension_behaviour.py`

### Files that remained unchanged

- `cuprum/_rust_backend.py` — Availability probe
- `cuprum/_streams_rs.py` — Rust shim
- `cuprum/_streams.py` — Pure Python streams
- `cuprum/rust.py` — Public Rust API
- `cuprum/__init__.py` — Package init (no new public exports needed)
- `cuprum/_subprocess_execution.py` — Execution pipeline
- `cuprum/_pipeline_streams.py` — Pipeline streams
- `cuprum/sh.py` — Facade

## Interfaces and dependencies

### New module: `cuprum/_backend.py`

    class StreamBackend(enum.StrEnum):
        AUTO = enum.auto()
        RUST = enum.auto()
        PYTHON = enum.auto()

    def _read_backend_env() -> StreamBackend:
        """Read and validate CUPRUM_STREAM_BACKEND from the environment."""

    @functools.lru_cache(maxsize=1)
    def _check_rust_available() -> bool:
        """Return whether the Rust extension is available, with caching."""

    def get_stream_backend() -> StreamBackend:
        """Resolve the active stream backend based on env var and availability."""

### Dependencies

- `os` (stdlib) — for `os.environ.get()`
- `enum` (stdlib) — for `StrEnum`
- `functools` (stdlib) — for `lru_cache`
- `cuprum._rust_backend` (internal) — for `is_available()`
- No new external dependencies
