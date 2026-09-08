# Architectural decision record (ADR) 010: Audited Rust safety boundaries

## Status

Accepted on 2026-09-08. The implementation is in the `cuprum-streams`,
`cuprum-native-io`, and `cuprum-rust` workspace crates. Verification remains in
progress where recorded below.

## Context and problem statement

The original `cuprum-rust` crate combined abstract stream policy with raw
descriptor construction, native reads and writes, Linux `splice`, and an
unchecked UTF-8 conversion. That made the requested `unsafe_code = "forbid"`
policy incompatible with the whole crate, even though the stream state machine
did not require unsafe Rust. Integer range checks at the PyO3 boundary also
could not establish that a descriptor or handle was live, uniquely owned, or
protected from concurrent close and reuse.

The Python pipeline has a corresponding cross-language contract. It pauses the
reader transport, changes both descriptors to blocking mode, duplicates the
writer, submits native work, and waits for native cleanup after cancellation.
That sequence establishes the validity and lifetime assumptions for the native
worker and must remain observable in Python regression tests.

The hand-off also rejects a transport already closing before borrowing its raw
reader descriptor. Asyncio can silently accept `pause_reading()` in that state
while a queued `connection_lost` callback closes the descriptor, so the hop
declines with `READER_PAUSE_FAILED` and the Python fall-back retains its
buffered prefix. The H9 real-EOF regression covers this contract; it does not
establish the cause of the historical native payload mismatch.

## Decision

Split the Rust workspace by responsibility:

- `cuprum-streams` contains safe orchestration, pump state transitions, error
  classification, Linux fallback policy, and checked UTF-8 replacement. Its
  crate root and compile-contract tests forbid unsafe Rust.
- `cuprum-native-io` contains lifetime-bound `AsStream` borrows, unique owned
  stream adoption, single read/write/splice calls, checked progress kernels,
  and Unix/Windows native test fixtures. `borrow` is safe and returns a
  lifetime-bound standard-library borrow. `adopt_writer` and `borrow_reader`
  are the only raw-resource constructors and are unsafe APIs with documented
  caller obligations. The crate denies unsafe operations inside unsafe
  functions.
- `cuprum-rust` stays the thin `cdylib` and PyO3/maturin integration crate. It
  validates Python arguments, releases the Global Interpreter Lock (GIL),
  converts errors, and is the only caller of the unsafe native constructors.
  PyO3-generated ABI code remains an audited integration trust boundary.

The writer contract is implemented through the generic
`cuprum_native_io::with_owned_writer`: the reader is borrowed, the transferred
writer is owned by the scope, and Rust drop elaboration closes it on normal,
error, and real unwind paths. Its callback receives an immutable reference, so
callback code cannot replace or move the owned writer out of the scope. The
normal path explicitly drops the writer after the callback; unwind relies on
automatic RAII. The safe policy crate never receives raw integers. The
avoidable unchecked UTF-8 operation was replaced by checked conversion.

The approved unsafe-bearing crates are limited to `cuprum-native-io` and the
raw PyO3 integration in `cuprum-rust`. Workspace membership is checked by
`scripts/check_boundary_contract.py`; adding another member requires an
explicit inventory decision. The same script compiles deliberate unsafe probes
against a copy of `cuprum-streams` library and compile-test targets, proving
the safe crate's rejection without modifying source files.

## Verification decision

Verify pure, typed progress contracts with Verus first. The proof input is
generated from the exact executable bodies in
`rust/cuprum-native-io/src/progress.rs` by `scripts/render_boundary_proofs.py`;
the correspondence test detects drift. Use Kani for production ownership/drop
and policy obligations where Verus does not practically support
operating-system handles, user-defined destructors, raw pointers, or unwind.
Keep native tests authoritative for kernel resource effects and real panic
unwind. Use Miri on applicable isolated native targets and memory paths,
excluding PyO3 and unsupported operating-system operations with an explicit
record.

The progress Verus run used Verus `0.2026.09.06.8dea4a2`, Rust `1.98.0`, and
prebuilt Z3 `4.16.0`; it verified two functions with zero errors. A direct
assessment of the unchanged production `adopt_writer` body failed because
`OwnedFd` and `FromRawFd::from_raw_fd` are unsupported representations in this
Verus version (`/tmp/issue379-verus-resource-assessment.log`). A separate
generic memory assessment lacks callback preconditions; that incompleteness is
not the sole justification for the Kani fallback. The Verus archive omits Z3,
so the separate prebuilt solver installation is required. The
`leynos/rust-prover-tools` installation succeeds, but its wrapper's version
parser rejects the annotation
`(overridden by environment variable RUSTUP_TOOLCHAIN)`; the local command
therefore invokes the pinned Verus binary directly.

Kani `0.67.0` with compiler `rustc 1.93.0-nightly (53732d5e0 2025-11-20)` and
CBMC `6.8.0` passed 7 native harnesses with 0 failures in the final native run;
see `/tmp/issue379-round27-kani-native.log`. The 11 safe-policy/decoder
harnesses remain current and passed in the complete run recorded at
`/tmp/issue379-round24-kani.log`. The native ownership bounds enumerate
`Normal` and `Error` exits, and the repeated-borrow harness limits borrows to
three with `#[kani::unwind(4)]`; the remaining symbolic and fixed domains are
recorded in the verification matrix. These are bounded results, not an
unbounded proof. Miri `nightly-2026-08-07` uses
`rustc 1.99.0-nightly (84b36a78a 2026-08-06)` and passed all 13 isolated native
tests with zero ignored in `/tmp/issue379-round27-miri.log`. PyO3, unshimmed
`libc::splice`, and unsupported operating-system representations remain
excluded. Round 27's fault run passed its controls and detected all four
mutations in `/tmp/issue379-round27-faults.log`; the real native unwind test
remains authoritative for OS effects.

Round 7's focused Python ABI regression first failed before reader
representability validation moved ahead of writer transfer, then passed all 14
focused tests (`/tmp/issue379-reader-validation-before.log` and
`/tmp/issue379-round7-focused-tests.log`). The validation proves only integer
representability and non-negativity; it does not establish resource validity,
lifetime, or exclusive ownership.

Nine safe-crate unsafe-rejection probes, including a future target without a
crate-level attribute, are covered by the contract gate. The normal callback
compile regression passed with the expected `&W` versus `&mut W` diagnostic in
`/tmp/issue379-round25-trybuild-normal.log`; the final fault run passed its
controls and detected all four mutations in `/tmp/issue379-round27-faults.log`.
Windows and macOS runtime execution remain pending hosted runs; the broader
native full-gate rerun is also pending. Packaging coverage found that
`uv build --sdist` omitted the Rust workspace and that
`uv run maturin sdist --manifest-path rust/cuprum-rust/Cargo.toml` omitted
`rust/rust-toolchain.toml`; the red evidence is in
`/tmp/issue379-native-sdist-before2.log`. Explicit archive entries and the
parametrized `tests/test_native_sdist.py` regression now cover the native
workspace and reject `target` output. Round 24 ran both cases successfully in
26.25 seconds (`2 passed`), and the extracted maturin source archive built a
CPython 3.13 native wheel with all three crates compiling; see
`/tmp/issue379-round24-extracted-wheel.log`. This is a source-archive
completeness correction; the optional pure-wheel backend is unchanged.

## Consequences

- Safe stream functionality can enforce `unsafe_code = "forbid"` without
  weakening native support or hiding unsafe code in an internal module.
- Raw-resource obligations have small, named APIs and one audited caller.
- Existing Python behaviour, optional backend selection, maturin packaging,
  and supported platform paths remain unchanged.
- Native integration tests remain necessary for descriptor/handle lifetime,
  EOF delivery, short and interrupted I/O, broken pipes, and cleanup after
  failure or cancellation.
- Verus, Kani, and Miri have different scopes. Tool limitations, trusted
  syscall and interpreter assumptions, bounds, and excluded targets must stay
  visible in
  [Rust boundary verification and unsafe inventory](rust-boundary-verification.md).

## Supersession and related decisions

This decision refines [ADR-001](adr-001-rust-extension.md) and
[ADR-002](adr-002-additional-rust-components.md) by preserving the optional
PyO3 extension while separating its safe policy and native resource boundaries.
The historical `fd_ownership_model.rs` remains useful provenance for the
earlier bounded model, but it is not the current ownership implementation and
does not prove actual operating-system close or real panic unwind. Existing
verification context in issues [#80], [#81], [#84], [#89], [#125], and [#233]
remains in force. Scheduled Loom interleaving work is a separate deliverable.

[#80]: https://github.com/leynos/cuprum/issues/80
[#81]: https://github.com/leynos/cuprum/issues/81
[#84]: https://github.com/leynos/cuprum/issues/84
[#89]: https://github.com/leynos/cuprum/issues/89
[#125]: https://github.com/leynos/cuprum/issues/125
[#233]: https://github.com/leynos/cuprum/issues/233

The safe crate's descriptor fixtures use `cap_std::fs::File` constructed from
owned descriptors. The Windows native adapter uses the same capability file
wrapper for borrowed-handle I/O, with the production retention kernel
suppressing its owner drop. This avoids carrying the former extension-wide
`std::fs` lint exclusion into either extracted crate; `cap-std` and its handle
adapters are trusted dependencies, not verifier-proved implementations.

The safe crate also declares `unsafe_code = "forbid"` in Cargo's lint table, so
future automatically discovered test, example, and benchmark targets inherit
the prohibition. Cargo cannot override an individual workspace-inherited lint;
the safe manifest therefore carries the workspace lint tables plus this one
addition. The boundary-contract gate requires exact policy parity, preventing
that required duplication from weakening other lints or drifting silently.
