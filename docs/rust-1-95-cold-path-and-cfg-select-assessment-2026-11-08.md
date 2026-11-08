# Rust 1.95 cold-path and `cfg_select!` assessment

This document records the assessment requested by issue #508[^1]: whether
`std::hint::cold_path()`[^2] and `cfg_select!`[^3] should be adopted in
Cuprum's native stream boundary and adapters. Both workstreams are **deferred**
at the same gate: the repository's declared source-compatibility floor is Rust
1.87.0, both APIs stabilized in Rust 1.95.0, and three verification routes pin
compilers below 1.95 and compile the candidate files. No benchmark was run and
no implementation result or performance gain is claimed.

## Provenance and scope

Issue #508 records an assessment date of 26 September 2026. The assessment
below was performed on 8 November 2026, on branch
`issue-508-evaluate-rust-1-95-cold-path-hints-and-selective-cfg-select-cleanup-after-stream-migrations`.
The preceding migrations it depends on are complete:

- Roadmap 6.1.1, the typed stream error boundary, landed with its Linux and
  Windows runtime evidence in PR #432[^4].
- #506 recorded the independent modern-stable Rust route in
  [modern-stable validation](modern-stable-validation.md).
- #507 replaced the handwritten anonymous-pipe constructors with
  `std::io::pipe` and raised the floor to Rust 1.87.0[^5].

The issue's ordering constraint therefore holds: the error boundary is stable,
and the platform-specific pipe code the configuration review would otherwise
have polished has already been deleted.

Sources inspected for this assessment were:

- Toolchain and manifests: `rust-toolchain.toml` and `rust/*/Cargo.toml`.
- Stream crate: `lib.rs`, `errors.rs`, `io_utils/mod.rs`, `buffer.rs`,
  `pump_machine.rs`, and `splice/mod.rs` under `rust/cuprum-streams/src/`.
- Native I/O crate: `lib.rs`, `windows.rs`, `progress.rs`, and `memory.rs`
  under `rust/cuprum-native-io/src/`.
- Python boundary: `lib.rs`, `errors.rs`, and `stream_pyfunctions*.rs` under
  `rust/cuprum-rust/src/`.
- Build and verification: the `Makefile`, `scripts/run_loom.py`,
  `scripts/render_boundary_proofs.py`, and the workflows under
  `.github/workflows/`.
- Measurement: `benchmarks/ratchet_history.py`,
  `benchmarks/ratchet_confirmation.py`, and the benchmark harnesses.
- Project documentation.

## Outcome at a glance

| Workstream                                                       | Disposition | Reason                                                                                                |
| ---------------------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------- |
| A: `cold_path()` in the post-6.1.1 error boundary and `io_utils` | Deferred    | Minimum-version gate; no frequency evidence for any candidate; measurement protocol predeclared below |
| B: `cfg_select!` for exclusive Unix/Windows selection            | Deferred    | Same minimum-version gate; readability-only benefit; truth table recorded below                       |

Neither workstream is rejected on merit. Both are blocked by the same decision,
which is not the repository's to make silently.

## The Rust 1.95 gate

### What the two APIs guarantee

`std::hint::cold_path` is `pub const fn cold_path()`, stable since 1.95.0. Its
documentation is explicit that it is a hint: "the exact effect to codegen is
not guaranteed", the compiler "may choose to optimize paths that are not cold
at the expense of paths that are cold", and using it "can actually _decrease_
performance if the branch is called more than expected"[^2]. Benchmarking is
advised by the API's own documentation, not merely by this issue.

`cfg_select!` is stable since 1.95.0. It evaluates a series of `cfg` predicates
at compile time, selects the **first** true one, and emits only that arm. A
trailing `_` supplies a fallback; if no predicate is true and no `_` is
present, a `compile_error!` is emitted. It is usable in item position and in
expression position, with or without braces on the right-hand side[^3].

### Why a newer compiler is not permission

The workspace declares `rust-version = "1.87.0"` in `rust/Cargo.toml`; every
member inherits it with `rust-version.workspace = true`, and
`rust/rust-toolchain.toml` selects channel `1.87.0`. `make msrv-check` re-runs
`cargo check --workspace --all-targets --all-features` through
`RUSTUP_TOOLCHAIN=1.87.0`. Compiling successfully under some newer toolchain
therefore proves only that the newer toolchain accepts the source; it does not
change what the manifest promises, and `make msrv-check` would still fail.

The issue's instruction is followed literally: no build-script version probing,
no polyfill, no no-op fallback hint, no old/new implementation branch, and no
compatibility facade exists or was added. A source that names `cold_path` or
`cfg_select!` simply requires 1.95.

### Compiler-route inventory

Every route that compiles Cuprum's Rust sources was identified and its compiler
measured with `rustc -vV`, except the wheel route, which is not pinned.

| Route                                            | Compiler selection                     | Measured compiler                              |
| ------------------------------------------------ | -------------------------------------- | ---------------------------------------------- |
| Workspace pin and `make msrv-check`              | Rust `1.87.0`                          | `rustc 1.87.0 (17067e9ac 2025-05-09)`          |
| CI routes using the pin                          | Rust `1.87.0`                          | `rustc 1.87.0 (17067e9ac 2025-05-09)`          |
| Loom models (`make loom`, `scripts/run_loom.py`) | Rust `1.87.0`                          | `rustc 1.87.0 (17067e9ac 2025-05-09)`          |
| Kani proofs (`make boundary-kani`)               | Kani `0.67.0` bundled toolchain        | `rustc 1.93.0-nightly (53732d5e0 2025-11-20)`  |
| Whitaker lint (`make lint-whitaker`)             | dylint driver for `nightly-2026-05-28` | `rustc 1.98.0-nightly (57d06900f 2026-05-27)`  |
| rustfmt (`make fmt`, `make check-fmt`)           | `nightly-2026-05-28`                   | `rustc 1.98.0-nightly (57d06900f 2026-05-27)`  |
| Verus boundary proofs                            | Rust `1.98.0`                          | `rustc 1.98.0 (88d9e12ae 2026-08-18)`          |
| Miri (`make boundary-miri`)                      | `nightly-2026-08-07`                   | `rustc 1.99.0-nightly (84b36a78a 2026-08-06)`  |
| modern-stable route                              | Rust/Cargo `1.99.1`                    | `rustc 1.99.1 (58cf8f9c3 2026-10-13)`          |
| Linux debug and Clippy (dev-fast)                | `nightly-2026-08-23`                   | `rustc 1.100.0-nightly (c54751567 2026-08-22)` |
| Wheel build                                      | mutable `stable` via `maturin-action`  | not pinned, not measured                       |

_Table 1: Compiler selected by each build, lint, and verification route._

Two entries in Table 1 need their evidence stated rather than assumed.

**Whitaker.** `make lint-whitaker` was traced with `strace -f -e trace=execve`.
The trace recorded the outer `cargo dylint` launcher resolving to the `1.87.0`
Cargo, but the lint compile itself ran as `cargo check` over the three
workspace packages with `--all-targets` and `--all-features`, executed from the
`nightly-2026-05-28` toolchain's `bin/cargo` under `~/.rustup/toolchains/`. The
workspace crate `cuprum_native_io` was compiled through `build-limits-rustc`
with the dylint driver from the Whitaker bootstrap directory, whose last
components are `dylint-6.3.0/<clippy-hash>/` and
`nightly-2026-05-28/dylint-driver`. That driver reports the `1.98.0-nightly`
version in Table 1, so Whitaker's effective compiler is the
`nightly-2026-05-28` nightly. The trace file was deleted after the versions and
paths above were extracted; it was never committed.

**Continuous Integration.** `ci.yml` installs Rust `1.95.0`, but only for the
Nixie and Merman bootstrap that validates documentation diagrams. It is not a
project compiler and does not compile the crates under assessment. Reading that
install as project support for 1.95 would be a mistake. The CI lint job is not
a 1.87 route either: its Rustdoc and Clippy leaf runs on the dev-fast nightly
and its Whitaker leaf on the `nightly-2026-05-28` driver, both recorded in
their own rows above. The CI routes that compile the crates on the project
toolchain are `make msrv-check` in the lint job, the Rust suite in the coverage
job, the Loom smoke job, the native boundary matrix in `rust-boundaries.yml`,
and `extension-tests-windows`. That Windows job installs `1.87.0` as its
default toolchain and builds the extension through `make develop`, which takes
the dev-fast route only on Linux, so its extension build compiles on the pin.
Its Linux counterpart builds through the same target but selects the dev-fast
nightly, so that job belongs to that row rather than to the 1.87 routes.

### Which routes block adoption

Applying the 1.95 requirement to Table 1 leaves three independent blockers:

| Route                                             | Compiler               | Sources it compiles                                                                     |
| ------------------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------- |
| `make msrv-check` and the CI routes using the pin | `rustc 1.87.0`         | the whole workspace, or the subset each job builds                                      |
| Loom models                                       | `rustc 1.87.0`         | `cuprum-rust` and, through it, `cuprum-streams` and `cuprum-native-io` with `cfg(loom)` |
| Kani proofs                                       | `rustc 1.93.0-nightly` | `cuprum-native-io` and `cuprum-streams` with `cfg(kani)`                                |

_Table 2: Routes whose compiler cannot accept either 1.95 API._

`io_utils/mod.rs` and the platform-selection items in `cuprum-native-io` are
compiled by all three. Kani is a particularly firm blocker:
`KANI_VERSION = 0.67.0` bundles the `1.93.0-nightly` toolchain, so raising the
floor to 1.95 would not be sufficient on its own — Kani would have to move to a
release that bundles a 1.95-or-newer nightly first. Loom's pin is a literal
`RUST_TOOLCHAIN = "1.87.0"` in `scripts/run_loom.py`, so it moves with a
deliberate edit rather than with the manifest.

The remaining routes in Table 1 already accept 1.95, which means the obstacle
is a set of deliberate pins, not a technical limitation. Moving that set is a
reviewed MSRV decision, and nothing in this assessment authorizes it.

## Workstream A: cold-path hints

### Candidate inventory

Candidates were treated as hypotheses. Nothing below is claimed to be cold; the
inventory records what the code does today and why a branch is a candidate at
all.

| ID  | Site                                        | Branch                                               | Current handling                                                                   |
| --- | ------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------- |
| A1  | `io_utils::read_raw_fd_with` (`unix`)       | `usize::try_from` on a non-negative `ssize_t` fails  | `LengthOverflow` plus an `error` event                                             |
| A2  | `io_utils::read_raw_fd_with`                | non-`EINTR` read error                               | `error` event, then `PumpError::Io`                                                |
| A3  | `io_utils::write_all_unix_with`             | `usize::try_from` on a positive `ssize_t` fails      | `LengthOverflow` plus an `error` event                                             |
| A4  | `io_utils::write_all_unix_with`             | write returns zero                                   | `WriteZero` `io::Error`                                                            |
| A5  | `io_utils::write_all_unix_with`             | non-`EINTR`, non-non-fatal write error               | `map_short_write_error` → `error` event, then `Err`                                |
| A6  | `io_utils::record_write_progress`           | length, range, or progress arithmetic fails          | `LengthOverflow` or `BufferRangeExceeded`                                          |
| A7  | `io_utils::write_all_windows`               | write returns zero                                   | `WriteZero` `io::Error`                                                            |
| A8  | `io_utils::write_all_windows`               | non-`EINTR` write error                              | `map_short_write_error`                                                            |
| A9  | `io_utils::read_stream`/`write_all_windows` | `usize::try_from` on a `u32` count fails             | `io::Error::other`                                                                 |
| A10 | `lib::consume_with_reader`                  | `u64::try_from(read_len)` fails; buffer slice fails  | `LengthOverflow`, `BufferRangeExceeded`                                            |
| A11 | `buffer::allocate_buffer`                   | `try_reserve_exact` fails                            | `error` event with category and power-of-two bucket, then `BufferAllocationFailed` |
| A12 | `splice::splice_once_with`                  | conversion fails; non-`EINTR` splice error           | `LengthOverflow`; `PumpError::Io`                                                  |
| A13 | `splice::accumulate_splices`                | `u64::try_from` or `checked_add` fails               | `LengthOverflow`                                                                   |
| A14 | `cuprum_native_io::syscall_result`          | negative result; count conversion; capacity exceeded | `last_os_error`, `io::Error::other`, `InvalidData`                                 |
| A15 | `cuprum_rust::convert_platform_fd`          | descriptor out of range or negative                  | `InvalidDescriptor` → `ValueError`                                                 |
| A16 | `cuprum_rust::errors` conversions           | building the `OSError`                               | `PyOSError` with errno or winerror preserved                                       |

_Table 3: Candidate cold-path sites, recorded as hypotheses only._

Several rows cannot fire on the supported platforms. A1, A3, and A9 fail only
on a count conversion, and the kernel returns a non-negative count that fits
the destination type. A6 needs arithmetic that `checked_progress` has already
bounded. A13 has two failure modes and only one of them is type-impossible:
`u64::try_from` on the kernel's non-negative count cannot fail, but
`total.checked_add(chunk)` can overflow in principle, bounded by the volume a
single pump transfers rather than by the types. "Cannot fire in practice" is a
reason to doubt a performance benefit, not a reason to annotate: unreachable
code that the optimizer can already prove unreachable gains nothing from a
hint, and code it cannot prove unreachable is exactly where an incorrect hint
costs performance.

### Deliberate control flow that must not be annotated

The following are expected control flow, not exceptional failure. Marking them
cold would mislead the optimizer and would misrepresent the stream policy:

- **`EINTR` retries.** The Unix read and write loops retry on interruption and
  count the retry. The splice loop retries too, but it only logs at trace
  level, so its retries have no counter that could be read as frequency
  evidence. A signal during a transfer is normal either way.
- **Broken pipe and connection reset.** `PumpError::is_nonfatal_write`,
  `WriteOutcome::NonFatalShortWrite`, and `WriteEvent::Closed` deliberately
  continue the required reader drain instead of failing.
- **End of file.** `Ok(0)` terminates the pump and the consume loop normally.
- **The first splice call's `EINVAL`.** `try_splice_pump` treats `EINVAL` from
  the first `splice_once` call as evidence that these descriptors cannot splice
  and selects the read/write fallback. After a splice has succeeded, `EINVAL`
  can no longer mean "unsupported", so a later one is fatal like any other
  error. Only that first probe is platform selection rather than failure.
- **Machine transitions.** `pump_machine::advance`'s EOF and drained
  transitions are the loop's ordinary states.
- **Tracing spans and events.** They observe the paths above; they do not steer
  them.

### Constraints beyond the compiler

Four further constraints narrow any future patch, and each is a reason the
patch cannot be mechanical:

- **Verus input is rendered from production source.** `progress.rs`'s
  non-test half is copied verbatim into the Verus input by
  `scripts/render_boundary_proofs.py`. A hint inside `checked_count` or
  `checked_progress` would enter the proof harness, so it would require a
  renderer change and a full `make boundary-verus` run, not only a rebuild.
- **Allocation telemetry is a contract.** The `buffer_allocation_failed`
  category, the power-of-two bucket, and the event fields are observable. A
  hint must not alter the event, the category, or the bucket.
- **The Python boundary preserves raw OS codes where it produces them.** On
  Unix, exception class, message text, `errno`, and validation order are pinned
  by tests. On Windows the stream exports reject raw handles before any
  synchronous I/O, so their tests pin a fixed rejection rather than a
  `winerror`; the five-argument conversion is there for an `io::Error` that
  does carry a native code, and the Rust test pins only that a wrapped failure
  still reports the code it holds. A hint inside the conversion helpers must
  leave both behaviours untouched.
- **The Windows arms are not the Unix arms.** Windows reads propagate
  `Interrupted` to the caller, while Windows writes retry it silently without a
  counter. Windows also wraps count-conversion failures with
  `io::Error::other`, where the Unix arms report `LengthOverflow`. Unifying the
  arms to make a hint tidy would change behaviour.

### Predeclared benchmark protocol

The protocol below is recorded so that a future experiment is judgement-ready
before it is run. It follows the repository's existing ratchet and noise
methodology without importing the phase-6 dispatcher wall-time gate.

1. **Baseline and variants.** The baseline is the same post-migration revision
   as the variant, built by the same compiler for both legs. Each hint is its
   own patch and its own measurement; no patch carries two hints. Both legs
   must share the target triple, the profile, the enabled features,
   `RUSTFLAGS`, the `Cargo.lock` and `uv.lock` digests, and the runtime
   environment. They must differ in exactly one respect: each leg runs the
   artefact built from its own sources. Record `rustc -vV`, `cargo -V`, each
   shared value, and each leg's expected artefact path and content hash
   together with the path and hash it actually used. Verify each run against
   its own build: an interleaved run can otherwise exercise one installed
   artefact twice, or measure two different dependency sets. A run whose
   artefact does not match its own build disqualifies the comparison rather
   than being attributed to the hint. Artefact identity proves which build ran,
   not which backend it took: `get_stream_backend()` resolves once per process
   from `CUPRUM_STREAM_BACKEND`, and an `auto` resolution selects the Python
   pump on Windows and whenever the extension probes unavailable. Each targeted
   run must therefore record the resolved backend from the
   `cuprum.stream_backend_resolved` event, or assert it in the harness, and a
   run recorded as Python is rejected rather than attributed to the hint. The
   Rust label on a scenario and the presence of the extension are not
   substitutes for that observation.
2. **Backend.** Build with the release profile and the released LLVM backend.
   Dev-fast Cranelift results are not release evidence and are excluded.
3. **Workloads.** Use `benchmarks/pipeline_throughput.py` across its payload
   sizes (1 KiB, 1 MiB, 100 MiB), stage depths, and line-callback modes for the
   end-to-end pump scenarios. In `benchmarks/test_stream_microbenchmarks.py`,
   `pump-latency` and `consume-throughput` both drive a command through
   `run_sync`, so they measure the integrated path rather than the native
   consume helper, and the only benchmark calling `rust_consume_stream`
   directly feeds it invalid UTF-8. A consume-path hint therefore needs a
   direct normal-UTF-8 benchmark added beside it. Early-consumer closure,
   interrupted/fatal error, partial-progress write, and zero-write paths need
   controlled stimulus too, and a fatal-error stimulus exercises neither of the
   last two, so those additions belong in benchmark-only in-crate scaffolding
   behind `#[cfg(test)]` or a benchmark feature. The artefact checked in step 1
   is the Python extension module for a Python-driven benchmark and the
   benchmark test executable for an in-crate harness, so it is a different file
   for those two kinds of leg but must still come from the leg's own build.
   Measuring the currently unintegrated consume helper does not authorize
   integrating it, and the integration guard in
   `cuprum/unittests/test_rust_consume_integration_guard.py` stays untouched.
4. **Runs.** Randomize the order of paired runs, state the warmup count and the
   number of rounds, and keep raw samples, exclusions, and failures. Report a
   paired bootstrap interval for the median effect at 95% and a dispersion
   measure for each scenario, and repeat the whole experiment as an independent
   confirmation run.
5. **Decision rule.** Before measuring, state for each targeted scenario the
   smallest effect that would count as practically useful, and record it
   alongside the protocol. A statistically detectable improvement is not by
   itself evidence of value. Adopt a hint only if, in both the primary and the
   confirmation run, its targeted scenario's 95% paired bootstrap interval lies
   wholly above that predeclared threshold. An interval that includes zero, or
   whose lower bound falls short of the threshold, is inconclusive rather than
   an improvement. Every scenario, not only the targeted one, must be reported
   with its interval and dispersion. Every regression must be reported and
   explained; an unexplained regression blocks adoption however small it is. In
   addition, no scenario may regress beyond the larger of the flat 30% floor
   and the three-sigma median-absolute-deviation band from
   `benchmarks/ratchet_history.py` (window size 7, `_MAD_TO_SIGMA = 1.4826`),
   computed on the baseline samples collected for each scenario in that run.
   The CI ratchet's Rust-to-Python ratio, the 20% dispatcher gate, and the 30%
   limits are separate policies and are neither imported nor weakened.
6. **Diagnostics.** Assembly and LLVM IR differences are permitted as
   explanation. They are never evidence of an end-to-end improvement.

An inconclusive or negative result closes the workstream. That is a successful
outcome, not a failure to deliver.

### Why no benchmark was run

The gate in Table 2 blocks adoption whatever an ordinary measurement showed, so
an ordinary result could not change the disposition. An exceptional one could
motivate a separate MSRV proposal — the issue allows that, and the
modern-stable route supplies a compiler above 1.95 that both legs can share —
but no candidate has frequency evidence in the first place: A1, A3, and A9 are
unreachable by construction, A13's conversion half is, and the rest are failure
arms with no measured rate. A credible experiment would still need controlled
stimulus for the early-consumer-closure and interrupted paths, release-profile
builds of both legs on one compiler, and a rebuilt native extension. Deferring
until an MSRV decision, a measured frequency, or a specific performance
question justifies that cost is the disposition this assessment records.

## Workstream B: `cfg_select!` candidates

### Truth table

Each site below is a genuine either-or between `unix` and `windows`, which is
the only shape the macro may be used for here. "Gain" is the readability
benefit after accounting for the fact that `cfg_select!` emits only the
selected arm, so each arm keeps its own doc comments and attributes.

| ID  | Site                                                           | Gain   | Note                                                                                        |
| --- | -------------------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------- |
| B1  | `cuprum-native-io/src/lib.rs`, platform re-exports and imports | Low    | Items have different names and shapes                                                       |
| B2  | `PlatformFd` alias, two adjacent definitions                   | High   | Same name, same documentation                                                               |
| B3  | `borrow` body block expressions                                | High   | Expression position, both arms return a borrow                                              |
| B4  | `adopt_writer`, Unix and Windows definitions                   | Low    | Different safety contracts; retain                                                          |
| B5  | `borrow_reader`, Unix and Windows definitions                  | Low    | Different safety contracts; retain                                                          |
| B6  | `cuprum-streams/io_utils/mod.rs`, platform imports             | Low    | One item per platform; retain                                                               |
| B7  | `read_stream`                                                  | High   | Same name, adjacent, exclusive                                                              |
| B8  | `handle_write`                                                 | High   | Same name, adjacent, exclusive                                                              |
| B9  | `classify_write`                                               | High   | Same name, adjacent, exclusive                                                              |
| B10 | `map_short_write_error`'s `platform` constant                  | High   | Expression position                                                                         |
| B11 | `cuprum-streams/lib.rs`, platform imports                      | Low    | Retain                                                                                      |
| B12 | `pump_stream`                                                  | High   | Same name, exclusive; parameter types differ                                                |
| B13 | `consume_stream`                                               | High   | Same name, exclusive                                                                        |
| B14 | `pump_stream_files`                                            | High   | Same name, exclusive                                                                        |
| B15 | `pump_stream_files_readwrite`                                  | High   | Same name, exclusive; the Windows arm carries a `@codescene` comment that must be preserved |
| B16 | `consume_stream_files`                                         | High   | Same name, exclusive                                                                        |
| B17 | `cuprum-streams/buffer.rs`, `PLATFORM` constant                | High   | Two adjacent constants, same name                                                           |
| B18 | `cuprum-rust/src/lib.rs`, `stream_pyfunctions` module          | Medium | Same name, one arm carries `#[path]`                                                        |
| B19 | `convert_platform_fd`                                          | High   | Same name, adjacent, exclusive                                                              |
| B20 | `cuprum-rust/src/errors.rs`, `os_error_to_py_err`              | High   | Same name, adjacent; Unix uses two arguments, Windows five                                  |

_Table 4: Sites where the macro could clarify an existing exclusive choice._

Twenty sites qualify, fourteen with a high readability gain. The gain is real
but bounded: it removes repeated `#[cfg]` attributes around mutually exclusive
definitions. It does not deduplicate documentation, because rustdoc sees only
the selected arm, and it does not deduplicate per-arm attributes such as the
`#[expect(clippy::too_many_arguments)]` on `stream_pyfunctions`, which each arm
would still need.

### Gates that must not be combined

The macro selects one arm and discards the rest, so any additive or overlapping
gate inside a `cfg_select!` would silently drop code that must be compiled. All
of the following are excluded:

- `target_os = "linux"` splice support, which overlaps `unix` rather than
  excluding it. In a chain, the earlier `unix` arm would match first and
  compile the Linux path out.
- `cfg(test)`, `cfg(kani)`, `cfg(loom)`, and `cfg(miri)`, which add
  configuration rather than choose between platforms.
- Feature gates such as `loom` and the Loom defect fixture.
- Windows test combinations such as `all(test, windows)`.
- The `#[expect]` reasons and `#![forbid(unsafe_code)]` attributes, which are
  not platform selection.

### Unsupported-target behaviour

Today, a target that is neither Unix nor Windows leaves the platform items
undefined, and the failure appears at the use site as an unresolved name. With
`cfg_select!` and no `_` arm, the failure becomes a `compile_error!` at the
selection site, which names the real problem directly. That is a diagnostic
improvement.

Two costs come with it, and both must be accepted explicitly:

- **A `_` arm is prohibited.** A fallback implementation would give
  unsupported targets a working build, changing the contract the issue requires
  be preserved. The macro must therefore be used without `_`.
- **The error moves earlier and gets harder.** A build that previously
  compiled `cuprum-native-io` for a third target without touching the platform
  items would now fail at the selection site. That is unlikely to matter for
  this workspace, but it is a contract change, not a pure refactor.

Because of that second cost, any future patch must observe the diagnostic
before and after rather than assume it. The workspace manifest is
`rust/Cargo.toml`, and rustup does not discover `rust/rust-toolchain.toml` from
the repository root, so both observations must select the same compiler
explicitly:

```sh
rustup target add wasm32-wasip1 --toolchain <toolchain>
cargo +<toolchain> check --manifest-path rust/Cargo.toml \
  -p cuprum-native-io --target wasm32-wasip1
```

That check was **not** run here, because no source changed and an unverified
claim about a diagnostic is worth less than a recorded gap.

Deliberate compile errors that must keep working are the `compile_fail`
doctests on `cuprum-native-io`'s capability types, the `compile_fail` cases on
`with_owned_writer`, and the `E0603` doctests that pin `pump_machine`'s private
transition boundary.

### Validation for a configuration patch

A future `cfg_select!` patch changes shared source, so it needs the full set,
not only the platform it was written for:

- `make msrv-check` at the new floor, because the affected files promise it.
- `make lint`, whose Rustdoc, Clippy, and Whitaker leaves all compile the
  rearranged items and whose spelling leaf checks the new prose, and
  `make check-fmt` for the changed files.
- `make lint-windows` for the cross-target configuration, plus Windows
  execution of each affected implementation: the `rust-boundaries.yml` native
  matrix with its doctests for the stream functions, and the Windows extension
  test for the Python boundary.
- `make loom`, `make boundary-kani`, `make boundary-miri`, and
  `make boundary-verus` where the patch reaches a modelled, proved, or rendered
  item. A rearrangement can change which `cfg` arms those tools compile.
- The unsupported-target `cargo check` above, run before and after, so the
  diagnostic change is observed rather than assumed.
- `make test` for the Rust suite and doctests, including the `compile_fail`
  cases named above.

### Separation from Workstream A

The two patches must stay independent. A configuration-only change that also
carries a hint would make neither attributable: the benchmark could not
separate the hint's effect from a codegen change caused by the rearrangement,
and a reviewer could not accept one without the other. Workstream B is
therefore a separate commit, in a separate proposal, after the gate opens.

## Limitations and remaining gaps

- **Windows runtime is unverified here.** `make lint-windows` type-checks the
  Windows `cfg` branches by cross-compilation; it does not execute them. A
  Table 4 change needs Windows execution of each affected implementation, split
  by layer: the native boundary matrix job in `rust-boundaries.yml` runs the
  `cuprum-streams` and `cuprum-native-io` tests on `windows-2022`, and the
  Windows extension jobs cover the Python boundary but reject raw handles
  before reaching the native pump or consume, so they exercise that rejection
  rather than the stream functions behind it.
- **The unsupported-target diagnostic was not observed.** It is described from
  the macro's documented semantics and the current source, and must be measured
  when a patch exists.
- **No frequency data exists for the fatal-error paths.** A2, A5, A8, A12, and
  A14 have no measurement of how often they are taken in real workloads. A hint
  in any of them is unjustified until such data exists, and gathering it must
  not add per-chunk production telemetry.
- **The wheel compiler is unpinned.** `build-wheels.yml` uses the
  `maturin-action` default, which tracks mutable `stable`. It is recorded as
  unmeasured rather than assumed to be either side of 1.95.
- **Only Linux and Windows were considered.** Other Unix systems use the Unix
  arms; no runtime evidence exists for them in this assessment.
- **The per-route claims come from measurement, not from rebuilding each
  route.** `make msrv-check` passed for this documentation-only change, but no
  Loom, Kani, Verus, or Miri run was performed, because no Rust source changed.
  Table 1 and Table 2 come from measured toolchains and from reading the
  recipes and scripts.

## Resume triggers

Workstream A resumes when a reviewed MSRV change permits Rust 1.95 or newer
across **every** compiler in Table 1 — including Loom and Kani — and a
measurement is commissioned. Adding the API under a newer compiler alone does
not resume it. A measurement may be commissioned earlier to inform such a
proposal, but it cannot adopt the API.

Workstream B resumes under the same gate, at which point the truth table in
Table 4 must be re-derived against that revision rather than reused: the sites,
their exclusivity, and their gains are facts about today's source.

Neither workstream becomes a prerequisite for the phase-6 capture-only
dispatcher or its separate wall-time gate.

## References

[^1]: <https://github.com/leynos/cuprum/issues/508>

[^2]: <https://doc.rust-lang.org/std/hint/fn.cold_path.html>

[^3]: <https://doc.rust-lang.org/std/macro.cfg_select.html>

[^4]: <https://github.com/leynos/cuprum/pull/432>

[^5]: <https://github.com/leynos/cuprum/pull/696>
