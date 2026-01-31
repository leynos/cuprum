# Implement Rust consume stream (4.2.2)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: DRAFT

PLANS.md is not present in this repository.

## Purpose / big picture

Provide a Rust-backed `rust_consume_stream()` that reads from a file
descriptor and returns decoded text with incremental UTF-8 decoding matching
Python's `errors="replace"` behaviour. The Rust function must align with the
pure Python `_consume_stream_without_lines()` semantics for output capture
while remaining optional and internal. Success is visible when new unit tests
and behavioural tests fail before the implementation, pass after it, and all
quality gates (`make check-fmt`, `make typecheck`, `make lint`, `make test`)
complete successfully. The roadmap entry 4.2.2 is marked done only after the
implementation, documentation updates, and validation succeed.

## Constraints

- Pure Python remains first-class; no runtime path may require Rust.
- Follow the documentation style guide and 80-column wrapping in `docs/`.
- Use Makefile targets for linting, formatting, type checks, and tests.
- For long-running commands, use `set -o pipefail` with `tee` for logs.
- Preserve existing public API behaviour; any public API signature change
  requires escalation.
- Rust `rust_consume_stream()` must match the output of
  `payload.decode("utf-8", errors="replace")` for the same byte stream.
- Only UTF-8 with `errors="replace"` is supported in Rust; unsupported
  encodings or error modes must raise `ValueError` (the dispatcher will route
  to Python later).
- The Rust implementation must release the Global Interpreter Lock (GIL)
  during blocking I/O and must not close the reader file descriptor owned by
  Python.

## Tolerances (exception triggers)

- Scope: touching more than 20 files or 1,000 net lines requires escalation.
- Interfaces: changing any public Python API signature requires escalation.
- Dependencies: adding any new third-party dependency (Python or Rust)
  requires escalation.
- Tests: if new tests still fail after two full fix attempts, stop and
  escalate.
- Time: if a single milestone takes more than 4 hours, stop and escalate.
- Ambiguity: if multiple valid interpretations exist and materially affect the
  outcome, stop and present options with trade-offs.

## Risks

- Risk: incremental UTF-8 decoding may diverge from Python's `errors="replace"`
  semantics for partial sequences. Severity: high Likelihood: medium
  Mitigation: compare Rust output to Python's `bytes.decode` with test vectors
  that include invalid bytes and boundary-split multibyte sequences.
- Risk: reader file descriptor may be closed prematurely in Rust, breaking
  upstream process management. Severity: high Likelihood: low
  Mitigation: treat the FD as borrowed and `std::mem::forget` the `File`.
- Risk: tests may be flaky if pipes are not closed or if writes are not
  drained. Severity: medium Likelihood: medium Mitigation: use existing
  `tests/helpers/stream_pipes.py` helpers and ensure pipes are closed in
  `finally` blocks.
- Risk: documentation may already constrain API names. Severity: low
  Likelihood: medium Mitigation: verify `docs/cuprum-design.md` Section 13
  before making naming changes.

## Progress

- [x] (2026-01-31 00:00Z) Draft ExecPlan for 4.2.2 implementation.
- [ ] Add failing unit tests for `rust_consume_stream` parity and edge cases.
- [ ] Add failing behavioural tests (pytest-bdd) for Rust consume output.
- [ ] Implement Rust `rust_consume_stream()` and Python shim updates.
- [ ] Update documentation and mark roadmap 4.2.2 done.
- [ ] Run quality gates and confirm results.

## Surprises & discoveries

- Observation: Qdrant notes store could not be reached (`qdrant-find` failed).
  Evidence: tool returned "All connection attempts failed."
  Impact: no prior notes available for this plan; proceed with local docs only.

## Decision log

- Decision: implement incremental UTF-8 decoding in Rust without introducing
  new dependencies, using `std::str::from_utf8` error metadata and a pending
  buffer to preserve boundary-spanning sequences. Rationale: keeps the Rust
  extension lightweight while matching Python `errors="replace"` behaviour.
  Date/Author: 2026-01-31 / Codex
- Decision: expose `rust_consume_stream()` via `cuprum._streams_rs` alongside
  `rust_pump_stream()`, reusing the same platform file descriptor conversion.
  Rationale: aligns with existing Rust shim patterns and the design document.
  Date/Author: 2026-01-31 / Codex

## Outcomes & retrospective

Not started yet. This section will be updated after implementation milestones
are completed.

## Context and orientation

Relevant code and documents:

- `cuprum/_streams.py`: Python reference implementation of
  `_consume_stream_without_lines()` and `_consume_stream_with_lines()`.
- `cuprum/_streams_rs.py`: Python shim for the optional Rust stream functions.
- `rust/cuprum-rust/src/lib.rs`: PyO3 module where Rust functions are exposed.
- `cuprum/unittests/test_rust_streams.py`: existing Rust pump unit tests; add
  consume stream unit tests here for parity.
- `tests/behaviour/test_rust_streams_behaviour.py` and
  `tests/features/rust_streams.feature`: pytest-bdd behavioural tests for Rust
  stream functions.
- `tests/helpers/stream_pipes.py`: pipe helpers for deterministic I/O tests.
- `docs/cuprum-design.md` Section 13: design contract for Rust stream
  functions and limitations.
- `docs/users-guide.md`: public-facing documentation for optional Rust
  functionality.
- `docs/roadmap.md`: 4.2.2 entry must be marked done when complete.

Current state: the Rust extension exposes `rust_pump_stream()` only. The Rust
consume function is not yet implemented; Python `_consume_stream_without_lines`
remains the reference behaviour. The Rust pathway is expected to support
UTF-8 decoding with `errors="replace"` semantics only.

## Plan of work

Stage A: confirm behaviour and constraints (no code changes).

Review `docs/cuprum-design.md` Section 13 and `cuprum/_streams.py` to confirm
what `errors="replace"` means for `_consume_stream_without_lines()`.
Specifically note that Python decodes the entire buffer at the end, so chunk
boundaries must not affect output. Capture any deviations or constraints in
`Decision Log` before writing tests.

Stage B: write tests first (failing).

Add unit tests in `cuprum/unittests/test_rust_streams.py` that exercise
`rust_consume_stream()` directly via the `rust_streams` fixture. Cover:

- Normal ASCII payload round-trip with default buffer size.
- Payload containing multibyte UTF-8 characters split across buffer
  boundaries using a small `buffer_size` (for example, 2 bytes).
- Payload containing invalid bytes (for example, `b"\xff\xfe"`) to assert
  replacement characters (`\uFFFD`) match `payload.decode("utf-8",
  errors="replace")`.
- Payload ending with an incomplete multibyte sequence to confirm the final
  replacement behaviour matches Python's `errors="replace"` at EOF.
- Validation errors: `buffer_size=0`, `encoding` not `"utf-8"`, and `errors`
  not `"replace"` should raise `ValueError`.

Add behavioural tests using pytest-bdd:

- Extend `tests/features/rust_streams.feature` with a new scenario that writes
  a byte payload containing invalid UTF-8 and asserts the decoded text matches
  the Python `errors="replace"` result.
- Extend `tests/behaviour/test_rust_streams_behaviour.py` with steps that
  write the payload, call `rust_consume_stream()`, and compare outputs.

Run the new tests to confirm they fail before implementation.

Stage C: implement Rust `rust_consume_stream()` and Python shim updates.

In `rust/cuprum-rust/src/lib.rs`, add a new PyO3 `rust_consume_stream()`
function with signature:

- `rust_consume_stream(reader_fd: int, buffer_size: int = 65536,
  encoding: str = "utf-8", errors: str = "replace") -> str`.

Implementation notes:

- Validate `buffer_size > 0`, `encoding == "utf-8"`, and
  `errors == "replace"`; otherwise raise `ValueError`.
- Convert the file descriptor with `convert_fd` and construct a `File` using
  `file_from_raw`. Treat the reader FD as borrowed and `std::mem::forget` the
  `File` to avoid closing it.
- Release the GIL for the read loop with `Python::allow_threads` or
  `py.detach`, mirroring `rust_pump_stream()`.
- Read into a reusable buffer sized to `buffer_size`. Maintain a `Vec<u8>` of
  pending bytes that represent an incomplete UTF-8 sequence at the end of the
  current buffer.
- Use `std::str::from_utf8` to decode the pending buffer. When an error is
  returned:
  - Append the valid prefix to the output string.
  - If `error_len` is `Some(len)`, append `\uFFFD` and skip the invalid bytes,
    then continue decoding the remainder.
  - If `error_len` is `None`, preserve the trailing bytes (the incomplete
    sequence) in the pending buffer and wait for the next chunk.
- At EOF, if pending bytes remain, decode them and append a single `\uFFFD`
  to match `errors="replace"` semantics for incomplete sequences.
- Map any `std::io::Error` to Python `OSError` via `PyErr::from`.

In `cuprum/_streams_rs.py`, add a `rust_consume_stream()` wrapper mirroring
`rust_pump_stream()`:

- Convert file descriptors via `_convert_fd_for_platform`.
- Pass through `buffer_size`, `encoding`, and `errors` keyword arguments.
- Extend `__all__` accordingly.

Stage D: documentation and roadmap updates.

Update `docs/cuprum-design.md` Section 13 to reflect the implemented Rust
consume behaviour and its UTF-8 limitations. Update `docs/users-guide.md` to
note the internal `rust_consume_stream()` function and the decoding
limitations (UTF-8 with `errors="replace"`). Mark roadmap task 4.2.2 as done
in `docs/roadmap.md` once tests and implementation pass.

## Concrete steps

All commands run from `/home/user/project`. Use `set -o pipefail` and `tee`
for long outputs.

1. Inspect current implementations and tests:

   rg -n "consume_stream" cuprum rust tests docs
   sed -n '1,220p' cuprum/_streams.py
   sed -n '1,220p' cuprum/_streams_rs.py
   sed -n '1,260p' rust/cuprum-rust/src/lib.rs
   sed -n '1,240p' cuprum/unittests/test_rust_streams.py
   sed -n '1,220p' tests/behaviour/test_rust_streams_behaviour.py
   sed -n '1,120p' tests/features/rust_streams.feature

2. Add failing unit tests for `rust_consume_stream()` parity and errors.

3. Add failing pytest-bdd scenario and step definitions for Rust consume
   output.

4. Run the new tests to confirm failure before implementation:

   set -o pipefail
   pytest cuprum/unittests/test_rust_streams.py -k rust_consume_stream \
     2>&1 | tee /tmp/cuprum-test-rust-consume-unit.log

   set -o pipefail
   pytest tests/behaviour/test_rust_streams_behaviour.py -k rust_consume_stream \
     2>&1 | tee /tmp/cuprum-test-rust-consume-bdd.log

5. Implement `rust_consume_stream()` in Rust and add the Python shim wrapper.

6. Run targeted tests again to confirm they pass:

   set -o pipefail
   pytest cuprum/unittests/test_rust_streams.py -k rust_consume_stream \
     2>&1 | tee /tmp/cuprum-test-rust-consume-unit.log

   set -o pipefail
   pytest tests/behaviour/test_rust_streams_behaviour.py -k rust_consume_stream \
     2>&1 | tee /tmp/cuprum-test-rust-consume-bdd.log

7. Update documentation (`docs/cuprum-design.md`, `docs/users-guide.md`) and
   mark roadmap 4.2.2 as done.

8. Run formatting and Markdown validation after docs changes:

   set -o pipefail
   make fmt 2>&1 | tee /tmp/cuprum-make-fmt.log

   set -o pipefail
   make markdownlint 2>&1 | tee /tmp/cuprum-make-markdownlint.log

   set -o pipefail
   make nixie 2>&1 | tee /tmp/cuprum-make-nixie.log

9. Run quality gates (must all pass):

   set -o pipefail
   make check-fmt 2>&1 | tee /tmp/cuprum-make-check-fmt.log

   set -o pipefail
   make lint 2>&1 | tee /tmp/cuprum-make-lint.log

   set -o pipefail
   make typecheck 2>&1 | tee /tmp/cuprum-make-typecheck.log

   set -o pipefail
   make test 2>&1 | tee /tmp/cuprum-make-test.log

## Validation and acceptance

Behavioural acceptance criteria:

- `rust_consume_stream()` returns the same string as
  `payload.decode("utf-8", errors="replace")` for payloads containing invalid
  UTF-8 and for payloads split across chunk boundaries.
- A payload ending in an incomplete UTF-8 sequence yields a final replacement
  character, matching Python's `errors="replace"` behaviour.
- Unsupported encodings or error modes raise `ValueError`.

Quality criteria (what done means):

- Tests: new unit tests and pytest-bdd scenarios pass, and `make test` passes.
- Lint/typecheck: `make lint` and `make typecheck` pass.
- Formatting: `make check-fmt` passes after `make fmt` is applied.
- Markdown: `make markdownlint` and `make nixie` pass after doc changes.

Quality method (how we check):

- Run the commands listed in "Concrete steps" using `set -o pipefail` and
  `tee` logs.

## Idempotence and recovery

All steps are repeatable. If a step fails, fix the issue and re-run the same
command. If the Rust extension is unavailable in the environment, the new
Rust-specific tests should skip via the existing `rust_streams` fixture; do
not remove the tests. No destructive commands are required.

## Artifacts and notes

Keep the following evidence in logs when validating:

- `pytest` output showing the new Rust consume tests passing.
- `make` logs for `check-fmt`, `lint`, `typecheck`, `test`, `markdownlint`, and
  `nixie`.

## Interfaces and dependencies

The following interfaces must exist at the end of implementation:

- Rust function in `rust/cuprum-rust/src/lib.rs`:
  - `rust_consume_stream(reader_fd: int, buffer_size: int = 65536,
    encoding: str = "utf-8", errors: str = "replace") -> str`.
- Python shim in `cuprum/_streams_rs.py`:
  - `rust_consume_stream(reader_fd: int, *, buffer_size: int = 65536,
    encoding: str = "utf-8", errors: str = "replace") -> str`.
- No new dependencies are permitted without escalation.
