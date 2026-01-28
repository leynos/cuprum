# Implement Rust pump stream (4.2.1)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: DRAFT

PLANS.md is not present in this repository.

## Purpose / big picture

Provide a Rust-backed `rust_pump_stream()` that transfers data between file
Descriptors outside the GIL with a configurable buffer (default 64 KB) and
clean error propagation to Python exceptions. The new function must be fully
covered by unit and behavioural tests, and documentation must reflect the Rust
extension architecture, API boundary, fallback strategy, and performance
characteristics. Success is visible when the new tests fail before the Rust
function exists, pass after implementation, and all quality gates (`make
check-fmt`, `make typecheck`, `make lint`, `make test`) succeed. The roadmap
entry 4.2.1 should be marked done after completion.

## Constraints

- Pure Python remains first-class; no runtime path may require Rust.
- Follow the documentation style guide and 80-column wrapping in `docs/`.
- Use Makefile targets for linting, formatting, type checks, and tests.
- For long-running commands, use `set -o pipefail` with `tee` for logs.
- Preserve existing public API behaviour; any public API signature change
  requires escalation.
- The Rust module must release the GIL during blocking I/O and use a default
  buffer size of 64 KB.
- Error propagation must map Rust I/O errors to Python `OSError` while treating
  expected `BrokenPipe`/`ConnectionReset` semantics consistently with the
  Python `_pump_stream()` implementation.

## Tolerances (exception triggers)

- Scope: touching more than 20 files or 1,000 net lines requires escalation.
- Interfaces: changing any public Python API signature requires escalation.
- Dependencies: adding any new third-party dependency (Python or Rust) requires
  escalation.
- Tests: if new tests still fail after two full fix attempts, stop and
  escalate.
- Time: if a single milestone takes more than 4 hours, stop and escalate.
- Ambiguity: if module naming or API boundary conflicts with existing
  documentation, stop and present options with trade-offs.

## Risks

- Risk: Rust file descriptor handling may accidentally close file descriptors
  owned by Python, causing subtle downstream failures. Severity: high
  Likelihood: medium Mitigation: use `ManuallyDrop<File>` (or equivalent)
  wrappers to avoid closing FDs and document ownership clearly.
- Risk: GIL release might not cover the full I/O loop, reducing throughput.
  Severity: medium Likelihood: medium Mitigation: wrap the entire pump loop in
  `Python::allow_threads` and avoid Python calls inside the loop.
- Risk: `BrokenPipeError` behaviour may diverge from Python `_pump_stream()`.
  Severity: medium Likelihood: medium Mitigation: replicate Python semantics
  (ignore broken pipe on write, continue draining reads).
- Risk: Documentation may already cover Section 13; updates could conflict with
  existing content. Severity: low Likelihood: medium Mitigation: treat updates
  as revisions, not additions, and note changes in the Decision Log.

## Progress

- [ ] (2026-01-28 00:00Z) Draft ExecPlan for 4.2.1 implementation.
- [ ] Write failing unit tests for `rust_pump_stream` normal and error paths.
- [ ] Write failing behavioural tests (pytest-bdd) that exercise the Rust
  pump stream when available.
- [ ] Implement Rust `rust_pump_stream()` with GIL release and error mapping.
- [ ] Add or update Python shim module(s) to expose Rust function safely.
- [ ] Update documentation (`docs/cuprum-design.md`, `docs/users-guide.md`).
- [ ] Mark roadmap 4.2.1 as done and validate quality gates.

## Surprises & discoveries

- Observation: Qdrant notes store could not be reached (`qdrant-find` failed).
  Evidence: tool returned “All connection attempts failed.”
  Impact: no prior notes available for this plan; proceed with local docs only.

## Decision log

- Decision: Pending. Record module naming (`cuprum._streams_rs` vs
  `_rust_backend_native`) and FD ownership strategy once confirmed.
  Rationale: Must align implementation with existing docs and tests.
  Date/Author: TBD

## Outcomes & retrospective

- Pending until implementation completes.

## Context and orientation

Relevant code and documents:

- `cuprum/_streams.py`: Python reference implementation of `_pump_stream()` and
  `_consume_stream()`.
- `rust/cuprum-rust/src/lib.rs`: current PyO3 module exposing
  `is_available()`.
- `cuprum/_rust_backend.py`: Python shim that imports the native module
  `_rust_backend_native`.
- `docs/cuprum-design.md` Section 13: describes Rust stream architecture and
  API boundary; must be updated to reflect actual implementation choices.
- `docs/users-guide.md`: contains the “Performance extensions (optional Rust)”
  section; must reflect any new consumer-visible behaviour.
- `docs/roadmap.md`: 4.2.1 task must be marked done on completion.
- `tests/behaviour/` and `tests/features/`: pytest-bdd behavioural tests.
- `cuprum/unittests/`: unit tests (pytest).

Current state: the Rust extension exists only as an availability probe. No
`rust_pump_stream()` function or `cuprum._streams_rs` module exists yet. The
Python `_pump_stream()` uses asyncio streams and 4 KB chunks.

## Plan of work

Stage A: confirm architecture and API boundary (no code changes).

Review Section 13 in `docs/cuprum-design.md` and ADR-001 to confirm whether the
Rust API should be exposed as `cuprum._streams_rs` or extended within
`cuprum._rust_backend_native`. Decide the module naming strategy and document
it in the Decision Log. If the decision requires updating Section 13 to avoid
mismatch, do that in Stage D.

Stage B: write tests first (failing).

Add unit tests in `cuprum/unittests/` that call the Rust pump function directly
via the chosen Python shim/module. Cover:

- Normal transfer from a readable pipe to a writable pipe with the default
  buffer size.
- Configurable buffer size behaviour (non-default value).
- Error path when an invalid or closed file descriptor is supplied (expect
  `OSError`).
- Broken pipe handling: downstream closes early, function should not raise and
  should continue draining input to match Python semantics.

Add behavioural tests using pytest-bdd that exercise the Rust pump stream when
available. The behavioural test should be skipped cleanly if the native
extension is unavailable. The scenario should verify that data written to a
source pipe is observed at the destination pipe after calling
`rust_pump_stream()`. Keep it minimal and deterministic.

Stage C: implement Rust `rust_pump_stream()` and Python shims.

Extend the Rust module in `rust/cuprum-rust/src/lib.rs` to export
`rust_pump_stream()`:

- Signature: `rust_pump_stream(reader_fd: int, writer_fd: int,
  buffer_size: int = 65536) -> int` returning the total bytes transferred.
- Validate `buffer_size > 0`, else raise `ValueError`.
- Release the GIL with `Python::allow_threads` for the entire read/write loop.
- Use a reusable buffer sized to `buffer_size`.
- Use non-owning file descriptor wrappers (`ManuallyDrop<File>` or equivalent)
  so Rust does not close FDs owned by Python.
- On `BrokenPipe`/`ConnectionReset` during writes, stop writing but keep
  draining reads to avoid upstream deadlocks (matching `_pump_stream()`).
- Map any other `std::io::Error` into `OSError` and propagate to Python.

If a new shim module is needed (for example `cuprum/_streams_rs.py`), add it as
pure Python that imports the native module and exposes `rust_pump_stream()`
without changing public APIs. Keep naming consistent with the design docs and
future dispatcher work (4.2.4).

Stage D: documentation and roadmap updates.

Update `docs/cuprum-design.md` Section 13 to reflect the actual Rust module
name, function signature, buffer default, and error propagation semantics. Note
any limitations or behavioural differences. Update `docs/users-guide.md` to
reflect any consumer-visible behaviour (even if it is just a brief note that
Rust pump stream exists but is internal/experimental). Mark 4.2.1 as done in
`docs/roadmap.md` once tests and implementation are complete.

## Concrete steps

All commands run from `/home/user/project`. Use `set -o pipefail` and `tee` for
long outputs.

1. Inspect existing documentation and Rust module layout.

    rg -n "_rust_backend_native|_streams_rs|rust_pump_stream" docs
    rg -n "_rust_backend_native|rust" cuprum
    rg -n "rust_pump_stream" rust

2. Add failing unit tests and run them.

    set -o pipefail
    uv run pytest cuprum/unittests/test_rust_streams.py \
      | tee /tmp/test-rust-streams-unit.txt

3. Add failing behavioural tests and run them.

    set -o pipefail
    uv run pytest tests/behaviour/test_rust_streams_behaviour.py \
      tests/features/rust_streams.feature \
      | tee /tmp/test-rust-streams-bdd.txt

4. Implement `rust_pump_stream()` in Rust and add any required Python shim.

5. Re-run the new unit and behavioural tests.

    set -o pipefail
    uv run pytest cuprum/unittests/test_rust_streams.py \
      | tee /tmp/test-rust-streams-unit.txt

    set -o pipefail
    uv run pytest tests/behaviour/test_rust_streams_behaviour.py \
      tests/features/rust_streams.feature \
      | tee /tmp/test-rust-streams-bdd.txt

6. Update documentation and roadmap, then run quality gates.

    set -o pipefail
    make check-fmt | tee /tmp/make-check-fmt.txt

    set -o pipefail
    make lint | tee /tmp/make-lint.txt

    set -o pipefail
    make typecheck | tee /tmp/make-typecheck.txt

    set -o pipefail
    make test | tee /tmp/make-test.txt

7. If docs changed, run markdown validation and Mermaid checks.

    set -o pipefail
    make markdownlint | tee /tmp/make-markdownlint.txt

    set -o pipefail
    make nixie | tee /tmp/make-nixie.txt

## Validation and acceptance

Done means:

- New unit tests cover normal transfers and error paths for
  `rust_pump_stream()`.
- New pytest-bdd behavioural test verifies the Rust pump stream when the native
  extension is available and skips cleanly otherwise.
- `rust_pump_stream()` releases the GIL, defaults to a 64 KB buffer, and maps
  I/O errors to `OSError` while preserving broken pipe semantics.
- Documentation updates in `docs/cuprum-design.md` Section 13 and
  `docs/users-guide.md` reflect the new Rust pump function and its boundaries.
- `docs/roadmap.md` marks 4.2.1 as done.
- Quality gates pass: `make check-fmt`, `make lint`, `make typecheck`,
  `make test`, plus `make markdownlint` and `make nixie` if docs changed.

## Idempotence and recovery

- Steps are repeatable; re-running tests and formatting is safe.
- If a test fails, fix the issue and re-run the same command to confirm.
- If Rust FD handling proves unsafe, revert the Rust changes and revisit the
  Decision Log before proceeding.

## Artifacts and notes

Expected changes include:

- Rust function in `rust/cuprum-rust/src/lib.rs` implementing
  `rust_pump_stream()`.
- Optional Python shim module (for example `cuprum/_streams_rs.py`).
- New unit tests in `cuprum/unittests/test_rust_streams.py`.
- New behavioural tests in `tests/behaviour/test_rust_streams_behaviour.py` and
  `tests/features/rust_streams.feature`.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`.
- Roadmap update in `docs/roadmap.md` for 4.2.1.

## Interfaces and dependencies

Planned Rust function (PyO3-exposed):

    rust_pump_stream(reader_fd: int, writer_fd: int, buffer_size: int = 65536)
        -> int

- Returns total bytes transferred.
- Raises `ValueError` for invalid buffer sizes.
- Raises `OSError` for I/O errors other than expected broken pipes.

Planned Python module exposure:

- `cuprum._rust_backend_native` remains the native module name.
- If required for clarity or future dispatch, a new Python shim module
  `cuprum/_streams_rs.py` re-exports `rust_pump_stream()`.

Dependencies:

- No new Python or Rust dependencies expected; if a new crate (for example
  `libc`) is required, stop and escalate per tolerances.

## Revision note (required when editing an ExecPlan)

Initial draft authored on 2026-01-28 to plan 4.2.1 implementation.
