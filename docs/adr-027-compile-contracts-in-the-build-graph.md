# Architectural decision record (ADR) 027: Keep compile-time contracts in the build graph

## Status

Accepted on 2026-10-23.

## Date

2026-10-23.

## Context and problem statement

Cuprum pins several contracts that hold only at compile time: the `PyO3` macro
shapes the extension relies on, the privacy of the pump machine's `Transition`
and `step`, the shared borrow `with_owned_writer` lends its callback, and the
Windows rule that a generic borrowed handle cannot reach synchronous native
I/O. Until this decision they were `trybuild` UI tests run as three nextest
binaries named `compile_tests`.

`trybuild` builds a scratch crate for every case, and that build can never
reuse the workspace's own. trybuild 1.0.118 adds `--cfg trybuild` to the
`RUSTFLAGS` of every crate it compiles (`src/rustflags.rs`), passes an explicit
`--target`, and folds every `[dev-dependencies]` entry into the scratch crate
(`src/run.rs`). Its nested `cargo` also never receives the
`--config tools/dev-fast/config.toml` fragment, so it builds with the LLVM
backend rather than Cranelift. No Cargo fingerprint or `sccache` key therefore
matches the outer build, and each checkout compiles the whole dependency graph,
including the `rstest-bdd` stack, a second time.

That second build ran inside one test's nextest allowance while every compiler
invocation waited for a slot in the host-wide compiler admission pool. The
allowance was raised from 300 s to 600 s, 900 s, 1200 s and finally 1800 s, and
each raise only moved the failure. On 2026-10-20 the outer test build of 176
units took 25m20s with between 66 and 382 admission requests queued ahead, and
`compile_time_ui` then started from an empty scratch directory and had built
122 dependency libraries when the 1800 s allowance expired. Nextest cancelled
the remaining 99 tests. The queue belongs to the host, not the test, so no
per-test timeout can bound it.

## Decision drivers

- A test's timer must bound work the test does, not a resource shared with
  every other agent on the host.
- A compile-time contract should cost one compilation against artefacts the
  build already produced, not a private copy of the dependency graph.
- A compile-fail check must fail for the reason it names, and must not pass
  because an include or import broke.
- Windows-only contracts must still run on a Windows runner.

## Options considered

1. **Raise the allowance again.** Rejected. Four raises did not hold, and the
   bound would still depend on host load rather than on the test.
2. **Move the UI cases into a dedicated crate whose only dev-dependency is
   `trybuild`.** Rejected. It shrinks the nested graph to roughly `PyO3` and
   the Cuprum crates, but it is still a second build per checkout, run inside a
   timer.
3. **Build the trybuild scratch graph in an untimed preparation step.**
   Rejected. It moves the duplicated build rather than removing it.
4. **Compile the contracts in the ordinary build graph.** Chosen. Every case
   becomes a doctest that rustdoc compiles against the workspace's existing
   libraries: compile-pass cases as ordinary doctests, and compile-fail cases as
   `compile_fail` doctests.

## Decision outcome

The `PyO3` compile-pass cases are doctests on the extension's `#[pymodule]` and
on `is_available` in `rust/cuprum-rust/src/lib.rs`: a module that registers
functions, one with a defaulted explicit signature, and the const-evaluable
availability marker. They are not an integration test, because on 2026-10-23
any integration-test binary that referenced the extension crate failed to link
here, with `PyO3`'s Python C-API symbols unresolved under the
`extension-module` feature; the same shapes link and run as doctests.

Each compile-fail case is a `compile_fail,EXXXX` doctest that shares its doc
comment with a positive control: a doctest of the same shape without the
violation, which must compile. The error code ensures the negative case fails
for the reason it names, and the control ensures the snippet's include and
imports resolve, so the negative case cannot pass vacuously. Where one
diagnostic would hide another, each guarded item gets its own negative case;
the pump machine therefore has one for `Transition` and one for `step`. The
doctests sit on:

- the `#[pymodule]` in `rust/cuprum-rust/src/lib.rs`, for the initializer's
  return type;
- the `mod pump_machine` declaration in `rust/cuprum-streams/src/lib.rs`, for
  the privacy of `Transition` and `step`, because a file cannot include itself
  as a module;
- `with_owned_writer` in `rust/cuprum-native-io/src/memory.rs`, for the shared
  writer borrow; and
- `read_once` in `rust/cuprum-native-io/src/windows.rs`, for the synchronous
  capability. This module is compiled only on Windows, so its doctests are
  collected only there.

`make test` and the coverage lane already run doctests. The
`rust-boundaries.yml` native matrix gains a `--doc` run, because
`--all-targets` omits doctests and that matrix is the only Windows runner of
the native crates.

`trybuild` is removed from every manifest, along with the
`binary(compile_tests)` nextest override.
`cuprum/unittests/test_compile_contracts_build_graph.py` refuses a `trybuild`
dependency or lockfile entry, refuses Rust sources that start a nested `cargo`
or `rustc`, requires every `compile_fail` doctest to name an error code and
share its doc comment with a positive control, and pins the inventory of
compile-fail doctests so that one cannot be deleted silently.

## Consequences

### Positive

- No Rust test is bounded by a build. Every nextest test sits on the 300 s
  per-test tier, and the compile contracts cost a handful of rustdoc
  invocations instead of a second dependency graph.
- Seven packages that existed only for `trybuild` leave the lockfile.

### Negative

- A `compile_fail` doctest pins the error code, not the full diagnostic text
  the `.stderr` snapshots compared. A change to the compiler's wording, span,
  or secondary notes no longer fails a test. For the visibility and borrow
  contracts the error code is the contract. For the `PyO3` return-type case it
  is a weaker check of a property that belongs to `PyO3` itself.
- Rustdoc reports a doctest by file and line rather than by a named UI case.

## Assumptions

- Rustdoc collects doctests from private items and from modules compiled only
  under `cfg(windows)`, and compiles each against the package's dependencies.
- The global nextest budget stays at 40 minutes. It was sized to leave ten
  minutes beyond the retired 1800 s tier, and the cargo watchdog and the
  boundary-harness marker are sized against it, so narrowing it is a separate,
  measured change.
