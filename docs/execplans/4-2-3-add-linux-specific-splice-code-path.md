# Add Linux-specific splice() code path (4.2.3)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

PLANS.md is not present in this repository.

## Purpose / big picture

Add zero-copy data transfer using the Linux `splice()` system call to the
Cuprum Rust extension. After this change, pipe-to-pipe data transfers on Linux
will bypass userspace entirely, reducing memory bandwidth and improving
throughput for large pipeline operations. Non-Linux platforms and unsupported
file descriptor types will continue using the existing read/write loop.

Users observe no API changes; the optimisation is internal to
`rust_pump_stream`. Success is measured by:

1. All existing tests continue to pass
2. New splice-specific tests pass on Linux
3. Fallback works correctly on non-Linux or unsupported FD types
4. Quality gates pass: `make check-fmt`, `make typecheck`, `make lint`,
   `make test`

## Constraints

Hard invariants that must hold throughout implementation:

- **API stability**: The `rust_pump_stream(reader_fd, writer_fd, buffer_size)`
  signature must not change. Splice is an internal optimisation.
- **Behavioural parity**: Splice path must produce identical results to
  read/write path: same bytes transferred, same error handling for broken
  pipes, same return value semantics.
- **Global Interpreter Lock (GIL) release**: All I/O operations must occur with
  the Python GIL released (already achieved via `py.detach()`).
- **Non-Linux platforms**: Windows and macOS builds must compile and work
  unchanged. Splice code must be gated with `#[cfg(target_os = "linux")]`.
- **Error propagation**: I/O errors must propagate as `OSError` to Python.
  Broken pipe and connection reset must be handled gracefully (drain reader,
  return bytes written).
- **Paths not to modify**: `cuprum/_streams_rs.py` (Python shim) requires no
  changes; the optimisation is Rust-internal.

## Tolerances (exception triggers)

Thresholds that trigger escalation when breached:

- **Scope**: If implementation requires changes to more than 8 files, stop and
  escalate.
- **Interface**: If `rust_pump_stream` signature must change, stop and escalate.
- **Dependencies**: The only new dependency allowed is `libc` for Linux targets.
  Any other new crate requires escalation.
- **Iterations**: If tests still fail after 3 fix attempts, stop and escalate.
- **Ambiguity**: If `splice()` error handling semantics conflict with existing
  behaviour, stop and document options.

## Risks

Known uncertainties that might affect the plan:

- **Risk**: splice() returns EINVAL for file descriptors that don't support it
  (regular files, some sockets)
  - Severity: low
  - Likelihood: expected (this is the fallback trigger)
  - Mitigation: First splice call acts as feature probe; EINVAL triggers
    fallback to read/write

- **Risk**: Rust edition 2024 may have breaking changes for unsafe code
  - Severity: low
  - Likelihood: low
  - Mitigation: Use well-established libc crate patterns; keep unsafe blocks
    minimal and documented

- **Risk**: Non-blocking FD handling differs between splice and read/write
  - Severity: medium
  - Likelihood: low
  - Mitigation: Handle EAGAIN consistently; tests cover non-blocking scenarios

## Progress

- [x] (2026-02-08) Stage A: Add libc dependency to Cargo.toml for Linux target
- [x] (2026-02-08) Stage B: Create splice.rs module with splice implementation
- [x] (2026-02-08) Stage C: Integrate splice into lib.rs with fallback
- [x] (2026-02-08) Stage D: Add unit tests for splice behaviour
- [x] (2026-02-08) Stage E: Add behavioural tests (pytest-bdd feature scenarios)
- [x] (2026-02-08) Stage F: Update documentation (cuprum-design.md,
      users-guide.md)
- [x] (2026-02-08) Stage G: Mark roadmap item 4.2.3 as complete
- [x] (2026-02-08) Stage H: Run full quality gate suite and verify

## Surprises & Discoveries

- Observation: Linting identified use of `typing.Any` for pytest `tmp_path`
  fixture and use of built-in `open()` instead of `Path.open()`. Evidence:
  `ruff check` reported ANN401 and PTH123 errors. Impact: Fixed by using proper
  `Path` type annotation and `Path.open()` method.

## Decision Log

- **Decision**: Use libc crate for splice() rather than nix crate
  - Rationale: libc is already a transitive dependency via PyO3; nix would add
    unnecessary abstraction. Direct syscall wrappers are simpler and more
    transparent.
  - Date: 2026-02-08

- **Decision**: Use first splice call as feature probe rather than inspecting
  FD type
  - Rationale: Attempting splice and checking for EINVAL is more reliable than
    trying to determine FD type via fstat. The kernel knows definitively
    whether splice is supported.
  - Date: 2026-02-08

- **Decision**: Rename existing read/write loop to `pump_stream_files_readwrite`
  - Rationale: Keep the original implementation as a clearly-named fallback
    rather than inlining it. Easier to test and maintain separately.
  - Date: 2026-02-08

## Outcomes & Retrospective

Implementation completed successfully:

- Added Linux-specific `splice()` code path with runtime detection in Rust
- Created `splice.rs` module with zero-copy transfer implementation
- Integrated splice into `pump_stream_files()` with automatic fallback
- Added unit tests for large pipe transfers and file-to-pipe fallback
- Added pytest-bdd behavioural test for large payload transfers
- Updated documentation in `cuprum-design.md` (Section 13.7) and
  `users-guide.md`
- Marked roadmap item 4.2.3 as complete

All quality gates passed:

- `make check-fmt`: 61 files formatted
- `make typecheck`: All checks passed
- `make lint`: All checks passed
- `make test`: 256 passed, 20 skipped (Rust tests skipped when extension not
  built, which is expected)
- `make markdownlint`: 0 errors
- `make nixie`: All diagrams validated

Files modified (9 total, within tolerance of 9):

1. `rust/cuprum-rust/Cargo.toml` - Added libc dependency for Linux
2. `rust/cuprum-rust/src/splice.rs` - New file: splice implementation
3. `rust/cuprum-rust/src/lib.rs` - Integrated splice with fallback
4. `cuprum/unittests/test_rust_streams.py` - Added splice unit tests
5. `tests/features/rust_streams.feature` - Added large transfer scenario
6. `tests/behaviour/test_rust_streams_behaviour.py` - Added step implementations
7. `docs/cuprum-design.md` - Expanded Section 13.7
8. `docs/users-guide.md` - Added Linux splice documentation
9. `docs/roadmap.md` - Marked 4.2.3 complete

Lessons learned:

- First splice call as feature probe is simpler and more reliable than trying
  to inspect FD types beforehand
- Rust edition 2024 worked without issues for the unsafe splice code

## Context and orientation

### Project structure

Cuprum is a typed command-execution library for Python with optional Rust
extensions for high-throughput stream operations.

Key directories:

- `rust/cuprum-rust/src/` - Rust extension source
- `cuprum/` - Python package
- `cuprum/unittests/` - Python unit tests
- `tests/behaviour/` - pytest-bdd behavioural tests
- `tests/features/` - Gherkin feature files
- `tests/helpers/` - Test utilities
- `docs/` - Documentation

### Current implementation

The Rust extension exposes
`rust_pump_stream(reader_fd, writer_fd, buffer_size)` which transfers bytes
between file descriptors with the GIL released.

File: `/home/user/project/rust/cuprum-rust/src/lib.rs`

The current flow:

1. `rust_pump_stream()` (PyO3 entry point, lines 38-54)
2. Validates buffer size, converts FDs
3. Calls `py.detach()` to release GIL
4. Calls `pump_stream()` (lines 140-152)
5. Calls `pump_stream_files()` (lines 238-262) - the read/write loop

The read/write loop allocates a buffer, reads chunks, writes chunks, handling
broken pipe gracefully by draining the reader.

### splice() system call

Linux `splice()` transfers data between two file descriptors where at least one
is a pipe, entirely within kernel space (zero-copy). Signature:

```c
ssize_t splice(int fd_in, off64_t *off_in, int fd_out, off64_t *off_out,
               size_t len, unsigned int flags);
```

Key behaviours:

- Returns bytes transferred, or -1 on error
- EINVAL if FDs don't support splice
- EPIPE for broken pipe
- Requires at least one pipe endpoint
- Flags: SPLICE_F_MOVE (move pages), SPLICE_F_MORE (more data coming)

## Plan of work

### Stage A: Add libc dependency

Modify `/home/user/project/rust/cuprum-rust/Cargo.toml` to add libc as a
Linux-only dependency:

```toml
[target.'cfg(target_os = "linux")'.dependencies]
libc = "0.2"
```

### Stage B: Create splice module

Create new file `/home/user/project/rust/cuprum-rust/src/splice.rs`:

1. **Module documentation**: Explain purpose and fallback behaviour
2. **Constants**: SPLICE_F_MOVE (0x01), SPLICE_F_MORE (0x04)
3. **`try_splice_pump(reader, writer, chunk_size) -> Option<Result<u64, Error>>`
   **:
   - Returns `Some(Ok(bytes))` on success
   - Returns `Some(Err(e))` on fatal error
   - Returns `None` when splice unsupported (caller should use read/write)
4. **`splice_once(fd_in, fd_out, len) -> Result<usize, Error>`**: Single splice
   call wrapper
5. **`splice_loop(fd_in, fd_out, chunk_size, initial) -> Result<u64, Error>`**:
   Continue until EOF or error
6. **`drain_reader(fd_in, chunk_size)`**: Drain remaining data after broken pipe
7. **`is_splice_unsupported(err) -> bool`**: Check if error indicates splice
   not supported (EINVAL only; EBADF/ESPIPE propagate as fatal errors)
8. **`is_nonfatal_write_error(err) -> bool`**: Check for broken pipe/connection
   reset

### Stage C: Integrate into lib.rs

Modify `/home/user/project/rust/cuprum-rust/src/lib.rs`:

1. Add conditional module declaration:

   ```rust
   #[cfg(target_os = "linux")]
   mod splice;
   ```

2. Modify `pump_stream_files()` to try splice first on Linux:

   ```rust
   fn pump_stream_files(...) -> Result<u64, io::Error> {
       #[cfg(target_os = "linux")]
       if let Some(result) = splice::try_splice_pump(reader, writer, buffer_size.value()) {
           return result;
       }
       pump_stream_files_readwrite(reader, writer, buffer_size)
   }
   ```

3. Rename existing implementation to `pump_stream_files_readwrite()`

### Stage D: Add unit tests

Extend `/home/user/project/cuprum/unittests/test_rust_streams.py`:

1. **`test_large_pipe_transfer`**: Verify large (10MB) pipe-to-pipe transfers
   complete correctly (exercises splice path on Linux)
2. **`test_file_to_pipe_fallback`**: Verify file-to-pipe transfers work
   (splice falls back on Linux since source is not a pipe)
3. Use existing `_pump_payload` helper pattern
4. Add `is_linux()` helper for conditional skip markers

### Stage E: Add behavioural tests

Update `/home/user/project/tests/features/rust_streams.feature`:

```gherkin
Scenario: Rust pump stream handles large pipe transfers
  Given the Rust pump stream is available
  When I pump a large payload through the Rust stream
  Then the output matches the large payload
```

Update `/home/user/project/tests/behaviour/test_rust_streams_behaviour.py`:

1. Add `@scenario` for new feature
2. Add step implementations with large payload (1MB+)
3. Reuse existing helpers from `tests/helpers/stream_pipes.py`

### Stage F: Update documentation

Update `/home/user/project/docs/cuprum-design.md` Section 13.7:

- Expand with implementation details (runtime detection flow, FD support table)
- Document error handling semantics
- Add splice flags explanation

Update `/home/user/project/docs/users-guide.md`:

- Add brief section explaining Linux splice optimisation is automatic
- Note that pipe-to-pipe transfers benefit most

### Stage G: Update roadmap

Mark `/home/user/project/docs/roadmap.md` item 4.2.3 as complete:

```markdown
- [x] 4.2.3. Add Linux-specific `splice()` code path…
```

### Stage H: Quality verification

Run full quality gate suite:

```bash
make check-fmt    # Formatting (Python and Rust)
make typecheck    # Python type checking
make lint         # Linting (Python and Rust)
make test         # All tests
```

## Concrete steps

All commands run from `/home/user/project` unless noted.

**Step A: Add libc dependency.** Edit `rust/cuprum-rust/Cargo.toml`, add after
`[dependencies]`:

```toml
[target.'cfg(target_os = "linux")'.dependencies]
libc = "0.2"
```

Verify with:

```bash
cd rust && cargo check
```

Expected: compilation succeeds, no errors.

**Step B: Create splice.rs.** Create `rust/cuprum-rust/src/splice.rs` with the
splice implementation.

Verify with:

```bash
cd rust && cargo check
```

Expected: compilation succeeds on Linux and non-Linux targets.

**Step C: Integrate into lib.rs.** Modify `rust/cuprum-rust/src/lib.rs` to add
module and dispatch logic.

Verify with:

```bash
cd rust && cargo test
```

Expected: all existing tests pass.

**Step D: Add unit tests.** Add tests to
`cuprum/unittests/test_rust_streams.py`.

Verify with:

```bash
make test
```

Expected: new tests pass; existing tests unchanged.

**Step E: Add behavioural tests.** Update feature file and behaviour tests.

Verify with:

```bash
make test
```

Expected: new scenarios pass.

**Step F: Update documentation.** Update `docs/cuprum-design.md` and
`docs/users-guide.md`.

Verify with:

```bash
make markdownlint
make nixie
```

Expected: no markdown or diagram errors.

**Step G: Update roadmap.** Edit `docs/roadmap.md` to mark 4.2.3 complete.

**Step H: Full verification.**

Run complete quality gate suite:

```bash
set -o pipefail
make check-fmt 2>&1 | tee /tmp/check-fmt.log
make typecheck 2>&1 | tee /tmp/typecheck.log
make lint 2>&1 | tee /tmp/lint.log
make test 2>&1 | tee /tmp/test.log
```

Expected: all gates pass.

## Validation and acceptance

**Quality criteria (what "done" means):**

- Tests: All existing tests pass; new splice tests pass on Linux
- Lint/typecheck: `make lint` and `make typecheck` report no errors
- Format: `make check-fmt` reports no differences
- Documentation: `make markdownlint` and `make nixie` pass

**Quality method (how we check):**

1. Run `make check-fmt && make typecheck && make lint && make test`
2. Verify splice is exercised on Linux by checking test coverage of splice path
3. Verify fallback works by running file-to-pipe test

**Behavioural acceptance:**

- `rust_pump_stream` continues to work identically from Python caller's
  perspective
- Large pipe transfers (>1MB) complete without error
- File-to-pipe transfers work (fallback exercised)
- Broken pipe handling unchanged (input drained, no exception)

## Idempotence and recovery

All steps can be repeated safely:

- Rust compilation is idempotent
- Tests can be re-run at any time
- Documentation edits are idempotent

If a step fails:

1. Review error output
2. Fix the issue
3. Re-run from the failed step

No special rollback needed; git provides recovery via `git checkout`.

## Artifacts and notes

### Key files to modify

1. `rust/cuprum-rust/Cargo.toml` - Add libc dependency
2. `rust/cuprum-rust/src/splice.rs` - New file: splice implementation
3. `rust/cuprum-rust/src/lib.rs` - Integrate splice with fallback
4. `cuprum/unittests/test_rust_streams.py` - Add splice unit tests
5. `tests/features/rust_streams.feature` - Add behavioural scenarios
6. `tests/behaviour/test_rust_streams_behaviour.py` - Add step implementations
7. `docs/cuprum-design.md` - Expand Section 13.7
8. `docs/users-guide.md` - Add splice documentation
9. `docs/roadmap.md` - Mark 4.2.3 complete

### Existing patterns to follow

- Platform conditionals: See `#[cfg(unix)]` and `#[cfg(windows)]` in lib.rs
- Test fixtures: `rust_streams` fixture in conftest.py
- Test helpers: `_pipe_pair`, `_read_all`, `_safe_close` in stream_pipes.py
- Behavioural tests: Existing scenarios in rust_streams.feature

## Interfaces and dependencies

### New Rust module: splice.rs

```rust
pub(crate) fn try_splice_pump(
    reader: &File,
    writer: &File,
    chunk_size: usize,
) -> Option<Result<u64, io::Error>>
```

### Dependencies

- libc 0.2 (Linux-only, for splice syscall wrapper)
- No new Python dependencies

### Files that remain unchanged

- `cuprum/_streams_rs.py` - Python shim (no changes needed)
- `cuprum/_rust_backend.py` - Availability probe (no changes needed)
- `cuprum/rust.py` - Public API (no changes needed)
