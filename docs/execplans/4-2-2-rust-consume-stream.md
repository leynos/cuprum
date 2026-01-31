# Implement Rust consume stream (4.2.2)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: DRAFT

PLANS.md is not present in this repository.

## Purpose / big picture

Deliver `rust_consume_stream()` with incremental UTF-8 decoding that matches
Python's `_consume_stream_with_lines()` behaviour for `errors="replace"`.
Add parity tests for edge cases, update design and user documentation, and
mark roadmap item 4.2.2 as done once all quality gates pass (`make check-fmt`,
`make typecheck`, `make lint`, `make test`). The Rust pathway must remain
optional, with pure Python as the default reference behaviour.

## Constraints

- Pure Python remains first-class; no runtime path may require Rust.
- Follow the documentation style guide and 80-column wrapping in `docs/`.
- Use Makefile targets for linting, formatting, type checks, and tests.
- For long-running commands, use `set -o pipefail` with `tee` for logs.
- Preserve existing public API behaviour; any public API signature change
  requires escalation.
- Rust pathway must release the GIL during blocking I/O.
- Rust `rust_consume_stream()` must implement incremental UTF-8 decoding with
  `errors="replace"`, matching Python behaviour for chunk boundaries and
  final flush semantics.
- Non-UTF-8 or non-`replace` configurations must continue to route to the
  Python pathway (per design constraints).

## Tolerances (exception triggers)

- Scope: touching more than 20 files or 1,000 net lines requires escalation.
- Interfaces: changing any public Python API signature requires escalation.
- Dependencies: adding any new third-party dependency (Python or Rust)
  requires escalation.
- Tests: if new tests still fail after two full fix attempts, stop and
  escalate.
- Time: if a single milestone takes more than 4 hours, stop and escalate.
- Ambiguity: if module naming or API boundary conflicts with existing
  documentation, stop and present options with trade-offs.

## Risks

- Risk: incremental decoding logic diverges from Python's
  `_consume_stream_with_lines()` (especially around replacement characters at
  chunk boundaries). Severity: high Likelihood: medium Mitigation: build
  parity tests that replay edge-case byte sequences and compare outputs.
- Risk: Rust GIL release does not cover the full read/decode loop, reducing
  throughput and introducing contention. Severity: medium Likelihood: medium
  Mitigation: keep the entire read/decode loop inside `Python::allow_threads`.
- Risk: decoding errors are handled differently between Rust and Python,
  causing non-deterministic output differences. Severity: medium
  Likelihood: medium Mitigation: use Rust's incremental decoder configured for
  UTF-8 and `replace`, mirroring Python's incremental decoder semantics.

## Progress

- [x] (2026-01-31 00:00Z) Draft ExecPlan for 4.2.2 implementation.

## Surprises & discoveries

- None yet.

## Decision log

- None yet.

## Outcomes & retrospective

- Pending.

## Context and orientation

Relevant code and documents:

- `cuprum/_streams.py`: Python reference implementation of
  `_consume_stream_with_lines()` and `_consume_stream_without_lines()`.
- `cuprum/_streams_rs.py`: Python shim that re-exports Rust stream functions.
- `rust/cuprum-rust/src/lib.rs`: PyO3 module that will expose
  `rust_consume_stream()`.
- `docs/cuprum-design.md` Section 13: Rust extension architecture and
  limitations, including UTF-8/`errors="replace"` constraints.
- `docs/users-guide.md`: user-facing performance guidance; update for any new
  behaviour or limitations.
- `docs/roadmap.md`: mark 4.2.2 as done after implementation and validation.
- `tests/behaviour/` and `tests/features/`: pytest-bdd behavioural tests.
- `cuprum/unittests/`: unit tests (pytest).

Current state: `rust_pump_stream()` is implemented and exposed through
`cuprum._streams_rs`; `rust_consume_stream()` is not yet implemented. The
Python pathway remains the behavioural reference.

## Plan of work

Stage A: confirm behaviour and API boundaries (no code changes).

Review `docs/cuprum-design.md` Section 13 and the Python `_consume_stream`
variants in `cuprum/_streams.py`. Identify exact behaviours for incremental
UTF-8 decoding with `errors="replace"`, including final flush semantics and
line emission. Confirm that Rust is limited to UTF-8 + `replace` and that
Python remains the fallback for other configurations.

Stage B: write tests first (failing).

Add unit tests in `cuprum/unittests/` for `rust_consume_stream()` that cover:

- Normal consumption with ASCII and multibyte UTF-8 content.
- Chunk boundary splits inside multibyte sequences (replacement behaviour).
- Final flush behaviour when the last chunk ends with an incomplete sequence.
- Invalid file descriptor handling and expected `OSError` mapping.

Add behavioural tests with pytest-bdd that compare Python and Rust outputs for
edge-case byte sequences. The tests must skip cleanly when the Rust extension
is unavailable.

Stage C: implement Rust `rust_consume_stream()` and Python shims.

Extend `rust/cuprum-rust/src/lib.rs` to export `rust_consume_stream()`:

- Signature should match the design doc and stubs (reader FD, buffer size,
  encoding, errors).
- Validate `buffer_size > 0` and raise `ValueError` otherwise.
- Ensure only UTF-8 + `errors="replace"` is accepted; reject other values
  with a clear `ValueError` or route in Python before calling Rust.
- Release the GIL for the full read/decode loop.
- Use an incremental UTF-8 decoder that mirrors Python's `codecs` behaviour.
- Return the decoded string with correct replacement characters for invalid
  sequences, including boundary and final flush handling.
- Map `std::io::Error` to `OSError` for I/O failures.

If the Python shim (`cuprum/_streams_rs.py`) or type stubs need updates, do so
without changing the public API surface.

Stage D: documentation and roadmap updates.

Update `docs/cuprum-design.md` Section 13 to reflect the implemented
`rust_consume_stream()` semantics, especially incremental decoding and
`errors="replace"` behaviour. Update `docs/users-guide.md` to describe when
Rust consumption applies and when it falls back to Python. Mark 4.2.2 as done
in `docs/roadmap.md` after tests and quality gates succeed. Record any design
choices in the design document as required.

Stage E: validation and quality gates.

Run all required checks with Makefile targets and capture logs via `tee`.
Ensure `make check-fmt`, `make typecheck`, `make lint`, and `make test`
succeed. If documentation changes, also run `make markdownlint` and
`make nixie`.

## Concrete steps

All commands run from `/root/repo`. Use `set -o pipefail` and `tee` for long
outputs.

1. Inspect existing documentation and Rust module layout.

```bash
rg -n "rust_consume_stream|_streams_rs" docs
rg -n "consume_stream" cuprum/_streams.py
rg -n "rust_consume_stream" rust
```

2. Add failing unit tests and run them.

```bash
set -o pipefail
uv run pytest cuprum/unittests/test_rust_streams.py \
  | tee /tmp/test-rust-streams-unit.txt
```

3. Add failing behavioural tests and run them.

```bash
set -o pipefail
uv run pytest tests/behaviour/test_rust_streams_behaviour.py \
  tests/features/rust_streams.feature \
  | tee /tmp/test-rust-streams-bdd.txt
```

4. Implement `rust_consume_stream()` in Rust and update Python shims/stubs if
   needed.

5. Re-run the new unit and behavioural tests.

```bash
set -o pipefail
uv run pytest cuprum/unittests/test_rust_streams.py \
  | tee /tmp/test-rust-streams-unit.txt

set -o pipefail
uv run pytest tests/behaviour/test_rust_streams_behaviour.py \
  tests/features/rust_streams.feature \
  | tee /tmp/test-rust-streams-bdd.txt
```

6. Update documentation and roadmap, then run quality gates.

```bash
set -o pipefail
make check-fmt | tee /tmp/make-check-fmt.txt

set -o pipefail
make lint | tee /tmp/make-lint.txt

set -o pipefail
make typecheck | tee /tmp/make-typecheck.txt

set -o pipefail
make test | tee /tmp/make-test.txt
```

7. If docs changed, run markdown validation and Mermaid checks.

```bash
set -o pipefail
make markdownlint | tee /tmp/make-markdownlint.txt

set -o pipefail
make nixie | tee /tmp/make-nixie.txt
```

## Validation and acceptance

Done means:

- New unit tests cover normal transfers, multibyte boundaries, and error
  handling for `rust_consume_stream()`.
- New pytest-bdd behavioural tests verify parity with the Python pathway for
  edge cases and skip cleanly when Rust is unavailable.
- Rust implementation releases the GIL, defaults to a 64 KB buffer, and
  matches Python's incremental UTF-8 decoding with `errors="replace"`.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`
  reflect the Rust consume stream and its limitations.
- `docs/roadmap.md` marks 4.2.2 as done.
- Quality gates pass: `make check-fmt`, `make lint`, `make typecheck`,
  `make test`, plus `make markdownlint` and `make nixie` if docs changed.

## Idempotence and recovery

- Steps are repeatable; re-running tests and formatting is safe.
- If a test fails, fix the issue and re-run the same command to confirm.
- If decoding semantics diverge from Python, pause and update the Decision Log
  before proceeding.

## Artifacts and notes

Expected changes include:

- Rust function in `rust/cuprum-rust/src/lib.rs` implementing
  `rust_consume_stream()`.
- Optional Python shim or stub updates in `cuprum/_streams_rs.py` and
  `cuprum/_streams_rs.pyi` (if present).
- New unit tests in `cuprum/unittests/test_rust_streams.py`.
- New behavioural tests in `tests/behaviour/test_rust_streams_behaviour.py`
  and `tests/features/rust_streams.feature`.
- Documentation updates in `docs/cuprum-design.md` and `docs/users-guide.md`.
- Roadmap update in `docs/roadmap.md` for 4.2.2.

## Interfaces and dependencies

Rust function (PyO3-exposed):

```plaintext
rust_consume_stream(
    reader_fd: int,
    buffer_size: int = 65536,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str
```

- Returns decoded stream content.
- Raises `ValueError` for invalid buffer sizes or unsupported encoding/error
  modes (if not pre-filtered by the Python dispatcher).
- Raises `OSError` for I/O failures.

Python module exposure:

- `cuprum._rust_backend_native` remains the native module name.
- `cuprum._streams_rs` re-exports `rust_consume_stream()` and performs any
  platform-specific file descriptor conversion.

Dependencies:

- No new Python or Rust dependencies expected; if a new crate is required,
  stop and escalate per tolerances.

## Revision note (required when editing an ExecPlan)

Initial draft authored on 2026-01-31.
