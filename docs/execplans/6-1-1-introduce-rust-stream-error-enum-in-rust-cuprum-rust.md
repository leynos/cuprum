# Centralize native stream errors (6.1.1)

Status: DRAFT — awaiting explicit approval before implementation.

This ExecPlan is a living document. Keep Constraints, Tolerances, Risks,
Progress, Surprises & discoveries, Decision log, Outcomes & retrospective,
Conformance basis, and Verification plan current at every milestone.

## Purpose / big picture

Introduce one typed error boundary for the optional native pump and consume
functions. Callers must continue to receive `ValueError` for invalid buffer
sizes and `OSError` for fatal stream failures, including the existing native
error codes and subclasses. Success is observable through the real compiled
extension as well as through Rust tests of the underlying error values.

This implements only roadmap item 6.1.1. It establishes a foundation for 6.1.2
and the later capture-only dispatcher; it does not integrate native consume,
claim decoding parity over the full domain, or claim a performance win. The 20%
wall-time gate against the tuned phase 5 baseline remains future work.

## Constraints

- Obtain explicit approval of this plan before changing implementation or
  tests. This planning pull request contains documentation only.
- Define `RustStreamError` in `rust/cuprum-rust/src/lib.rs`. Keep safe stream
  policy and its existing `PumpError` in `cuprum-streams`; keep native resource
  operations in `cuprum-native-io`.
- Preserve Python names, argument signatures, defaults, returned values,
  exception messages, subclasses, `errno`, and Windows `winerror`.
- Preserve native validation order: buffer, reader conversion, writer
  conversion for pumping, writer adoption, then detached I/O. The Python
  Windows shim has its own earlier descriptor preparation; do not reorder it.
- Preserve borrowed-reader and consumed-writer ownership, cleanup, cancellation,
  Global Interpreter Lock (GIL) release, and non-fatal downstream closure.
- Keep `rust_consume_stream` implemented but not integrated, including its
  production-use guards. Do not alter dispatch, observation events, metrics,
  UTF-8 decoding, allocation limits, or the safe/native crate boundary.
- Keep Rust 1.85 compatibility, caret dependency requirements, strict lints,
  and the 400-line code-file limit. Do not introduce unsafe code or
  suppressions.
- Run gates sequentially through Makefile targets. Use the shared Cargo cache;
  use `/tmp` only for logs and scratch, never as a build target.

## Tolerances (exception triggers)

Stop implementation, record a proposed deviation, set status to BLOCKED, and
request direction if the work needs a public Python signature or exception
contract change, altered ownership or dispatch, a new production abstraction
outside the integration crate, or changes to more than 18 files excluding
lockfiles and this plan. The planned `thiserror` dependency and Rust
behavioural-test dependencies are allowed; any further dependency requires
review. A Rust behavioural-test release incompatible with Rust 1.85 or the
current `rstest` is a decision gate, not permission to upgrade the toolchain.
There is no time limit. Tool failures do not justify lowering acceptance.

## Risks

- High impact, medium likelihood: replacing the existing OS conversion loses
  machine-readable codes. Retain the existing platform-specific helpers and
  exercise actual Python exception attributes on Unix and Windows.
- High impact, low likelihood: moving validation across writer adoption changes
  resource ownership. Retain operation order and run existing hand-off and
  ownership regressions, including validation-before-adoption witnesses.
- Medium impact, medium likelihood: Cargo tests attempt to link Python symbols.
  The crate enables `pyo3/extension-module`; keep Rust tests interpreter-free
  and assert Python exception objects in extension-required Python tests.
- Medium impact, medium likelihood: an optional-extension test silently skips.
  Rebuild the extension and require `make test-extension`; ordinary pytest
  success without the extension does not discharge the boundary contract.
- Medium impact, low likelihood: a broad refactor consumes later roadmap work.
  Retain the existing stream error taxonomy and limit properties to this
  conversion; full decoding parity remains 6.1.2.

## Progress

- [x] (2026-09-19) Read repository instructions and requested skills; inspected
  the current boundary and roadmap at the revision recorded below.
- [x] (2026-09-19) Created the requested local branch; the remote branch did not
  exist at initial inspection. Publication will establish upstream tracking.
- [x] (2026-09-19) Requested two Wyvern reconnaissance passes and an expert
  community review covering all six Logisphere perspectives.
- [x] (2026-09-19) Reconciled expert findings on OS codes, validation order,
  native versus shim behaviour, and Rust behavioural-test compatibility.
- [ ] Validate and publish this plan as a draft pull request.
- [ ] Obtain explicit implementation approval.
- [ ] M1: implement and validate the typed boundary and its tests.
- [ ] M2: reconcile documentation, complete platform evidence, and mark 6.1.1
      done.

## Surprises & discoveries

The task's original location predates the current three-crate split. At the
planning revision, `lib.rs` contains argument validation, while
`stream_pyfunctions.rs::run_stream_operation` already centralizes preparation,
detachment, and the `PumpError` conversion. `errors.rs` already preserves
native OS codes. The missing piece is a typed error unifying argument and
stream failures, not a new stream engine.

`cuprum/unittests/test_rust_streams_boundary_property.py` already contains
Hypothesis boundary coverage. Reuse it instead of creating a second generator
suite. Its Windows exclusions reflect shim preparation order and must not be
removed on the assumption that native and shim ordering are identical.

Two attempts to create a context pack failed because the server rejected an
existing pack larger than its 524288-byte limit. Planning agents exchanged
bounded repository paths and evidence instead; no shared pack was deleted.

## Decision log

- (2026-09-19) Place a crate-private integration enum in `lib.rs`, wrapping
  `PumpError` rather than copying its variants. This preserves ADR-011's
  ownership of safe policy and avoids parallel taxonomies.
- (2026-09-19) Use one `From<RustStreamError> for PyErr` implementation in
  `errors.rs` and one invocation at the common stream-operation boundary.
  Existing OS conversion helpers remain private implementation details.
- (2026-09-19) Retain Python exception construction in the host interpreter.
  Pure Rust tests verify typed values; Python tests verify conversion through
  real entry points. No test-only Python export or embedded interpreter is
  required.
- (2026-09-19) Treat this as a behaviour-preserving boundary consolidation.
  Amend the relevant design section during implementation; a new ADR is not
  needed unless evidence changes the accepted architecture.

## Outcomes & retrospective

Implementation has not begun. The plan targets one focused boundary change; no
roadmap checkbox is completed by publishing it. Record actual red and green
test evidence, gate logs, platform results, and deviations here during delivery.

## Context and orientation

`rust/cuprum-rust/src/lib.rs` defines `validate_buffer_size`, `convert_fd`, and
platform-specific integer-to-descriptor conversion. Both public native exports
live in `rust/cuprum-rust/src/stream_pyfunctions.rs` and use
`run_stream_operation`. That helper validates before transferring the writer,
releases the GIL with `py.detach`, then translates the safe operation's error.

`rust/cuprum-rust/src/errors.rs` translates `PumpError`, retaining raw
operating system codes and stripping Rust's duplicate OS-code suffix. It also
contains pure tests. `rust/cuprum-streams/src/errors.rs` owns `PumpError` and
its non-fatal-write predicate. `BufferSize::new` in that crate's `src/lib.rs`
accepts 1 through 1073741824 bytes and returns stable validation messages.

`cuprum/_streams_rs.py` is the Python shim. Existing unit coverage includes
`cuprum/unittests/test_rust_streams.py`, `test_rust_consume_stream.py`,
`test_rust_streams_boundary_property.py`, `test_rust_errno.py`, and
`test_rust_errno_windows.py`. Behavioural coverage uses
`tests/features/rust_streams.feature` and
`tests/behaviour/test_rust_streams_behaviour.py`. Native consume's inert status
is protected by `cuprum/unittests/test_rust_consume_integration_guard.py`.

## Conformance basis

The repository baseline is commit `861fe2f053645311482141f155baeaa70dca0299`.
There is no separate terms of reference for this task. The governing sources at
that revision are:

- [Roadmap](../roadmap.md), item 6.1.1: typed error and single Python
  conversion.
- [Cuprum design](../cuprum-design.md), section 13: native error propagation,
  backend selection, and descriptor lifecycle.
- [ADR-002](../adr-002-additional-rust-components.md): behavioural reference,
  decoding and descriptor risks; its overall status remains Proposed.
- [ADR-008](../adr-008-rust-pump-observation-channel.md): separate observation
  channel, bounded labels, and cancellation cleanup contracts.
- [ADR-011](../adr-011-audited-rust-boundaries.md) and
  [boundary verification](../rust-boundary-verification.md): the current safe
  policy, native resource, and PyO3 integration boundaries.
- [Profiling baseline](../tee-hotpath-profiling-baseline-2026-06-12.md),
  hypothesis verdicts 3–4: motivation, not performance evidence for this change.
- [Developers' guide](../developers-guide.md), [users' guide](../users-guide.md),
  [documentation style](../documentation-style-guide.md), `AGENTS.md`, and
  `.rules/python-*.md`, especially exception handling, typing, and core style.

Trace R1 (6.1.1 error categories and centralization) through M1 to V1–V3 below.
Trace R2 (ADR-002/011 ownership and native error fidelity) through M1 to V3–V4.
Trace R3 (scope and documentation accuracy) through M2 to the native-consume
integration guard, unchanged observation contracts, and the documentation diff.
Recheck these links at both milestone boundaries.

## Interfaces and dependencies

Define a crate-private `RustStreamError` in `lib.rs`, deriving `Debug` and
`thiserror::Error`, with these variants:

```rust
enum RustStreamError {
    InvalidBufferSize(&'static str),
    InvalidDescriptor(&'static str),
    Stream(PumpError),
}
```

The snippet shows the shape; add `#[error("{0}")]` to the argument variants,
`#[error(transparent)]` to `Stream`, and `#[from]` to its payload. Reuse the
existing `thiserror = "2.0.18"` requirement in the integration crate's manifest
and update `rust/Cargo.lock` without unrelated upgrades.

Change `validate_buffer_size` and `convert_fd` to return
`Result<_, RustStreamError>`, preserving `convert_platform_fd` and its string
contract. Convert messages to the appropriate enum variant. The preparation
closure returns `Result<Operation, RustStreamError>`. Keep the detached
operation's `Result<T, PumpError>` and convert it to the wrapper after detach.
A typed inner closure in `run_stream_operation` performs the existing sequence;
its result is converted once with `map_err(PyErr::from)`. Public pyfunctions
retain `PyResult<u64>` and `PyResult<String>`.

Implement `From<RustStreamError> for PyErr` in `errors.rs`: argument variants
become `PyValueError`; `Stream` exhaustively matches existing `PumpError`
variants and reuses the raw-code/synthetic-I/O conversion logic. Remove the old
`pump_error_to_py_err` entry point rather than retain an alias. Keep the
module-initialization errors and PyO3 argument-extraction errors outside this
stream-domain conversion. No new Python exception class is introduced.

Add `rstest-bdd = "0.5.0"` and `rstest-bdd-macros = "0.5.0"` as dev
dependencies. Their declared minimum Rust version is 1.85; version 0.6.0
requires 1.88 and is excluded. Version 0.5.0 uses `rstest` 0.26.1 in its own
tests, so verify compatibility with this repository's 0.27 using the focused
Rust command and `make msrv-check` before completing M1. If incompatible, stop
at the dependency tolerance rather than upgrade the toolchain. Keep behavioural
tests in an internal test module to access the private enum without widening
its visibility. Record the selected versions and evidence before M1.

## Verification plan

V1: argument validation retains its full signed-64-bit input classification.
Extend pure `rstest` tests in a new
`rust/cuprum-rust/src/stream_error_tests.rs`, registered under `cfg(test)`.
Assert typed variants and existing platform-specific messages for zero,
negative sizes, cap plus one, `i64::MIN`, and `i64::MAX`; use pure validation
for accepted cap values to avoid allocating a gigabyte. Preserve the 32-bit
`usize` overflow message rather than assuming every oversized value reaches the
cap check. Extend proptest across `any::<i64>()` and explicit boundary
witnesses. Preserve existing descriptor properties in `fd_tests.rs`. Every
invalid buffer maps to `InvalidBufferSize`, and descriptor conversion failures
map to `InvalidDescriptor`. A seeded wrong variant must fail the tests. This
checks a forwarding invariant, not a new size policy.

V2: `Stream(PumpError)` preserves its source and existing semantic variants. Use
`rstest` cases for all four `PumpError` variants, synthesized I/O kinds, and a
real raw OS code; inspect the wrapped source without an interpreter. Add Rust
behavioural scenarios in
`rust/cuprum-rust/tests/features/stream_errors.feature`, bound by an internal
`src/stream_error_behaviour.rs` module using `rstest-bdd`. Reuse production
validators and conversions, not a test-only classifier. A mutation discarding
an OS code must fail a source-retention assertion.

V3: the real Python boundary raises the required classes and retains codes.
Extend the existing pytest unit and pytest-bdd modules named above, and reuse
the existing Hypothesis suite, extending its negative lower bound from
`-(1 << 62)` to `i64::MIN`. Check both entry points, native validation
precedence, valid empty input and a short payload, fatal reader I/O, and
existing errno/winerror/subclass assertions. Call the compiled native module
for native-order checks; use platform-aware wrapper fixtures for shim checks.
An invalid buffer and invalid reader together must report the buffer error
without adopting a writer. Integer extraction outside `i64` and wrong Python
types retain PyO3's existing `OverflowError`/`TypeError` behaviour and are not
reclassified as stream validation errors. The native suite must run without
extension-absence skips. A mutation mapping invalid buffers to `OSError` must
fail a real-extension assertion after rebuilding.

V4: resource ownership and non-fatal write policy remain intact. Reuse existing
writer-transfer, reader-validation, debug-abort, and native boundary tests. Use
owned test pipes and explicit transfer bookkeeping, never arbitrary positive
descriptor numbers or a double-close cleanup. Broken pipe/reset on the
downstream write side must still drain and succeed; a fatal read failure must
still raise. Exercise native cleanup with `make boundary-test` and the
extension-required suite. Windows runtime evidence must come from Windows; a
cross-target compile is supplementary evidence only.

The trusted external assumptions are PyO3's `From<E> for PyErr` contract,
CPython's platform-specific `OSError` construction, OS descriptor semantics,
and the existing safe stream engine. Exercise integration with those
interfaces; do not claim to prove their internals. No new arithmetic, decoding,
ownership algorithm, axiom, or business-logic lemma is introduced. Therefore no
new Verus or Kani model is justified for this forwarding change. Retain
existing proofs; if implementation introduces such logic, revise the plan
before proceeding and require a substantive production-linked proof rather than
a duplicate model.

No new output format is introduced: exact stable message assertions and
semantic exception-attribute checks are sufficient. Retain existing consume
snapshots; do not snapshot platform-localized OS messages or whole tracebacks.
If review uncovers a new multivariant output contract, add focused `insta` or
`syrupy` snapshots with semantic assertions and nondeterministic data redacted.
Properties provide sampled evidence, not exhaustive proofs; record seeds and
ensure invalid/valid classes have explicit witnesses without heavy filtering.

## Behavioural specification

The Rust feature describes typed errors without creating Python objects:

```gherkin
Feature: Typed native stream failures
  Scenario: Reject an invalid buffer before stream preparation
    Given a buffer size of zero
    When the native buffer validator checks the size
    Then the error is InvalidBufferSize
    And its message is "buffer_size must be greater than zero"

  Scenario: Retain a native I/O failure
    Given a stream I/O error with a platform error code
    When it becomes a RustStreamError
    Then the stream error retains the original platform error code
```

Extend `tests/features/rust_streams.feature` with the following outline, bound
in `tests/behaviour/test_rust_streams_behaviour.py`. Build fixtures from
existing pipe ownership helpers and keep tests bounded so they cannot block on
a writer left open.

```gherkin
Scenario Outline: Preserve native stream exception categories
  Given the compiled Rust backend is required
  When the <operation> native helper receives <failure>
  Then it raises <exception>

  Examples:
    | operation | failure             | exception  |
    | pump      | a zero buffer size  | ValueError |
    | consume   | a zero buffer size  | ValueError |
    | pump      | a fatal reader error | OSError   |
    | consume   | a fatal reader error | OSError   |
```

## Plan of work and milestones

### M1: deliver the typed boundary and executable contract

After approval, first run focused existing tests for a baseline. Add the pure
Rust tests and behavioural scenarios before production edits. Their initial
failure must identify the absent enum or typed result, not missing
dependencies. Existing Python behaviour may already pass: record it as
characterization, not invented red evidence. Add any missing native contract
regressions and record their baseline outcomes.

Implement the enum, typed validators, common boundary, and converter together.
Remove the obsolete converter and update all affected callers atomically. Run
pure focused tests and their negative controls first. Follow the ordered
full-gate and native-build sequence below; exercise native negative controls
only in the final extension stage, restore them, rebuild, and rerun affected
native checks before committing. Keep test files below 400 lines by moving
cohesive existing test sections if necessary; no unrelated refactor.

Document the internal enum and its reuse scope in the error-propagation section
of `docs/cuprum-design.md`. Explain in `docs/developers-guide.md` why pure Rust
checks and extension-required Python checks are complementary. Clarify the
existing buffer/error contract in the native-backend section of
`docs/users-guide.md`, keeping consume described as not integrated.

The M1 plateau is a passing implementation, tests, and accurate documentation
in one gated atomic commit. R1 and R2 are discharged by V1–V4 on available
platforms; outstanding Windows evidence must remain explicit. Review the diff
against ADR-011 and the ownership sequence before committing. No compatibility
shim or additional public interface is needed. Failed red tests are transient
working-tree evidence, never committed as a broken plateau.

### M2: close evidence and reconcile the roadmap

Obtain remaining platform results, review changed and adjacent code for
cohesion, and reconcile every upstream reference and decision. Run final gates
on the final implementation state. Only after all required evidence passes, use
the `mapsplice` skill to mark exactly 6.1.1 `[x]` in `docs/roadmap.md`. Leave
6.1.2 and later items open. Update this plan's progress, outcome, and status to
COMPLETE, record evidence and remaining phase-6 scope, then gate and commit the
documentation closeout. If design changes prove necessary, record a deviation
and obtain approval rather than silently widening scope.

## Concrete steps and acceptance

Run from the repository root. Load `codegraph-mcp`, `python-router`,
`rust-router`, and `execplans`; the relevant routed skills are `rust-errors`,
`python-errors-and-logging`, `rust-unit-testing`, and `python-testing`. Use
`firecrawl-mcp` for external documentation gaps, `logisphere-experts` for
design review, and `commit-message`/`pr-creation` for publication. Read
`AGENTS.md`, the documentation index, and `.rules/python-00.md` plus the
exception and typing rules before edits. Use `codegraph` for symbol relations.

Scrutineer runs gates sequentially and logs each with `set -o pipefail` and
`tee` under `/tmp`; use a branch-specific log name. For example:

```bash
set -o pipefail
make test-rust TEST_FLAGS='-p cuprum-rust --all-targets --all-features' 2>&1 | tee /tmp/611-rust-focused.out
make test-python PYTEST_TARGETS='cuprum/unittests/test_rust_consume_integration_guard.py' 2>&1 | tee /tmp/611-guard.out
```

The focused Rust command includes the internal unit, property, and behavioural
modules. Before green implementation it must fail because the new typed
contract is absent; afterwards it must pass. Record the actual test names and
counts rather than predicting them.

Before each implementation commit, run the full sequence below in this order.
The first block must use a pure-Python environment without the extension. If
the current environment already contains it, create a task-owned environment
under the worktree, outside `/tmp`, and set `UV_PROJECT_ENVIRONMENT` to that
absolute path for the whole sequence. Do not clear another session's
environment. Verify absence by calling `importlib.util.find_spec` for
`cuprum._rust_backend_native`; it must return `None` before broad tests. If a
source-tree shared library remains visible, stop and resolve the environment
isolation before running broad tests; do not delete an unowned library.

```bash
make fmt
make check-fmt
make lint
make typecheck
make test
make markdownlint
make nixie
make msrv-check
```

Only after the pure-Python gates pass, build and validate native integration:

```bash
set -o pipefail
make develop 2>&1 | tee /tmp/611-develop.out
make test-extension 2>&1 | tee /tmp/611-extension.out
make boundary-test 2>&1 | tee /tmp/611-boundary.out
make lint-windows 2>&1 | tee /tmp/611-windows-compile.out
```

A successful extension build and passing, non-skipped relevant native tests are
required. Rebuild after every native change. Do not subsequently run the broad
Python suite in this native environment; start the sequence with a fresh
pure-Python environment when necessary. Preserve the standard Rust toolchain
for release and verification; use dev-fast only after its prerequisite check.

Capture each command separately with the logging pattern above and preserve its
exit status. Inspect formatter/generated-file changes immediately; retain only
intended changes. Native build and extension checks follow the full pure-Python
sequence. Documentation-only commits need `make fmt`, `make markdownlint`
(including spelling), and `make nixie`, plus diff review.

Acceptance requires one stream-domain conversion entry point in `errors.rs`, no
PyErr construction in argument validators or stream call sites, preserved
exception and ownership evidence, unchanged consume guards, passing gates, and
a roadmap update only at implementation completion. Ordinary PyO3 module
initialization and argument extraction are explicitly outside that count.

## Idempotence and recovery

Tests, builds, and documentation gates can be repeated. Keep failing logs,
inspect the cited failure, fix it, then rerun the affected gate; do not rerun
unchanged passing suites without reason. Retain property regressions discovered
by shrinking. Never reset another contributor's changes. Undo an uncommitted
experiment by restoring only its known patch; undo a published implementation
with a reviewed revert commit and rerun gates. Rebuild the native extension
after a revert so Python does not load stale code.

## Expert review reconciliation

The six-perspective review found no architectural blocker. Pandalump required
one invocation as well as one conversion implementation; Wafflecat preferred
wrapping the existing taxonomy; Telefono required extraction-error exclusions
and platform-specific messages; Doggylump required native/shim ordering tests;
Buzzy Bee required ownership witnesses without performance claims; Dinolump
required pure Rust tests and a verified dependency baseline. These findings are
incorporated above. The remaining implementation-time uncertainty is compiling
Rust behavioural tests with the existing `rstest` version on Rust 1.85.

## Artefacts and notes

The primary external reference, checked through Firecrawl on 2026-09-19, is
[PyO3 0.29 error handling](https://pyo3.rs/v0.29.0/function/error-handling). It
documents custom `From<E> for PyErr` conversion and `Result` propagation. The
repository's existing OS-code conversion remains authoritative for the stronger
Cuprum contract; the generic PyO3 conversion is not a replacement.

Firecrawl also checked the published manifests for
[rstest-bdd 0.5.0][bdd-manifest], [its macros][bdd-macros-manifest], and
[0.6.0](https://docs.rs/crate/rstest-bdd/0.6.0/source/Cargo.toml). This
supports the minimum-version decision, not a claim that the planned test suite
already compiles.

Revision note (2026-09-19): initial draft adapts the roadmap wording to the
current three-crate architecture and distinguishes native validation order from
the Windows shim. Implementation remains pending approval.

[bdd-manifest]: https://docs.rs/crate/rstest-bdd/0.5.0/source/Cargo.toml
[bdd-macros-manifest]: https://docs.rs/crate/rstest-bdd-macros/0.5.0/source/Cargo.toml
