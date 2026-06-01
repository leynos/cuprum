# Architectural decision record (ADR) 002: Additional Rust components

## Status

Proposed.

## Date

2026-05-01.

## Context and problem statement

ADR-001 introduced an optional Rust extension for high-throughput stream
operations. The implementation now accelerates inter-stage pipeline pumping
when Cuprum can extract raw pipe file descriptors, and it preserves the pure
Python path as the behavioural reference implementation.

The current acceleration boundary is intentionally narrow. Pipeline pipe hops
can use `rust_pump_stream()`, including the Linux `splice()` path, but final
stdout and stderr consumption still use Python's asyncio readers. Tee
behaviour, output capture, line callbacks, custom encodings, and subprocess
lifecycle orchestration remain Python-owned. This keeps compatibility simple,
but it leaves measurable overhead in large-output commands and in workloads
that use `capture=True`, `echo=True`, or both.

Cuprum now also has profiling and benchmark tooling that can separate these
costs. The end-to-end throughput benchmark measures pipeline execution across
payload sizes, pipeline depths, callback modes, and stream backends. The tee
hot-path profiling harness measures parent-side capture and echo behaviour with
deterministic fixtures, Linux `perf`, optional `py-spy`, and per-scenario
artefacts under `dist/profiles/`.

The next Rust performance work should therefore expand the extension only where
profiling shows sustained wins, and only where Cuprum can preserve existing
runtime semantics without turning Rust into a second, divergent execution
engine.

## Decision drivers

- Preserve the public API and the `CUPRUM_STREAM_BACKEND` contract from ADR-001.
- Keep pure Python first-class for compatibility, observability, and debugging.
- Improve high-volume subprocess output handling, not only inter-stage pumping.
- Release the Global Interpreter Lock (GIL) around long-running I/O loops.
- Avoid Python per-chunk allocation, decoding, and sink flush overhead where
  Rust can safely own the hot path.
- Make performance claims from profile artefacts, not implementation intuition.
- Keep unsupported behaviours on the Python path rather than approximating
  them in Rust.

## Requirements

### Functional requirements

- Existing `SafeCmd.run`, `SafeCmd.run_sync`, `Pipeline.run`, and
  `Pipeline.run_sync` semantics must remain unchanged.
- Forced `CUPRUM_STREAM_BACKEND=rust` must still fail fast when the native
  extension is unavailable.
- `CUPRUM_STREAM_BACKEND=auto` must continue to fall back to Python when a Rust
  component is unavailable, unsupported, or cannot extract suitable file
  descriptors.
- Line callbacks and observation hooks must preserve ordering and payload text
  exactly as the Python path does today.
- Non-UTF-8 encodings and non-default error policies must stay on Python until
  Rust explicitly supports them with parity tests.
- Timeout and cancellation behaviour must continue to terminate subprocesses
  and return partial captured output where the current Python implementation
  does so.

### Technical requirements

- Native helpers should remain in the existing Rust workspace unless profiling
  identifies a stable boundary that justifies a new crate.
- Rust helpers should operate on raw file descriptors or platform handles, with
  explicit ownership rules for borrowed and consumed descriptors.
- All long I/O loops must release the GIL.
- Python dispatchers must be capability-based: select Rust only when the
  stream configuration, sink, encoding, and lifecycle requirements are known to
  be supported.
- Profile and benchmark output must record enough metadata to compare runs
  across commits and machines.

## Options considered

### Option A: Extend the existing native module incrementally

Add narrowly scoped PyO3 functions to `cuprum._rust_backend_native`, then route
eligible Python call sites to them through the existing backend dispatcher.
Each new Rust component has an explicit Python fallback.

This is the preferred shape. It reuses the current packaging, optional wheel,
availability probe, test matrix, and backend selection model.

### Option B: Add a native subprocess supervisor

Move spawning, pipe wiring, timeout handling, output capture, and pipeline
coordination into Rust as a larger native execution engine.

This may eventually produce the largest win for synchronous high-throughput
workloads, but it has the highest semantic risk. It duplicates timeout,
cancellation, hook, environment, working-directory, and result-shaping logic
that currently lives in Python.

### Option C: Use an external Rust helper process

Spawn a separate helper binary to own subprocess execution and stream copying.

This remains unattractive for the same reasons recorded in ADR-001: it adds
inter-process communication overhead, deployment complexity, and another
lifecycle boundary.

### Option D: Tune only Python buffer sizes and flushing

Increase `_READ_SIZE`, reduce flush frequency, and tune Python buffering.

This can still be a useful control experiment, but it does not remove event
loop round trips, Python object allocation, or GIL contention for large outputs.

The trade-offs are:

- Option A has low to medium compatibility risk, reuses the current wheel, has
  medium to high likely throughput upside, and needs incremental parity tests.
  It is the primary direction.
- Option B has high compatibility risk, reuses the current wheel, has high
  likely throughput upside, and needs full execution parity tests. It remains a
  later experiment.
- Option C has high compatibility risk, adds a helper binary, has medium likely
  throughput upside, and needs helper protocol tests. It remains rejected.
- Option D has low compatibility risk, no packaging impact, low to medium
  likely throughput upside, and uses existing Python tests. It is a baseline
  comparison.

## Proposed direction

Proceed with Option A and treat Option B as a later, evidence-gated experiment.
The Rust extension should grow by adding specific components that remove
measured hot-path costs while leaving unsupported behaviours on the Python path.

### Proposal 1: Add capture-only Rust stream consumption dispatch

Use the existing `rust_consume_stream()` function as the native implementation
for final stdout or stderr consumption when all of the following are true:

- `capture=True`;
- `echo=False`;
- no line callback is registered;
- the effective encoding is `utf-8`;
- the effective error policy is `replace`;
- the stream exposes a suitable raw file descriptor or platform handle.

This would accelerate large-output
`SafeCmd.run(output=RunOutputOptions(capture=True, echo=False))` and final
pipeline output capture without changing public API semantics.

The dispatcher must fall back to `_consume_stream_without_lines()` for custom
encodings, callbacks, echoing, missing descriptors, or any unsupported platform
behaviour.

### Proposal 2: Add raw sink echo and tee helpers

Introduce a Rust helper that consumes a pipe once and can independently:

- append bytes to the captured output buffer;
- write the same bytes to a raw sink file descriptor;
- decode captured bytes as UTF-8 with replacement semantics at the end.

This targets `echo=True` and `capture=True, echo=True` workloads where the sink
has a binary file descriptor, such as standard output, standard error,
`/dev/null`, or a pseudo-terminal sink. Text-only sinks, custom encodings, and
line callbacks should remain Python-owned.

On Linux, this helper can later use `splice()` and `tee()` experiments for
pipe-to-FD echo cases, but the first version should prefer a safe read/write
loop with reusable buffers. That gives the profiling harness a simple candidate
before adding more platform-specific branches.

### Proposal 3: Add Rust-side pump telemetry and buffer tuning

Add optional internal counters for Rust pump and consume operations:

- bytes read and bytes written;
- chunk count and configured buffer size;
- `splice()` attempts, hits, fallbacks, and non-fatal broken-pipe drains;
- elapsed native I/O time.

Expose these counters only to tests, debug logging, or profiling artefacts.
They should not become part of the public runtime API until the stability and
cardinality of the data are proven.

The same work should allow controlled buffer-size experiments without changing
public APIs. The default 64 KiB Rust buffer from ADR-001 should remain the
default until profile data justifies a change.

### Proposal 4: Prototype a synchronous Rust subprocess fast path

For `run_sync()` only, prototype a native subprocess helper for the simplest
safe case:

- no line callbacks or observe hooks;
- `capture` and `echo` modes supported only where Proposals 1 and 2 apply;
- supported UTF-8 replacement semantics only;
- no custom asynchronous cancellation integration beyond existing timeout and
  process termination requirements.

The Rust helper would spawn the process, consume stdout and stderr outside the
GIL, wait for exit, and return pid, exit code, captured output, and timing
metadata to Python. Python would still emit the existing plan, start, and exit
events and shape the public result objects.

This must remain a prototype until it proves that duplicated lifecycle logic
does not create behavioural drift.

### Proposal 5: Defer full native pipeline orchestration

Do not move complete pipeline spawning and coordination into Rust yet. The
current Rust pump already accelerates the dominant inter-stage pipe-copying
path. A full native pipeline runner should wait until profiling shows that
Python spawn coordination, task creation, or final stream collection dominates
after Proposals 1 to 4.

## Profiling and benchmark plan

The existing Cuprum profiling mechanism should be the evidence source for all
five proposals.

### Baseline protocol

Before implementing a component, capture baseline artefacts on the target
branch:

```bash
export RUSTFLAGS="-C force-frame-pointers=yes"
export PYTHONPERFSUPPORT=1
maturin develop --release
uv run python benchmarks/profile_tee_hotpath.py --profiler perf run
make benchmark-e2e
```

Use `--profiler none` for quick smoke comparisons and `--profiler perf` for
call-stack evidence. Use the deterministic fixture generator when the default
fixtures are absent.

### Scenario mapping

- Capture-only consume: profile `tee-devnull-nocb-s1` minus echo-only cost,
  add a capture-only scenario, and benchmark `*-single-nocb` plus
  `*-multi-nocb` with capture enabled.
- Raw sink echo and tee: profile `echo-devnull-nocb-s1`,
  `echo-pty-nocb-s1`, and `tee-devnull-nocb-s1`. Add pipeline benchmark rows
  with `echo=True` when the benchmark matrix grows to cover echo modes.
- Pump telemetry and buffer tuning: profile `echo-devnull-nocb-s4-python` and
  `echo-devnull-nocb-s4-rust`, then compare existing multi-stage
  Python-versus-Rust benchmark rows.
- Synchronous subprocess fast path: profile single-stage echo, capture, and
  tee worker scenarios, then add single-command throughput benchmark rows.
- Native pipeline orchestration: profile multi-stage Python-versus-Rust tee
  scenarios and compare the existing large multi-stage benchmark rows.

### Metrics to add before accepting performance claims

The profiling worker should continue to emit `worker-result.json`, but it
should add the following fields before any proposal is accepted:

- per-repeat wall-time samples, not only aggregate wall time;
- bytes per second and lines per second, derived from existing count fields;
- phase timings for spawn, pump, final consume, sink writes, callbacks, and
  result finalization where the code can measure them cleanly;
- backend component labels, for example `python-consume`, `rust-consume`,
  `rust-pump-splice`, or `rust-tee-readwrite`;
- Rust counters from Proposal 3 when the native path is used;
- reproducibility metadata: git commit, Python version, Rust profile,
  operating system, CPU model, and fixture hash.

The `perf` path should continue to produce `perf.data`, `perf.report.txt`,
`stacks.folded`, and `summary.json`. New Rust components should be accepted
only when those artefacts show the expected hot frames moving out of Python
chunk handling and into native I/O or kernel wait time.

### Acceptance thresholds

A component should not graduate from prototype to default `auto` dispatch
unless benchmark and profile data show all of the following:

- at least 20% lower median wall time for the targeted heavy scenario;
- no more than 5% regression for small-output and fallback scenarios;
- no increase in captured-output memory beyond the expected captured payload;
- no behavioural parity failures across existing unit, behavioural, and
  property-based stream tests;
- no regression beyond the existing 10% Rust ratchet threshold in Continuous
  Integration (CI).

## Migration plan

### Phase 1: Measurement schema

Extend the tee profiling worker result schema, scenario metadata, and summary
documentation with the metrics listed above. Keep existing fields stable so
current artefact consumers continue to work.

### Phase 2: Capture-only consume dispatch

Route eligible capture-only streams through `rust_consume_stream()`. Add unit,
behavioural, and property tests that prove parity for stdout, stderr, empty
streams, invalid UTF-8, large payloads, and forced Python fallback.

### Phase 3: Raw sink echo and tee helper

Add the Rust raw sink helper behind capability checks. Extend the profiling
matrix with explicit capture-only and tee scenarios, including a
pseudo-terminal sink, and keep text-only sinks on the Python path.

### Phase 4: Native subprocess prototype

Prototype the synchronous fast path under an internal feature gate or testing
hook. Compare it against the Python path with the profiling harness before
allowing `auto` dispatch to select it.

### Phase 5: Pipeline orchestration decision

Revisit full native pipeline orchestration only after earlier phases have
profile evidence. Record a new ADR if the project chooses to move process
spawn, pipe wiring, and lifecycle coordination into Rust.

## Known risks and limitations

- Rust capture and tee helpers can drift from Python's exact decoding,
  flushing, and callback semantics if dispatch predicates are too broad.
- Raw file descriptor ownership remains subtle. A Rust helper must not close a
  descriptor still owned by an asyncio transport unless the Python side has
  explicitly transferred that responsibility.
- A native subprocess fast path duplicates timeout and termination behaviour,
  which is correctness-sensitive and platform-dependent.
- Pseudo-terminal and terminal sinks can block differently from `/dev/null` or
  regular files, so raw echo support needs sink-specific profiling.
- More native branches increase wheel test coverage requirements across Linux,
  macOS, and Windows.
- Additional profile metrics can become noisy or expensive if collected during
  normal execution rather than only in benchmark or debug contexts.

## Outstanding decisions

- Should component-level Rust selection use only `CUPRUM_STREAM_BACKEND`, or
  should tests and profiling gain a private component override?
- Which raw sink types should qualify for Rust echo and tee dispatch in the
  first implementation?
- Should Rust learn additional encodings, or should non-UTF-8 output remain a
  permanent Python-only feature?
- Should CI ratchets add dedicated tee hot-path thresholds, or should they
  remain advisory until the scenario matrix stabilizes?

## Architectural rationale

Incremental native components match Cuprum's current architecture better than a
second execution engine. The existing Python code owns command construction,
allowlist enforcement, context propagation, hooks, result objects, and fallback
policy. Rust should own only the hot loops where it can reduce allocation,
copying, GIL contention, or syscall overhead without changing those contracts.

This keeps ADR-001 intact: the Rust extension remains optional, the Python path
remains the behavioural reference, and users keep the same public API.

## References

- ADR-001: `docs/adr-001-rust-extension.md`
- Design specification: `docs/cuprum-design.md` Section 13
- Roadmap: `docs/roadmap.md` Phase 4
- Profiling guide: `benchmarks/README.md`
- Developer profiling notes: `docs/developers-guide.md`
- Current stream implementation: `cuprum/_streams.py`
- Current pipeline stream dispatch: `cuprum/_pipeline_streams.py`
- Current subprocess execution: `cuprum/_subprocess_execution.py`
