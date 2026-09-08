# Rust boundary verification and unsafe inventory

## Status and scope

This document records the initial unsafe-code audit for issue [issue 379]. The
inventory is anchored to baseline commit
[`34a59eac4cd13de82c3730d4b569eeaefbb5ee61`](https://github.com/leynos/cuprum/tree/34a59eac4cd13de82c3730d4b569eeaefbb5ee61)
and covers Unix, Linux, and Windows conditional code, test targets, and
PyO3-generated foreign-function interface (FFI) paths. It concerns Rust
unsafety only; the separately named `cuprum.unsafe` command namespace is out of
scope.

The implemented architecture has two Rust libraries behind the original
extension crate. `cuprum-streams` owns safe stream policy, pump state
transitions, error classification, and checked UTF-8 decoding, and enforces
`unsafe_code = "forbid"` for library and test targets. `cuprum-native-io` owns
the smallest necessary descriptor/handle and syscall surface. The existing
`cuprum-rust` crate remains the thin PyO3/maturin integration crate and is the
only caller of the raw-resource constructors. The approved unsafe-bearing crate
set is therefore `cuprum-native-io` and the PyO3 integration boundary in
`cuprum-rust`; `cuprum-streams` is the safe crate.

No row below is proof that the target architecture or its verification is
complete. “Existing coverage” describes baseline tests or models; “gap” is work
required by issue #379.

The baseline scan found no `unsafe fn`, `unsafe impl`, or unsafe trait
implementation. Every explicit unsafe block is represented in Table 1; the
generated PyO3 operations are recorded separately because their expansion is
not visible in the source scan.

## Baseline inventory

Table 1. Rust unsafe operations and their proposed boundary.

| Baseline location                              | Operation and obligations                                                                                                                                                                                                                                                                      | Caller or entry point                                                                                  | Proposed destination                                                                                                                     | Existing coverage and gap                                                                                                                                                                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rust/cuprum-rust/src/lib.rs:224-228`          | `OwnedFd::from_raw_fd` reconstructs an owning Unix descriptor. The integer must identify a live descriptor, remain valid during the operation, and not be closed or reused concurrently. The borrow path must suppress the reconstructed owner's drop on normal, error, and real unwind paths. | `rust_pump_stream` and `rust_consume_stream`, ultimately called by Python with a transport descriptor. | `cuprum-native-io` borrowed-reader constructor with a lifetime-bearing safe API; raw validity stays at the audited integration boundary. | `lib_tests.rs` checks borrowed-reader success and panic paths and checks the descriptor remains open. The Kani model checks drop counts only; it does not execute `close(2)` or a real panic unwind.                                           |
| `rust/cuprum-rust/src/lib.rs:246-250`          | `ManuallyDrop` is the ownership guard around the reconstructed reader. Any replacement must preserve caller ownership without a trailing `mem::forget` that is skipped during unwind. The consumed writer must still drop exactly once.                                                        | `pump_stream` and `consume_stream`.                                                                    | `cuprum-native-io` typed borrowed/owned resource handles, with safe stream callers unable to confuse the two.                            | Real descriptor regression tests exist. The historical trailing-`mem::forget` unwind error must remain a deliberate fault in verification or test harnesses.                                                                                   |
| `rust/cuprum-rust/src/lib.rs:253-260`          | `File::from_raw_handle` reconstructs an owning Windows handle. The handle must be a valid, uniquely transferred, pointer-sized native handle and must not be closed or reused concurrently; drop must close it once.                                                                           | Python's Windows handoff, after CRT descriptor conversion and duplication.                             | `cuprum-native-io` Windows ownership boundary.                                                                                           | Baseline source has no equivalent real Windows lifetime/unwind evidence in this crate. Add native handle tests and document Win32/CRT assumptions.                                                                                             |
| `rust/cuprum-rust/src/io_utils/mod.rs:222-228` | `libc::read` receives a live descriptor and a mutable, initialized buffer valid for `buffer.len()` writes. A non-negative result must fit the returned type; `EINTR`, EOF, and errors must preserve valid state.                                                                               | `read_stream` and the pump/consume loops.                                                              | `cuprum-native-io` checked read primitive returning only initialized, in-bounds bytes.                                                   | Baseline pipe, EOF, unreadable-descriptor, and injected-`EINTR` tests exist. Symbolic bounds, pointer/initialization proof, and native short/error tests remain pending.                                                                       |
| `rust/cuprum-rust/src/io_utils/mod.rs:265-277` | `libc::write` receives a live descriptor and an immutable, initialized slice. Partial writes, zero progress, `EINTR`, broken pipes, and byte conversion must not invalidate the slice or overflow accounting.                                                                                  | `classify_write` and the read/write pump fallback.                                                     | `cuprum-native-io` checked write primitive; safe `cuprum-streams` retains policy classification.                                         | Baseline injected partial-write, zero-progress, `EINTR`, broken-pipe, fatal-error, and overflow-adjacent tests exist. Native short writes and verifier witnesses for all accounting branches remain pending.                                   |
| `rust/cuprum-rust/src/splice/mod.rs:69-89`     | `libc::splice` uses two live descriptors, a valid length, and null offsets for pipe endpoints. `EINTR`, unsupported `EINVAL`, partial transfer, broken-pipe drain, and checked byte accumulation must preserve state.                                                                          | Linux pump path, selected before read/write fallback.                                                  | `cuprum-native-io` Linux-only syscall adapter with a safe result contract; policy and fallback remain in `cuprum-streams`.               | Baseline Linux pipe, regular-file fallback, broken-pipe drain, `EINTR`, generated sequence, and overflow tests exist. Verus cannot model the syscall directly; Kani/seam proof, Miri seam coverage, and native kernel evidence remain pending. |
| `rust/cuprum-rust/src/utf8.rs:78-89`           | `str::from_utf8_unchecked` requires that the `valid_up_to` prefix is both in bounds and valid UTF-8. An invalid prefix would create undefined behaviour.                                                                                                                                       | Incremental replacement decoder.                                                                       | Remove this avoidable unsafe operation; use checked conversion in safe `cuprum-streams`.                                                 | Baseline decoder, chunk-boundary, invalid-byte, and Kani model tests exist. The unchecked operation must not be carried into a boundary crate; update or remove its proof target after checked conversion.                                     |
| `rust/cuprum-rust/src/test_support.rs:8-15`    | `libc::pipe` fills a two-element buffer; after success both descriptors are fresh and exclusively owned before `OwnedFd::from_raw_fd`. Failure must not leak a partial result.                                                                                                                 | Unix unit and integration fixtures.                                                                    | Native boundary test support or a safe test seam in `cuprum-native-io`; safe crates must not import this helper.                         | Real descriptor tests depend on it. Move the unsafe setup with its failure contract and keep the tests as external-effect regressions.                                                                                                         |
| `rust/cuprum-rust/src/test_support.rs:18-29`   | `libc::dup` must return a valid new descriptor before `File::from_raw_fd` takes unique ownership; failure and drop must not leak or double-close.                                                                                                                                              | Read/write test helpers.                                                                               | Native boundary test support.                                                                                                            | Existing tests exercise duplicated reads and writes but do not constitute a verifier proof of all setup-failure paths. Add deliberate duplicate/setup faults.                                                                                  |
| `rust/cuprum-rust/src/test_support.rs:56-68`   | `fcntl(F_GETFD)` reads descriptor status without dereferencing memory; `EINTR` must retry and any other failure means closed.                                                                                                                                                                  | `fd_is_open` assertions in Unix tests.                                                                 | Native boundary test support, or a narrowly scoped integration-only helper.                                                              | Useful real-close oracle exists. It must not create an unsafe loophole in `cuprum-streams`; retain it beside native descriptor tests.                                                                                                          |
| `rust/cuprum-rust/src/lib_tests.rs:217-220`    | Test-only `File::from_raw_fd` wraps a borrowed descriptor in `ManuallyDrop`; the test must not accidentally close the caller-owned descriptor.                                                                                                                                                 | Borrowed-reader panic/success regression.                                                              | Native boundary test support with the same borrowed contract.                                                                            | Existing real descriptor test is required evidence and must move with the boundary, rather than being deleted or made safe-looking through an unverified wrapper.                                                                              |

The baseline workspace had only `cuprum-rust` as a Cargo member and declared no
`unsafe_code = "forbid"` lint in `rust/Cargo.toml`. That was the inventory
conflict: abstract stream policy and native operations shared one crate, so
forbidding unsafe code there would reject valid native work. The current split
keeps the lint on `cuprum-streams`, including its test and helper targets, and
confines raw-resource construction to the two approved boundaries.

## Current split

Table 2. Current crate responsibilities and safe/unsafe surfaces.

| Crate                   | Current responsibility                                                                                                                                                                    | Unsafe policy and boundary                                                                                                                                                                                                                                                         |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rust/cuprum-streams`   | Safe stream orchestration, read/write and Linux `splice` policy, pump state machine, errors, and checked UTF-8 replacement.                                                               | `#![forbid(unsafe_code)]` at the crate root and in compile-contract tests. It receives `AsStream` borrows and `OwnedStream` writers from the native boundary.                                                                                                                      |
| `rust/cuprum-native-io` | Lifetime-bound OS borrows, unique writer adoption, single read/write/splice calls, checked native progress, Unix and Windows pipe fixtures, and close observation for native regressions. | Approved unsafe-bearing crate. `borrow` is safe and lifetime-bound; `adopt_writer` and `borrow_reader` are unsafe integration constructors with caller obligations. Every unsafe block has a local safety comment, and the crate denies unsafe operations inside unsafe functions. |
| `rust/cuprum-rust`      | Original `cdylib`/PyO3 module, argument validation, GIL release, error conversion, and the raw Python hand-off.                                                                           | Approved integration boundary. Only this crate calls `adopt_writer` and `borrow_reader`; its generated PyO3 ABI code and explicit call-site safety comments remain subject to the FFI contract.                                                                                    |

The safe writer path is `cuprum_native_io::with_owned_writer`: generic drop
elaboration owns the transferred writer on normal return, error, and real
unwind, while the reader is borrowed for the operation. After a normal callback
return, the scope explicitly drops the writer; unwind relies on automatic RAII.
`fd_ownership_model.rs` now lives under `cuprum-native-io` as historical model
code; it is not the current implementation and does not observe OS close
effects. The production retention kernel used by the Windows adapter and its
real-unwind regression is `memory::with_retained_owner`.

## Current-operation inventory

Table 2a. Remaining unsafe operations and their current callers.

| Responsibility and source                                                                                | Raw operation and local obligation                                                                                                                                                                                                                     | Caller and validation                                                                                                                                                                                                                                            |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cuprum-native-io::adopt_writer` (`src/lib.rs:57`)                                                       | Unix `OwnedFd::from_raw_fd` (`:61`) and Windows `OwnedHandle::from_raw_handle` (`:67`) require a valid, open, uniquely transferred resource that is never concurrently closed or reused. Drop closes once on normal, error, and real-unwind paths.     | PyO3 call at `rust/cuprum-rust/src/stream_pyfunctions.rs:53`; native tests `ownership_tests::{transferred_writer_closes_and_delivers_eof,owned_descriptor_reads_and_closes}` exercise adoption. Kani checks typed drop counts; native tests check close and EOF. |
| `cuprum-native-io::borrow_reader` (`src/lib.rs:78`)                                                      | Unix and Windows `BorrowedStream::borrow_raw` (`:82`, `:88`) require the owner to keep the same resource live for the returned lifetime and prevent close/reuse during GIL release, cancellation, and the callback.                                    | PyO3 calls at `rust/cuprum-rust/src/stream_pyfunctions.rs:57,89`; native regression `ownership_tests::borrowed_reader_survives` exercises the raw borrow contract. Safe `borrow` uses an existing `AsStream` lifetime.                                           |
| Unix single-call I/O (`src/lib.rs:97,110`)                                                               | `libc::read` (`:101`) requires live descriptors and writable initialized storage; `libc::write` (`:114`) requires live descriptors and initialized readable storage. Checked conversion rejects out-of-bounds counts.                                  | `cuprum-streams` owns retry, short-I/O, classification, and accounting policy. Syscall and OS guarantees remain trusted.                                                                                                                                         |
| Linux `splice_once` (`src/lib.rs:136`)                                                                   | `libc::splice` (`:143-152`) requires live pipe endpoints, valid length, and null offsets; partial results, `EINTR`, `EINVAL`, and broken-pipe drain must preserve state.                                                                               | `cuprum-streams` selects fallback and drives the loop. Miri excludes unshimmed `libc::splice`; Linux native tests cover kernel effects.                                                                                                                          |
| Unix fixtures and observation (`src/lib.rs:161,184`)                                                     | `libc::pipe` (`:164`) fills two endpoint slots before `from_raw_fd` (`:170-174`) adopts them. `fcntl(F_GETFD)` (`:188`) observes close state and retries `EINTR` without granting ownership.                                                           | Native fixtures only. The fallible `descriptor_guard` `LockResult<MutexGuard>` serializes close observations in `ownership_tests.rs:16-20`.                                                                                                                      |
| Windows fixtures (`src/windows.rs:15,37`)                                                                | `CreatePipe` (`:20`) fills handle slots before `from_raw_handle` (`:25-29`) adopts them. `GetHandleInformation` (`:41`) observes state without ownership.                                                                                              | Windows tests are written; cross-Clippy passed, while hosted Windows runtime execution remains pending.                                                                                                                                                          |
| Windows borrowed I/O (`src/windows.rs:60`)                                                               | `cap_std::fs::File::from_raw_handle` (`:67`) temporarily reconstructs a file; `with_retained_owner` prevents closing the borrowed handle on return or unwind.                                                                                          | Private adapter used only by `read_once`/`write_once`. Locked dependency is `cap-std 4.0.3`; `rust/dylint.toml` has no filesystem exclusion.                                                                                                                     |
| PyO3 generated ABI (`rust/cuprum-rust/src/stream_pyfunctions.rs:40-41,78-79`; module `src/lib.rs:78-82`) | `#[pyfunction]`, `#[pyo3(signature = ...)]`, `#[pymodule]`, and `wrap_pyfunction!` generate/register wrappers. `Python::detach` (`stream_pyfunctions.rs:26`) requires `Send`, no Python-state access, and valid resource lifetimes through completion. | Explicit raw calls are `adopt_writer` (`stream_pyfunctions.rs:53`) and `borrow_reader` (`:57,89`). PyO3/CPython/asyncio/GIL behaviour remains trusted.                                                                                                           |

There are no unsafe `Send`/`Sync` implementations or unsafe trait
implementations. Auto-trait results for typed handles and private `FnOnce`
callbacks, callback re-entrancy, panic/drop behaviour, and GIL release remain
explicit trust assumptions.

## Generated FFI and cross-language handoff

The following operations contain no explicit `unsafe` token in the baseline,
but are part of the trusted boundary and must be audited with the explicit
operations above:

- `#[pyfunction]` and `#[pyo3(signature = ...)]` on
  `rust/cuprum-rust/src/stream_pyfunctions.rs:40-41` and `78-79` generate
  Python ABI wrappers. `#[pymodule]` at `rust/cuprum-rust/src/lib.rs:78-82` and
  `wrap_pyfunction!` register those wrappers. The compile-pass and compile-fail
  UI tests in `rust/cuprum-rust/tests/ui/` cover macro shape and return-type
  contracts; they do not prove descriptor validity, ownership, or
  generated-wrapper unwind behaviour.
- `Python::detach` at `rust/cuprum-rust/src/stream_pyfunctions.rs:26` releases
  the Global Interpreter Lock (GIL) while native I/O runs. The detached
  operation must be `Send`, must not access Python state, and must keep every
  borrowed resource valid until completion. PyO3, CPython, and the
  operating-system ABI remain trusted dependencies; Miri should exclude the
  interpreter path and test the Rust seam separately.
- `cuprum/_pipeline_stream_fds.py` extracts transport descriptors, pauses the
  reader, and restores it after native completion (`:28-79`, `:111-142`). It
  changes both descriptors to blocking mode and records their previous modes
  (`:145-208`). These steps establish the conditions for a blocking native
  worker and must be undone even when setup or the worker fails.
- `cuprum/_pipeline_streams.py` duplicates the writer before executor
  submission (`:209-252`), passes the reader and duplicate to Rust, and waits
  for cleanup after cancellation (`:176-206`, `:277-312`). A duplicate that
  fails setup or submission must be closed exactly once. Once submitted, Rust
  owns the duplicate and the completion callback must wait for Rust's drop
  rather than close a possibly released or reused descriptor.
- `cuprum/_streams_rs.py` validates buffer sizes before transfer
  (`:114-167`), converts platform values, and on Windows duplicates a native
  handle before closing the CRT descriptor (`:192-250`). A Python integer's
  range is not proof of validity, lifetime, or exclusivity. The caller must
  establish that the extracted reader stays open and caller-owned, that the
  writer duplicate is unique, and that no concurrent transport close or FD
  reuse occurs during the detached operation.

  `_prepare_native_reader` now checks the platform ABI representation before
  writer ownership is transferred. Round 7 first recorded a red focused
  regression run in `/tmp/issue379-reader-validation-before.log`; the corrected
  hand-off passed 14 focused tests, including the boundary-contract script, in
  `/tmp/issue379-round7-focused-tests.log`. This check establishes only that
  the integer can be represented and is non-negative. It does not establish
  that the resource is valid, remains alive, or is exclusively owned. Round
  22's extension-required suite then passed 80 tests with 1 skip after buffer
  validation was restored before reader preparation; the round 21 property test
  caught that precedence regression. It preserves POSIX invalid-buffer error
  precedence and closes the duplicate on validation failure.

These Python functions are not Rust unsafe sites, but their tests are part of
the Rust boundary contract. In particular, preserve pause/block/duplicate/
submit/await-cleanup cancellation regressions, native-load and setup-failure
rollback tests, and Windows duplicate-handle cleanup tests.

### Pinned PyO3 trust inventory

`rust/Cargo.lock` pins `pyo3`, `pyo3-ffi`, `pyo3-macros`, and
`pyo3-macros-backend` at 0.29.2. The following is an audit of the pinned local
sources and declarations. `cargo-expand` is unavailable, so actual macro
expansion was not executed; the macro rows use the pinned code-generation
sources. Cuprum UI tests and Python ABI tests exercise the wrapper surface, but
do not prove PyO3 internals or the CPython ABI.

| Trusted operation (pinned source)                                                                                                                                                                                                                                                                                             | Obligation, caller, and current evidence                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Raw argument arrays and extraction: `pyo3/src/impl_/extract_argument.rs::{extract_arguments_fastcall,extract_arguments_tuple_dict,cast_function_argument,unwrap_required_argument}`; `pyo3-ffi/src/modsupport.rs::{PyArg_ParseTuple,PyArg_UnpackTuple}`; `core::hint::unreachable_unchecked` at the required-argument paths.  | PyO3 macros must establish non-null, valid argument arrays and required-argument presence before the unsafe casts/unreachable branch. Cuprum's `rust/cuprum-rust/tests/ui/` compile tests cover signatures and error shapes; pointer validity and the macro invariant remain assumed.                                                                       |
| Borrowed and owned `PyObject` pointers: `pyo3/src/instance.rs::{Bound::from_borrowed_ptr,Bound::from_owned_ptr,Py::clone_ref,Py::drop_ref}` and `pyo3/src/internal/state.rs::{register_decref,ReferencePool}`.                                                                                                                | The GIL-attached caller must supply a live borrowed pointer or exactly one owned reference; each transfer must balance INCREF/DECREF, including deferred decref and finalization. Cuprum import/error tests exercise the integration, not PyO3's refcount implementation.                                                                                   |
| Method/module C ABI registration and static definitions: `pyo3/src/pyclass/create_type_object.rs::PyClassTypeBuilder::finalize_methods_and_properties`; `pyo3/src/impl_/pymodule.rs::{ModuleDef::init_multi_phase,__pyo3_pymodexport,__pyo3_pyinit}`; `pyo3-ffi/src/modsupport.rs::{PyModule_AddFunctions,PyModule_Create2}`. | Generated `PyMethodDef`/slot arrays, callback signatures, NUL-terminated names, and `PyInit_*`/Python 3.15 `PyModExport_*` symbols must match the selected CPython ABI and remain valid for module lifetime. Cuprum UI/import/build tests cover generated registration; ABI and loader guarantees are dependency assumptions.                               |
| Return, error, and null sentinels: `pyo3/src/impl_/callback.rs::{PyCallbackOutput,IntoPyCallbackOutput::convert}` and `pyo3/src/err/mod.rs::{PyErr::occurred,PyErr::take,PyErr::fetch}`.                                                                                                                                      | A callback must return the sentinel required by its C signature exactly when the Python error indicator is set, and must never dereference a null result. Cuprum error-mapping tests cover observed wrapper behaviour; the C ABI's sentinel contract is assumed.                                                                                            |
| Panic trampoline and attach guard: `pyo3/src/impl_/trampoline.rs::{trampoline,trampoline_unraisable}`, `pyo3/src/impl_/panic.rs::PanicTrap`, and `pyo3/src/internal/state.rs::{AttachGuard::attach,AttachGuard::drop}`.                                                                                                       | No Rust unwind may cross C; panic conversion, double-panic abort, attachment counts, and re-entrancy/finalization rules must hold. Cuprum real-unwind tests cover native resource cleanup only; PyO3's trampoline remains outside Cuprum proofs.                                                                                                            |
| Detach and finalization: `pyo3/src/marker.rs::{Ungil,Python::detach}`, `pyo3/src/internal/state.rs::SuspendAttach`, `pyo3-ffi/src/ceval.rs::{PyEval_SaveThread,PyEval_RestoreThread}`, and `pyo3-ffi/src/pylifecycle.rs::Py_IsFinalizing`.                                                                                    | The detached closure and result must satisfy `Ungil`, use no Python state, restore the same thread state, and avoid attach/decref after finalization. `rust/cuprum-rust/src/stream_pyfunctions.rs:26` plus Python cancellation tests cover Cuprum's hand-off; thread-state, GIL, and interpreter-lifecycle guarantees are trusted PyO3/CPython assumptions. |

## Verification matrix

Table 3. Verification evidence by boundary.

Round 35 integrated local checks are green: the Python suite recorded 1,568
passes and one skip in `/tmp/issue379-round35-test.log`, the Rust suite
recorded 115/115 tests with no skips, and the extension-required suite recorded
80 passes and one skip. The nine unsafe-code compiler probes, Windows
cross-Clippy, and development build also passed. The cached Kani setup check
had already passed in Round 33, which remains retained in the preceding
reports. Hosted Windows/macOS runtime checks and the second CodeRabbit review
remain pending; the first review passed with 0 findings.

| Boundary and production surface                            | Property                                                                                                                                                                   | Selected tool and executed subject                                                                                                                                                          | Assumptions and bounds                                                                                                                                                                                                                                                                | Native or integration evidence                                                                                                                 | Status and limitation                                                                                                                                                                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cuprum-native-io` borrowed and owned Unix/Windows handles | Borrowed readers remain caller-owned; transferred writers close once on normal, error, and real unwind; no released/reused descriptor is touched.                          | Kani exercises the production `with_owned_writer` drop counter where Verus cannot represent OS handles or destructors. The direct Verus resource assessment is recorded below.              | Valid-live-resource, unique-transfer, and no-concurrent-close obligations remain caller contracts. Kani's symbolic exit is bounded and has coverage witnesses. The writer callback receives an immutable reference, so it cannot replace or move the owned resource out of the scope. | Round 35 Rust checks covered 115/115 tests with no skips. Hosted Windows/macOS runtime execution remains pending.                              | Round 27 Kani passed 7 native harnesses; Miri passed 13 tests with 0 ignored; the fault harness detected all 4 mutations.                                                                                               |
| `cuprum-native-io` read/write kernels                      | Buffers expose only initialized in-bounds bytes; short reads/writes, `EINTR`, zero progress, and checked byte accounting preserve valid state.                             | Verus verifies the exact production `progress.rs` bodies rendered by `scripts/render_boundary_proofs.py`; Kani checks the production progress kernels and ownership seam.                   | `checked_progress` rejects overflow and oversized lengths; `record_write_progress` commits tail/count only after both checks. Aggregate pump/consume tracing counts retain saturating-add semantics, so they are not exact beyond `u64::MAX`.                                         | Round 35 Rust checks covered 115/115 tests with no skips. Hosted Windows/macOS runtime execution remains pending.                              | Verus passed 2 functions with 0 errors; Kani/Miri and native tests remain scoped as recorded above. Neither verifier proves syscall effects.                                                                            |
| Linux `splice` adapter and fallback policy                 | Unsupported descriptors fall back; partial/interrupted transfers and broken-pipe drain preserve bytes and termination.                                                     | Kani/Verus cover the typed decision, progress, and accumulation seams; the kernel call itself is external. Miri covers applicable native crate paths but excludes unshimmed `libc::splice`. | Syscall return/error guarantees, symbolic chunk and trace bounds, and non-vacuity witnesses are explicit assumptions or bounds.                                                                                                                                                       | Round 35 local Rust and native checks passed; hosted Windows/macOS runtime execution remains pending.                                          | The syscall is not directly verified or Miri-interpreted because `libc::splice` has no supported shim. Round 27 Miri passed 13 tests with 0 ignored.                                                                    |
| `cuprum-streams` safe policy and decoder                   | Safe crate and all test/helper targets reject unsafe Rust; state machine never writes after closure, stops on EOF, and checked UTF-8 replacement preserves valid prefixes. | Compiler contract checks the copied safe crate and compile tests. Kani covers pure pump policy; checked UTF-8 no longer contains `from_utf8_unchecked`.                                     | No raw resource integers or pointers enter the safe crate; lifetime-bound typed native handles do enter through `AsStream`. Aggregate tracing counts retain saturating-add semantics; per-chunk checked progress is verified separately.                                              | Round 35 Rust checks covered 115/115 tests with no skips; nine compiler probes passed. Hosted Windows/macOS runtime execution remains pending. | Safe-policy/decoder Kani passed 11 harnesses.                                                                                                                                                                           |
| `cuprum-rust` PyO3 integration                             | Generated wrappers expose stable signatures, map errors, release the GIL safely, and preserve the native resource contract.                                                | PyO3 compile UI tests and Python integration tests. Verus/Miri exclude interpreter-generated ABI code; typed Rust seams are verified separately.                                            | PyO3/CPython ABI, `Send`, interpreter lifecycle, and Python transport hand-off are trusted assumptions.                                                                                                                                                                               | Round 35 Python checks recorded 1,568 passes and one skip; the extension-required suite recorded 80 passes and one skip.                       | Local integration checks are green; Python lifetime establishment and hosted Windows/macOS runtime remain outside this evidence. The first CodeRabbit review passed with 0 findings; the second review remains pending. |

Round 22 also found a packaging boundary omission. A real `uv build --sdist`
archive initially omitted the Rust workspace, and
`uv run maturin sdist --manifest-path rust/cuprum-rust/Cargo.toml` initially
omitted `rust/rust-toolchain.toml`; the red evidence is in
`/tmp/issue379-native-sdist-before2.log`. The source include entries and the
parametrized `tests/test_native_sdist.py` regression now cover all three
workspace manifests, source trees, lock/toolchain metadata, and the absence of
`target` output. Round 24 ran both parametrized cases successfully in 26.25
seconds (`2 passed`), and the extracted maturin source archive built a CPython
3.13 native wheel with all three crates compiling; see
`/tmp/issue379-round24-extracted-wheel.log`. The optional pure-Python wheel
backend is unchanged.

The writer callback contract was tightened after an API audit found that an
`&mut` callback could use `mem::replace` to move the actual owned writer out of
its scope. The callback now takes `&OwnedStream`, and the normal path
explicitly drops the writer after the callback while unwind relies on automatic
RAII. The deliberate `replace_scoped_writer.rs` fixture reproduced the old
defect before the fix in `/tmp/issue379-writer-replace-before.log`; the
corrected compile regression passed with the expected `&W` versus `&mut W`
diagnostic in `/tmp/issue379-round25-trybuild-normal.log`.

The tool records must include exact Rust, Verus, Kani, and Miri versions and
reproducible commands. A missing compatible binary is a dependency blocker, not
a passing skip. Miri runs should cover applicable isolated native targets and
in-memory seams; they must name every excluded PyO3, syscall, platform, or
unsupported-representation test and must not disable undefined-behaviour checks.

The correspondence test in `scripts/tests/test_boundary_proof_render.py` checks
that the rendered proof bodies still match the production kernels. The current
Verus run used Verus `0.2026.09.06.8dea4a2`, Rust `1.98.0`, and external
prebuilt Z3 `4.16.0`. `make boundary-verus` first regenerates the input from
the exact production `progress.rs` bodies, then runs the pinned Verus binary.
The default `make test` suite includes `tests/test_native_sdist.py` and the
`scripts/tests/test_boundary_*.py` contract tests. `make boundary-test` runs
all four boundary script contracts together with the native tests. The current
final-source result is `2 verified, 0 errors` in
`/tmp/issue379-round24-verus.log`. The Verus archive does not contain the
solver, so `scripts/install_boundary_z3.py` installs the required prebuilt Z3
separately. The `leynos/rust-prover-tools` installer works for Verus, but its
wrapper's version parser rejects the annotation
`(overridden by environment variable RUSTUP_TOOLCHAIN)`; the Makefile uses the
pinned binary directly rather than silently changing toolchains.

The pinned Z3 asset targets x86-64 Linux with glibc 2.39. Hosted execution
assumes that GitHub's `ubuntu-latest` image currently resolves to Ubuntu 24.04
([runner-images README](https://github.com/actions/runner-images/blob/main/README.md)).
The installer and workflow must fail closed when the architecture, libc, or
image is incompatible; an unavailable compatible binary is a dependency
blocker, never a passing skip.

The direct Verus assessment of the unchanged production `adopt_writer` body
failed because `OwnedFd` and `FromRawFd::from_raw_fd` are unsupported
representations in this Verus version. The exact diagnostic is recorded in
`/tmp/issue379-verus-resource-assessment.log`. This is a representation blocker
for direct resource-boundary verification, not a proof of the unsafe
constructor. A separate generic memory assessment also lacks callback
preconditions; that incompleteness is not the sole reason for the Kani
fallback. Kani covers the typed Rust-owned drop seam, while native tests cover
OS effects and real unwind.

The reproducible resource assessment is:

```bash
uv run python scripts/render_boundary_proofs.py
VERUS_Z3_PATH="$PWD/.cache/boundary-z3/z3" \
  RUSTUP_TOOLCHAIN=1.98.0 \
  "$HOME/.local/share/cuprum-verus-0.2026.09.06.8dea4a2/verus/verus" \
  rust/target/boundary-verification/resource-assessment.rs --crate-type=lib
```

The renderer creates `resource-assessment.rs` from the unchanged production
`adopt_writer` body. Its expected unsupported `OwnedFd`/`FromRawFd` diagnostic
is an assessment limitation, neither a proof nor a successful skip.

Kani is pinned to `0.67.0` with compiler
`rustc 1.93.0-nightly (53732d5e0 2025-11-20)` and CBMC `6.8.0`. The final
native boundary run passed 7 harnesses with 0 failures in
`/tmp/issue379-round27-kani-native.log`; the 11 safe-policy/decoder harnesses
remain current and passed in the complete run recorded at
`/tmp/issue379-round24-kani.log`. The native ownership proofs enumerate
`Normal` and `Error` exits; only `repeated_borrows_never_close_the_reader`
loops, with `borrows <= 3` and `#[kani::unwind(4)]`. Production progress proofs
use symbolic `usize` `count`/`capacity` and `u64` `total`/`remaining`/
`written`, and cover EOF, short progress, invalid lengths, and overflow. Safe
pump proofs use symbolic `PumpState`, `usize` read lengths, and `u64` write
counts; their three-step proof has `#[kani::unwind(4)]`. UTF-8 proofs use fixed
three- and four-byte arrays, with unwind bounds 5 for the first four harnesses
and 4 for the final prefix harness. These are bounded results, not an unbounded
proof. Verus is pinned to `0.2026.09.06.8dea4a2` with Rust `1.98.0` and
prebuilt Z3 `4.16.0`; the production progress proof passed 2 functions with 0
errors in `/tmp/issue379-round24-verus.log`. Miri is pinned to
`nightly-2026-08-07` with `rustc 1.99.0-nightly (84b36a78a 2026-08-06)`; the
final native run passed 13 tests with 0 ignored in
`/tmp/issue379-round27-miri.log`. PyO3, unshimmed `libc::splice`, unsupported
syscall representations, and other excluded targets remain outside that Miri
run.

Round 28 unit and round 29 behavioural runs still report unexplained native
payload mismatches. The isolated diagnostics pass, but no production fix is
claimed while the baseline comparison runs; see the
[issue 379 parity debugging plan](debugging/debugging-plan-20260908-issue379-parity.md).
An archived H7 baseline also produced empty `HELLO` output with a two-stage
exit status of zero, so the symptom predates the boundary extraction.
Separately, the H9 regression showed that an asyncio transport can silently
accept `pause_reading()` while closing; `_pause_reader_transport` now declines
with `READER_PAUSE_FAILED` before raw borrowing, preserving the buffered prefix
in the Python fall-back. This closes the transport race but does not prove that
it caused the historical payload mismatch; the real-EOF regression is recorded
in `/tmp/issue379-reader-lease-before.log` and `after.log`.

## Trusted assumptions and fault sensitivity

The trusted-code inventory currently includes the Rust standard-library drop
implementations for `OwnedFd` and Windows `File`, libc and OS syscall
contracts, the Windows Kernel32 handle API, PyO3's generated ABI glue,
CPython's GIL/thread contract, and Python asyncio transport behaviour. Native
tests can observe some of these effects; Verus and Kani must not present those
assumptions as proofs. The historical Kani ownership model is valuable evidence
for the Rust drop structure and now calls the production
`memory::with_retained_owner` kernel, but it covers only `Normal` and `Error`
outcomes. It makes no simulated panic-unwind claim, and its close log is not an
OS close. The real `catch_unwind` native tests remain authoritative for panic
unwind and resource effects.

The fault harness exercises invalid bounds, incorrect accounting, leaked
writers, and the historical trailing-`mem::forget` unwind defect. The final run
detected all four mutations while its controls passed; see
`/tmp/issue379-round27-faults.log`. `make boundary-faults` regenerates a
disposable copy, runs controls before the mutations, and archives the results
for the scheduled Kani job. Real-descriptor tests remain authoritative for
descriptor state, close effects, EOF delivery, and actual panic unwind; models
and proof seams must identify exactly which production kernel they exercise.

Kani's decoder `Vec`/`String` harnesses also rely on its allocator and
intrinsic models: the logs abstract allocation failure through `assume(false)`
in `alloc::raw_vec::handle_error`/`reserve` and `intrinsics::ToISize`, while
the successful bounded allocations are the exercised domain. They do not cover
out-of-memory behaviour or process abort.

Existing verification provenance must be retained:

- [issue 80]
- [issue 81]
- [issue 84]
- [issue 89]
- [issue 125]
- [issue 233]

The scheduled Loom interleaving harness remains a separate deliverable.

[issue 379]: https://github.com/leynos/cuprum/issues/379
[issue 80]: https://github.com/leynos/cuprum/issues/80
[issue 81]: https://github.com/leynos/cuprum/issues/81
[issue 84]: https://github.com/leynos/cuprum/issues/84
[issue 89]: https://github.com/leynos/cuprum/issues/89
[issue 125]: https://github.com/leynos/cuprum/issues/125
[issue 233]: https://github.com/leynos/cuprum/issues/233

## Hosted installation and code-health follow-up

The first hosted boundary run, `34247183097`, passed Ubuntu and Windows native
contracts. Its Verus installation failed because `UV_PYTHON=3.13` selected an
interpreter incompatible with the pinned `rust-prover-tools` package, which
requires Python 3.14. The Makefile now selects that installer runtime
explicitly; `test_prover_tools_selects_its_required_python` failed before this
correction. The failure log is `/tmp/issue379-hosted-verus-failure.log`. A
fresh hosted proof run remains required; a successful cached local installation
is not its substitute.

CodeScene identified duplicated platform dispatch and complex verification
orchestration. Windows I/O now lives beside its handle adapter, and the scripts
separate response classification, redirect traversal, progress faults,
ownership faults, and unsafe-probe classification. All five affected files
scored 10.0 without findings in `/tmp/issue379-codescene-after-*.json`. The
underlying ownership and progress kernels, symbolic domains, and unsafe
contracts are unchanged. Fresh deterministic gates and a further CodeRabbit
review remain required before publishing these follow-up changes.
