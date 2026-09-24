# Architectural decision record (ADR) 016: Stable-ABI native wheels

## Status

Accepted on 2026-09-24. The native extension targets the CPython stable
application binary interface (ABI) from 3.12, so one wheel per platform serves
every supported interpreter.

## Date

2026-09-24.

## Context and problem statement

Cuprum supports CPython 3.12 and newer, but the release workflow built native
wheels with a single interpreter, CPython 3.13. `pyo3` was declared without a
stable-ABI feature, so each wheel was tagged for, and loadable by, exactly the
interpreter that built it. Users on 3.12 and 3.14 silently received the pure
Python wheel and lost Rust acceleration on every platform.

Covering each interpreter separately would multiply the five native build legs
by the number of supported versions. It would also rename the Linux legs' check
contexts, which the `main-required-checks` ruleset names verbatim, and every
new CPython release would need another leg.

## Decision

Enable the `abi3-py312` feature on the `pyo3` dependency in
`rust/cuprum-rust/Cargo.toml`. Maturin then builds a `cp312-abi3` wheel for
each platform, which CPython 3.12 and every later version can load. The floor
matches `requires-python = ">=3.12"` in `pyproject.toml`; raising either means
raising both.

The extension already compiled against the limited API without source changes.
A wheel built with CPython 3.12 was installed under 3.12, 3.13, and 3.14, and
each ran a pipeline through the forced Rust backend.

The build matrix is unchanged. `verify-wheel-install` now requires exactly one
`cp312-abi3` wheel per architecture, runs its full check on 3.12, and repeats
an import and a Rust-backed pipeline on 3.14, the newest supported interpreter,
in the same job so that its check name is unchanged.
`test_maturin_wheel_build_snapshot` asserts the wheel tag, and the wheel
snapshot records the untagged extension file name, so losing the feature fails
both.

## Consequences

- One native wheel per platform covers CPython 3.12, 3.13, 3.14, and later
  releases without workflow changes.
- The extension is restricted to the limited API. A future PyO3 feature that
  needs the full API would have to be weighed against per-interpreter builds.
- Free-threaded CPython builds cannot load `abi3` extensions and continue to
  use the pure Python wheel. Phase 10 of the [roadmap](roadmap.md) plans a
  `cp315-abi3.abi3t` wheel under PEP 803 after 0.2.0.
- The per-interpreter compiler-cache families in CI remain. Switching the
  build interpreter still recompiles `pyo3-ffi`, `pyo3`, and the extension,
  because the pyo3 build script records the interpreter's configuration, so
  archives are not shown to be shareable. Collapsing the families needs a CI
  measurement first; see [CI cache ownership](ci-cache-ownership.md).
- Windows arm64 wheels remain out of scope and are planned for a later beta.
