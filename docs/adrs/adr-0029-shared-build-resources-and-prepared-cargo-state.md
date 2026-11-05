# Architectural decision record (ADR) 0029: Shared build resources and prepared Cargo state

## Status

Accepted on 2026-11-05. The Rust test routes and packaging consumers prepare a
content-keyed, read-only Cargo cache snapshot before timed work, give each
top-level run a private writable Cargo home and target directory, record
dependency preparation separately from the workload, and retain a redacted
JSON-lines diagnostic per Cargo invocation while using the external
compiler-admission mechanism unchanged.

## Date

2026-11-05.

## Context and problem statement

Cuprum's Rust routes share several build resources on one host and across
concurrent agents: the Cargo home, Cargo's target directory, the workspace
lockfile, and the host-wide compiler-admission pool. Before this decision, some
timed test workloads fetched dependencies and compiled inside the same budget
intended to measure only test work. A slow fetch or a cold first compile could
therefore exhaust a test's allowance, and a failure reported an exit code
without the Cargo output that explained it.

A shared writable Cargo home made the problem worse in two directions. One run
could observe another run's configuration, or its credentials, while Cargo was
active in the same directory. A cached home could also pin stale settings
behind a key that no longer described them. Cargo coordinates cache access only
within one home, and offline mode does not remove those locks, so handing the
published snapshot itself to a build is not an option.

Issue #633 asks how Cuprum should control shared build resources before timed
test workloads start, without inventing a second admission queue in front of
the one the host already provides.

## Decision drivers

- A timed workload must begin only after dependency preparation has finished.
- A published cache must not retain configuration or credentials that a later
  run could inherit as its own.
- Incompatible Cargo configurations must never share a target directory.
- Shared compiler admission is an external contract; Cuprum must not duplicate
  its accounting or bypass it.
- Every bound must be a finite end-to-end watchdog that owns its process group.
- A failure must leave actionable evidence without retaining secret values.
- Packaging builds are expensive enough that two consumers of the same archive
  should not each build it.

## Options considered

1. **Keep one shared writable Cargo home and raise the timeouts.** Rejected.
   Raising a timer does not stop a fetch or compile from consuming the test
   budget, and a shared writable home still leaks settings and credentials
   between runs.
2. **Add a repository-local admission queue in front of Cargo.** Rejected. The
   upstream `leynos/dev-env-rocky` contract exposes bounded compiler scopes but
   no bounded top-level admission call. A local queue would duplicate the
   shared pool's accounting, and its expiry could not be reported as an
   allocation failure while Cargo still held a lock.
3. **Publish content-keyed read-only snapshots and give each run private
   writable state.** Chosen. Preparation happens before the timed workload,
   snapshots hold cache data only, each run copies a writable home and selects
   its own target directory, and the external admission mechanism is used as-is.

| Factor                          | Shared writable home | Local admission queue | Prepared snapshots |
| ------------------------------- | -------------------- | --------------------- | ------------------ |
| Preparation outside timer       | No                   | Partly                | Yes                |
| Settings and credentials        | Shared               | Shared                | Per run            |
| Duplicates host admission       | No                   | Yes                   | No                 |
| Incompatible targets kept apart | No                   | Yes                   | Yes                |

_Table 1: Comparison of the rejected and chosen approaches._

## Decision outcome

### Content-keyed snapshots

`scripts/cargo_state.py` prepares one snapshot per content key and publishes it
atomically into a snapshot root. `scripts/cargo_state_metadata.py` derives the
key from `rust/Cargo.lock`, `rust/rust-toolchain.toml`, the Cargo version, and
the runner's generation, operating system, architecture, and environment. An
existing snapshot is validated and reused; a new one is staged inside the
snapshot root and renamed into place, so a reader never observes a partial
copy. Published snapshots are made read-only, and their copies contain ordinary
files rather than links back to the snapshot or the caller cache.

### Cache data only

The staging home receives the caller's configuration and credentials because
the locked fetch needs them. Before publication, `remove_cargo_credentials` and
`remove_cargo_settings` delete `credentials*`, `config`, and `config.toml`, so
a published snapshot holds cache data only. Each run home then receives the
caller's current configuration afresh through `scripts/cargo_run_home.py`
(`create_run_cargo_home`), and never its credentials. A reused snapshot
therefore cannot pin stale settings such as `build.rustc-wrapper`, source
replacement, or linkage choices.

### Private run state

`create_run_cargo_home` copies the snapshot into a writable `cargo-home`
beneath a top-level run's root. Each run selects its own `CARGO_TARGET_DIR`.
The boundary harness and the pytest session fixture remove both when the run
ends; the Makefile's target directory persists between runs so later runs can
reuse compiled artefacts. Target directories are keyed by build configuration:
`make test-rust` folds the route, toolchain fragment, test Rust flags, and
doctest Rustdoc flags into the key, and the pytest helper keys its target root
by configuration, so incompatible configurations never share one. The Makefile
contract test `cuprum/unittests/test_cargo_job_count_make_contract.py` pins the
private home, the keyed target directory, and the offline frozen run.

### Preparation recorded separately from the workload

Dependency preparation runs before the timed workload. `boundary_cargo_run` in
`scripts/boundary_cargo.py` records it as the `dependency_preparation` phase
and records the workload as the `workload` phase in `timings.jsonl`;
`scripts/cargo_preparation.py:record_phase` appends both. `make test-rust`
invokes `scripts.prepare_cargo_state` before its test commands and records the
suite as `rust_test_workload`. Packaging builds are recorded as
`packaging_build` by `tests/helpers/distribution_artefacts.py`. The phase
records carry only a name and a duration, never environment data.

### Redacted diagnostics

`scripts/cargo_preparation.py:run_recorded_cargo` retains every preparation
invocation, successful or failed, as one JSON line through
`scripts/cargo_diagnostics.py`. Each record carries the operation, working
directory, Cargo home, exit code, elapsed time, and captured standard output
and error. Values from environment names containing `TOKEN`, `SECRET`,
`PASSWORD`, `CREDENTIAL`, or `AUTH` are replaced with `<redacted>`; values
shorter than eight characters are left alone because they cannot be
distinguished from ordinary words. Where a preparation has a diagnostics sink,
the file sits beside `timings.jsonl` in the run root. The boundary harnesses'
run roots are archived by CI in `rust-boundaries.yml`, so a failed preparation
can be read alongside the steps that preceded it.

### Finite end-to-end watchdogs

Preparation commands are bounded by `CARGO_TIMEOUT_SECONDS` (900 s) in
`scripts/cargo_state_command.py`, with an owned process group. Packaging
frontends are bounded by `tests/helpers/distribution_build.py` at 180 s for uv
and 3600 s for Maturin, also with owned process groups. The
`CUPRUM_PACKAGING_BUILD_TIMEOUT` override can raise either bound but never
lowers a backend below its default. These are end-to-end watchdogs, not
workload-only budgets.

### External compiler admission used as-is

The installed `RUSTC_WRAPPER`/`build-limits-rustc` mechanism is used without
change. A local admission queue was rejected: the upstream contract offers no
bounded top-level admission call, and a local interface would duplicate the
shared pool's accounting. Admission waits therefore still count against the
end-to-end watchdog above.

### Grouped packaging execution

The wheel and source-archive consumers build through one pytest session factory,
`DistributionArtefactFactory` in `tests/helpers/distribution_artefacts.py`.
The consumers carry the `distribution-artefacts` xdist group and
`make test-python` runs grouped targets with `--dist loadgroup`, so each
frontend and archive format is built once per invocation and a second consumer
observes the first build's success or its retained diagnostic.

## Goals and non-goals

- Goals:
  - Move dependency preparation and the first cold compile ahead of the timed
    workload.
  - Keep configuration and credentials out of shared, reusable cache state.
  - Keep incompatible Cargo configurations in separate target directories.
  - Retain redacted, actionable diagnostics for every preparation invocation.
  - Use the external compiler-admission boundary without duplicating it.
- Non-goals:
  - A top-level workload lease or admission bound. That is follow-up work
    tracked in issue #694.
  - Cold, warm, and concurrent-run measurements. These are the stated
    follow-up to this decision, not claims made here.
  - Changing the external admission contract or owning host CPU and memory
    limits.
  - An automatic age-based snapshot pruner.

## Known risks and limitations

- Admission waits still count against every end-to-end watchdog, because no
  workload lease exists. A finite watchdog can therefore expire under heavy
  queueing even when the workload itself is healthy. A top-level workload lease
  is out of scope for #633 and is tracked in issue #694.
- The cold, warm, and concurrent-run measurements are follow-up work. This ADR
  does not establish a workload-only budget, and no measured claim here should
  be read as one.
- Redaction is name-based and skips values shorter than eight characters. A
  secret that does not appear in an environment variable whose name carries a
  marker, or that is shorter than the minimum, is not removed.
- Snapshots remain in the per-user cache under their content keys. Removing
  one is manual: restore write permissions with `make_tree_writable` and do not
  remove a snapshot while a preparation or run-home copy is active. An
  automatic pruner would need to coordinate with active copies.
- A snapshot preparation failure fails the run before the workload starts.
  This is deliberate fail-closed behaviour, but it means a network or registry
  problem surfaces as a preparation failure rather than a test failure.
