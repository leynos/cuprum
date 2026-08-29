# Raise the pure-Python stream read size to the profiled plateau (5.1.1)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`,
`Decision log`, `Outcomes & retrospective`, `Conformance basis`, and
`Verification plan` must be kept up to date as work proceeds.

Status: DRAFT

Roadmap reference: `docs/roadmap.md` item `5.1.1` (phase 5, step 5.1).

`PLANS.md` is not present in this repository, so this plan follows the
`execplans` skill template and the repository instructions in `AGENTS.md`.

## Purpose / big picture

Cuprum reads subprocess output in 4 KiB chunks. The tee hot-path profiling
baseline measured that raising that chunk size reclaims roughly a fifth of
parent-side consume wall time on every scenario it swept, with no change to
observable behaviour. This plan banks that win.

After this change, a user running any Cuprum command or pipeline that captures
or echoes subprocess output gets measurably faster execution for large outputs,
with byte-identical results. Nothing in the public API changes; no new
configuration is introduced.

The work matters beyond its own speedup. Phase 6 of the roadmap proposes
dispatching capture-only consumption into the Rust extension, gated on a 20%
improvement. If the Rust bridge is measured against an untuned 4 KiB Python
baseline, it will be credited for a win that a one-line Python change already
delivers. Landing this first keeps that later gate honest.

This task is complete only when:

1. `_READ_SIZE` is raised from 4096 to a value in the 16-64 KiB range chosen
   from a fresh read-size sweep run on the target machine, not assumed from the
   2026-06-12 baseline document.
2. The `tee-devnull-nocb-s1` workload's median wall time at the chosen value is
   at least 20% below its median wall time at 4 KiB, both measured in the same
   sweep session (see `Decision log`, decision D1).
3. No parity, property, behavioural, or unit test regresses, and the read-size
   independence of stream content is verified by a property test that exercises
   several read sizes rather than only the new one.
4. The chosen value and the sweep artefact are committed alongside
   `docs/tee-hotpath-profiling-baseline-2026-06-12.md` and cross-linked from it.
5. `docs/cuprum-design.md`, `docs/developers-guide.md`, and
   `docs/users-guide.md` are updated, and `docs/roadmap.md` item 5.1.1 is marked
   done.
6. All repository gates pass.

## Constraints

- The public API must not change. `_READ_SIZE` is a private module constant with
  no environment-variable, keyword-argument, or configuration surface, and this
  plan must not give it one. Exposing a tunable is explicitly out of scope.
- The chosen value must lie in the closed range 16384 to 65536 bytes, as
  required by roadmap item 5.1.1. A sweep result outside that range is a
  tolerance breach, not a licence to widen the range.
- Stream content must remain byte-identical for every payload, chunking, and
  encoding. The read size is an internal transfer detail and must never be
  observable in captured output, emitted lines, or echoed bytes.
- The Rust extension must not be modified. `rust/cuprum-rust/src/lib.rs` already
  defaults `buffer_size` to 65536 and caps it at `MAX_BUFFER_SIZE` (1 GiB);
  those values are independent of `_READ_SIZE` and stay untouched.
- Keep scope bounded to roadmap item 5.1.1. Do not implement the per-line
  event-emission work of step 5.2, and do not wire `rust_consume_stream` into
  production (phase 6). The test asserting that production code does not
  reference `rust_consume_stream`
  (`cuprum/unittests/test_rust_streams.py:360-389`) must continue to pass.
- Keep documentation aligned in `docs/cuprum-design.md` (§13.1, §13.5),
  `docs/developers-guide.md` ("Canonical stream-drain loop"),
  `docs/users-guide.md` ("Performance extensions"), and `docs/roadmap.md`.
- Markdown follows `docs/documentation-style-guide.md`: prose wrapped at 80
  columns, code blocks at 120, sentence-case headings, en-GB-oxendict spelling,
  and a caption below every table.
- Benchmark measurement must not run concurrently with repository gates or with
  other agents' builds. Contended measurement invalidates the sweep and can
  corrupt the shared coverage database.

## Tolerances (exception triggers)

- Scope: if implementation requires changes to more than 16 files, stop and
  escalate.
- Acceptance gate: if the fresh sweep's best in-range value does not reach a 20%
  median improvement over the same-session 4 KiB control, stop and escalate.
  Do not widen the range, re-pick the baseline, or relax the threshold to make
  the number fit. Report the measured curve and await direction.
- Range: if the measured plateau falls outside 16-64 KiB (for example, if 128
  KiB is materially better, or if 8 KiB is already at plateau), stop and
  escalate rather than choosing an out-of-range value.
- Interface: if any public API signature must change, or if `_READ_SIZE` appears
  to need a configuration surface to complete the task, stop and escalate.
- Dependencies: if a new external dependency is required, stop and escalate.
- Regression: if any existing test fails at the new read size and the failure is
  not a test that hard-codes the old value, stop and escalate. A genuine
  behavioural difference across read sizes is a defect in the drain loop, not a
  test to adjust.
- Memory: if peak resident memory for the concurrent-pipeline scenarios rises by
  more than 5% at the chosen value, stop and escalate.
- Iterations: if gates still fail after 3 fix attempts, stop and escalate.
- Ambiguity: if the sweep produces a curve with no clear plateau (for example,
  monotonic improvement across the whole range), stop and present the options.

## Risks

- Risk: `_READ_SIZE` exists as two independent module-level bindings, so a
  partial override silently measures the wrong thing.
  Severity: high. Likelihood: high (it has already happened once).
  Mitigation: the 2026-06-12 sweep patched only `cuprum._streams._READ_SIZE`
  and therefore never varied the pump path. Route every override through one
  helper that sets both bindings and asserts they agree afterwards, and add a
  unit test that both modules report the same value.

- Risk: the acceptance gate as literally written in the roadmap is not
  achievable from the historical numbers.
  Severity: high. Likelihood: certain (arithmetic, not chance).
  Mitigation: Table 3 records tee at 13.50 s (4 KiB) and 10.98 s (64 KiB), an
  18.7% improvement, which is below the stated 20% gate. Decision D1 resolves
  this by measuring a fresh same-session 4 KiB control. If the fresh delta is
  also below 20%, that is a real result and triggers the acceptance-gate
  tolerance above.

- Risk: the boundary property test base64-encodes its payload into a single
  argv entry, and Linux caps one argument at `MAX_ARG_STRLEN` (131072 bytes).
  Severity: high. Likelihood: high if unaddressed.
  Mitigation: measured on the target machine, a payload of 98304 bytes fails
  with `E2BIG`; 66048 bytes (64 KiB + 512) encodes to 88064 bytes and succeeds.
  The chosen value plus `_BOUNDARY_DELTA` must stay at or below roughly 98300
  bytes. At 64 KiB this holds with about 33% headroom. Record the ceiling in the
  test module so a future increase fails loudly rather than mysteriously.

- Risk: larger reads increase transient memory per concurrent stream.
  Severity: medium. Likelihood: medium.
  Mitigation: peak transient cost per active stream rises from 4 KiB to the
  chosen value; at 64 KiB and 1000 concurrent streams that is roughly 64 MB
  rather than 4 MB. The capture buffer itself is unchanged. Measure the
  concurrent scenarios, hold to the 5% memory tolerance above, and document the
  trade-off in the users' guide.

- Risk: pipe capacity on this machine degrades under concurrent-agent load,
  deadlocking tests that write more than capacity before a reader drains.
  Severity: medium. Likelihood: medium.
  Mitigation: default capacity is 65536 bytes but shrinks under load
  (`/proc/sys/fs/pipe-user-pages-soft`). Larger boundary payloads make this more
  likely. Keep new tests feeding data concurrently through
  `tests/helpers/stream_pipes.py`, and prefer in-process `asyncio.StreamReader`
  fixtures over real pipes for the read-size invariance property.

- Risk: benchmark noise makes the 20% gate unreproducible.
  Severity: medium. Likelihood: medium.
  Mitigation: the repository has already been bitten by a single noisy run
  becoming a published ratchet baseline (cuprum issue #219). Use a median of at
  least five measured repeats per sweep point, record every raw sample in the
  committed artefact, and run the sweep with no other gate or build active.

- Risk: raising the constant also changes the inter-stage pump path, whose
  behaviour the original sweep never measured.
  Severity: medium. Likelihood: high.
  Mitigation: `_READ_SIZE` is read by `_relay_chunks` and `_drain_stream_reader`
  in `cuprum/_streams_pump.py` as well as by `_drain` in `cuprum/_streams.py`.
  Include the multi-stage scenarios `echo-devnull-nocb-s4-python` and
  `echo-devnull-nocb-s4-rust` in the sweep so the pump path is measured for
  regression, not merely assumed neutral.

- Risk: at a 64 KiB read size, the existing general property test (payloads of
  0-1024 bytes) no longer crosses a read boundary at all, silently reducing
  multi-chunk coverage.
  Severity: medium. Likelihood: certain if unaddressed.
  Mitigation: the new read-size invariance property test exercises small read
  sizes explicitly, restoring and strengthening multi-chunk coverage
  independently of the constant's value.

## Progress

- [ ] EP-M1: sweep tooling added, fresh sweep run, value chosen, artefact
  committed.
- [ ] EP-M2: verification scaffold landed and green at the current 4 KiB value,
  with negative-control evidence recorded.
- [ ] EP-M3: `_READ_SIZE` raised, full suite green, acceptance benchmark re-run
  and gate met.
- [ ] EP-M4: documentation and roadmap updated, all gates green.

## Surprises & discoveries

- Observation: `_READ_SIZE` is not defined where the roadmap says it is.
  Evidence: `docs/roadmap.md:236` cites `cuprum/_streams.py:14`, but that line
  is the close of the module docstring. The constant is defined at
  `cuprum/_streams_pump.py:23` and re-imported into `cuprum/_streams.py:24`.
  `docs/adr-001-rust-extension.md:14` carries the same stale citation;
  `docs/cuprum-design.md:1960` is correct.
  Impact: the edit site is `cuprum/_streams_pump.py`. Correct both stale
  citations as part of this work.

- Observation: the import creates two independent module-level bindings, not one
  shared cell.
  Evidence: `from cuprum._streams_pump import _READ_SIZE` binds the integer into
  `cuprum._streams`'s own namespace. `_drain` (`cuprum/_streams.py:78`) reads
  the `_streams` binding; `_relay_chunks` (`cuprum/_streams_pump.py:84`) and
  `_drain_stream_reader` (`:102`) read the `_streams_pump` binding.
  Impact: editing the source constant updates both, but any runtime override
  must set both. The 2026-06-12 sweep set only the `_streams` binding
  (`docs/tee-hotpath-profiling-baseline-2026-06-12.md:82`), so its multi-stage
  Python row never varied the pump read size.

- Observation: the roadmap's 20% gate is not met by the numbers it cites.
  Evidence: Table 3's tee row gives 13.50 s at 4 KiB and 10.98 s at 64 KiB, an
  18.7% improvement. Table 2's `tee-devnull-nocb-s1` scenario figure is 14.45 s,
  against which 10.98 s is a 24.0% improvement.
  Impact: the gate's baseline must be stated unambiguously before measurement
  begins. See decision D1.

- Observation: the boundary property test is bounded by a kernel argv limit, not
  by test design.
  Evidence: `tests/helpers/parity.py` base64-encodes the payload into
  `sys.argv[1]` of a generated writer script. Measured on the target machine, a
  98304-byte payload (131072 base64 bytes) raises
  `OSError: [Errno 7] Argument list too long`, while 66048 bytes succeeds.
  Impact: `_READ_SIZE + _BOUNDARY_DELTA` must stay at or below roughly 98300
  bytes. This caps the boundary-test approach at 64 KiB and rules out 128 KiB
  without restructuring the test to feed payloads over stdin.

- Observation: 64 KiB is a convergence point across three independent ceilings.
  Evidence: measured on the target machine, the default pipe capacity is 65536
  bytes; `asyncio.streams._DEFAULT_LIMIT` is 65536, and a `StreamReader` pauses
  its transport once buffered bytes exceed twice that limit; and the Rust
  extension already defaults `buffer_size` to 65536
  (`rust/cuprum-rust/src/lib.rs:80,112`).
  Impact: reads larger than 64 KiB cannot draw more per call from a
  default-capacity pipe, which explains the measured plateau and gives a
  principled upper bound. Record this reasoning in the design document.

## Decision log

- Decision D1: the 20% acceptance gate is measured against a fresh 4 KiB control
  taken in the same sweep session, using medians of at least five repeats on
  both sides.
  Rationale: the roadmap cites Table 3, whose tee row yields only 18.7%, so the
  literal reading cannot be satisfied by the very measurement it points at.
  Comparing a fresh median against a single historical measurement taken at a
  different commit under different load is not a sound comparison in either
  direction. Measuring control and candidate together on the same machine, in
  the same session, with the same repeat count is the only reading that makes
  the gate a statement about the change rather than about the environment. The
  historical figures are still recorded in the artefact for continuity. If the
  fresh delta falls short of 20%, that is a genuine finding and triggers the
  acceptance-gate tolerance rather than a change of baseline.
  Date/Author: 2026-08-29, planning agent. Requires reviewer confirmation at
  approval time.

- Decision D2: the sweep is driven by a committed `--read-size` option on the
  profiling worker plus a small sweep driver, rather than the throwaway inline
  `python -c` snippet recorded in the baseline document.
  Rationale: the inline snippet is unreproducible except by copying it out of
  prose, and its single-binding override is precisely the trap that invalidated
  the multi-stage row of the original sweep. A committed option is testable in
  the same way every other module under `benchmarks/` is tested, sets both
  bindings through one helper, and makes the sweep repeatable by anyone. The
  added surface is a benchmark-only command-line flag with no production effect.
  Date/Author: 2026-08-29, planning agent.

- Decision D3: no new ADR is written; the rationale is recorded in
  `docs/cuprum-design.md` §13.1 and §13.5.
  Rationale: `docs/adr-002-additional-rust-components.md` Proposal 3 already
  covers buffer tuning and states that profile data should justify a change.
  This work supplies that data and acts within the existing decision rather than
  taking a new architectural one. A constant change with no interface or
  dependency impact does not clear the bar in
  `docs/documentation-style-guide.md` for a separate ADR. The one durable point
  worth recording, that the Python read size now deliberately matches the Rust
  extension's 64 KiB default and the platform pipe capacity, belongs in the
  design document's performance section.
  Date/Author: 2026-08-29, planning agent.

- Decision D4: the sweep artefact is committed as a dated companion document
  plus its raw JSON, cross-linked from the baseline document, rather than
  appended to the baseline document.
  Rationale: the roadmap requires the value and artefact be "recorded alongside
  the baseline document". That document's title pins it to a specific
  2026-06-12 run, so appending a later run's results would misrepresent it. A
  dated companion preserves the one-document-per-measurement-session convention
  the baseline itself establishes. Committing the raw JSON is a departure from
  current practice, where all benchmark output lives under gitignored `dist/`,
  and is justified here because the roadmap makes the artefact part of the
  acceptance criterion rather than transient CI output.
  Date/Author: 2026-08-29, planning agent.

- Decision D5: Red-Green-Refactor is not available in its behavioural form for
  the constant change itself, and the benchmark serves as the observable
  substitute.
  Rationale: the change is behaviour-preserving by construction, so no test can
  legitimately fail before it and pass after it on behavioural grounds; a test
  that did would be asserting that output depends on the read size, which is the
  opposite of the invariant being protected. The `execplans` skill permits a
  documented substitute in this situation. The substitute here is a measurable
  red-to-green transition in wall time, plus a seeded-mutation negative control
  proving the new invariance test is not vacuous. Two pinning tests (the value
  and its range, and the agreement of the two bindings) do transition red to
  green, and are recorded honestly as pinning rather than as behavioural
  evidence.
  Date/Author: 2026-08-29, planning agent.

## Outcomes & retrospective

To be completed at EP-M4. Record the chosen value, the measured curve, the
acceptance margin against the fresh control, any scenario that regressed, and
whether the two-binding hazard warranted a structural fix beyond documentation.

## Context and orientation

Cuprum runs external programs and pipelines. When a command's output is
captured, echoed to a sink, or delivered to line callbacks, the parent Python
process reads that output from the child's pipe in a loop. That loop is the
"parent-side consume path" this plan targets.

The relevant modules, by full path:

- `cuprum/_streams_pump.py` defines `_READ_SIZE = 4096` at line 23. It is the
  single source of truth. The module owns the inter-stage pump: `_relay_chunks`
  (line 84) copies one stage's stdout into the next stage's stdin with
  backpressure, and `_drain_stream_reader` (line 102) discards output to end of
  file when there is no downstream writer. Both call
  `reader.read(_READ_SIZE)`.
- `cuprum/_streams.py` consumes a finished stage's output. It imports
  `_READ_SIZE` from `_streams_pump` (line 24) and re-exports it (line 247).
  `_drain` (line 60) is the canonical read/echo/capture loop shared by
  `_consume_stream_without_lines` and `_consume_stream_with_lines`; it calls
  `stream.read(_READ_SIZE)` at line 78. Because the import binds the integer
  into this module's own namespace, `cuprum._streams._READ_SIZE` and
  `cuprum._streams_pump._READ_SIZE` are two distinct names. Editing the
  definition updates both; overriding one at runtime does not.
- `cuprum/_testing.py` re-exports `_READ_SIZE` (lines 36 and 96) as part of the
  private surface tests import from.
- `cuprum/_streams_rs.py` wraps the optional Rust extension. Its
  `rust_pump_stream` and `rust_consume_stream` wrappers default `buffer_size` to
  65536 (lines 88 and 128), mirroring `rust/cuprum-rust/src/lib.rs:80,112`.
  These are separate knobs from `_READ_SIZE` and are not touched here.

The tests that depend on the constant:

- `cuprum/unittests/test_stream_property_based.py` imports `_READ_SIZE` (line
  15) and derives a boundary window from it:
  `_BOUNDARY_DELTA = 512` (line 28), `_BOUNDARY_MIN_SIZE` (line 29), and
  `_BOUNDARY_MAX_SIZE` (line 30). The boundary test at line 120 draws payloads
  in that window, splits them at up to sixteen random cut points, and runs them
  through a real two-stage pipeline. Because the window is derived rather than
  hard-coded, it tracks the constant automatically. What it cannot do
  automatically is stay within the kernel's argv limit, or preserve small-scale
  multi-chunk coverage once the boundary moves to 64 KiB.
- `cuprum/unittests/test_stream_pump_runtime_behaviour.py` imports `_READ_SIZE`
  (line 17) and builds a payload of `_READ_SIZE * 2 + 1` bytes (lines 281, 289)
  fed to an in-process `asyncio.StreamReader`, plus a stub reader yielding
  `_READ_SIZE`-sized chunks (line 308). These scale automatically and use no
  real pipes, so they are safe at the larger size.
- `tests/helpers/parity.py` builds the property pipeline. `chunked_writer_script`
  generates a Python program that reads a base64 payload from `sys.argv[1]` and
  chunk sizes from `sys.argv[2]`, then writes the payload in those chunks. This
  is the argv-limited path described in `Risks`. The docstring of
  `utf8_stress_payload` (line 105) states "4096-byte reads" and will need
  updating.
- `tests/features/` holds Gherkin feature files paired with pytest-bdd step
  modules in `tests/behaviour/`. `tests/features/stream_parity.feature` and
  `tests/behaviour/test_stream_parity_behaviour.py` are the closest existing
  analogue for the new scenario.

The benchmark harness, under `benchmarks/`:

- `benchmarks/tee_profile_worker.py` is the subprocess worker that runs one
  scenario and writes `worker-result.json`. Its argument parser is `_parse_args`
  (line 471); its result type is `TeeProfileWorkerResult` (line 67), whose
  `wall_time_seconds` field is the measurement of interest. It has no read-size
  option today.
- `benchmarks/tee_profile_scenarios.py` defines `TeeProfileScenario` and the
  default matrix, including `tee-devnull-nocb-s1`,
  `echo-devnull-nocb-s4-python`, and `echo-devnull-nocb-s4-rust`.
- `benchmarks/profile_tee_hotpath.py` is the driver entry point, delegating
  argument parsing to `benchmarks/tee_profile_driver.py:_base_parser` (line
  156). Artefacts are written under `dist/profiles/<scenario>/`, which is
  gitignored.
- `benchmarks/deterministic_b64_fixture.py` generates the deterministic base64
  fixtures the scenarios replay.

Terms used in this plan:

- *Read size*: the maximum number of bytes requested per `read()` call on an
  asyncio stream. A read returns at most this many bytes and at least one, or
  zero at end of file.
- *Plateau*: the point on the read-size-versus-wall-time curve beyond which
  further increases stop producing a material improvement.
- *Parity*: byte-identical results between the pure-Python and Rust stream
  backends for the same input.
- *Negative control*: a deliberate, temporary fault seeded into the
  implementation to confirm that a test rejects it, proving the test is capable
  of failing.

## Conformance basis

Upstream artefacts governing this work:

- `docs/roadmap.md` item 5.1.1, within step 5.1 and phase 5, at revision
  `ba32d3f5`. This is the requirement of record.
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §1 (hypothesis 1, Table 3)
  supplies the prior measurement and the 16-64 KiB range.
- `docs/adr-002-additional-rust-components.md` Proposal 3 (lines 176-191)
  sanctions buffer tuning and requires profile data to justify a change. Its
  "Acceptance thresholds" section (lines 278-289) supplies the 20% median
  wall-time bar and the 5% small-scenario regression bar. Note that this ADR's
  status is "Proposed", not "Accepted"; the roadmap nonetheless treats its
  thresholds as binding, and this plan follows the roadmap.
- `docs/cuprum-design.md` §13.1 and §13.5 are the technical-design sections of
  record for stream consumption and performance characteristics.
- `docs/adr-008-rust-pump-observation-channel.md` is in scope only as a
  constraint: it explicitly excludes buffer and throughput counters from the
  public runtime API, so this plan adds none.

There is no separate Terms of Reference document in this repository.

Trace links:

```plaintext
roadmap-5.1.1 -> ADR002-Proposal3 -> design-13.1 -> EP-M1 -> docs/read-size-sweep-<date>.md
roadmap-5.1.1 -> ADR002-thresholds -> EP-M3 -> acceptance benchmark transcript
roadmap-5.1.1-subtask-boundary-test -> EP-M2 -> tests: read-size invariance and boundary
design-13.5 -> EP-M4 -> docs/users-guide.md memory trade-off note
```

## Verification plan

The change is a constant. It introduces no new logic, so it introduces no new
invariant. What it does is place existing invariants under a parameter value
they have never been exercised at, and the verification burden is to show those
invariants are genuinely independent of that parameter rather than
coincidentally true at 4096.

Non-trivial axioms this reasoning depends on:

- A1: `asyncio.StreamReader.read(n)` returns between 1 and `n` bytes, or empty
  bytes at end of file, and never reorders or drops data. This is a documented
  third-party contract and is not verified here.
- A2: A `StreamReader` created for a subprocess pipe uses
  `asyncio.streams._DEFAULT_LIMIT` (65536 bytes) as its high-water mark and
  pauses its transport above twice that. Measured on the target machine;
  relevant only to the plateau explanation, not to correctness.
- A3: Linux caps a single `argv` entry at `MAX_ARG_STRLEN` (131072 bytes).
  Measured empirically on the target machine; a 98304-byte payload base64-encodes
  to exactly 131072 bytes and raises `E2BIG`.
- A4: Default pipe capacity is 65536 bytes and can shrink under per-user page
  pressure. Measured; drives the preference for in-process readers in new tests.

Obligations:

- Obligation V1: read-size independence of captured content. For every payload
  and every upstream chunking, `_consume_stream` returns identical captured text
  regardless of `_READ_SIZE`.
  Method: Hypothesis property test comparing outputs across several read sizes
  within one example.
  Rationale: this is the invariant the entire change rests on, it ranges over an
  open input domain, and comparing sizes within a single example turns the
  property into a differential test that cannot pass by accident at one size.
  Domain: payloads from 0 to roughly 200 KiB including multi-byte UTF-8 and
  payloads with and without a trailing newline; read sizes drawn from a fixed
  set spanning below, at, and above the boundary, at minimum 1, 3, 4096, the
  chosen value, and the chosen value plus one.
  Artefact: a new test in `cuprum/unittests/test_stream_property_based.py`,
  driving an in-process `asyncio.StreamReader` so neither the argv limit (A3)
  nor pipe capacity (A4) applies.
  Evidence: `uv run pytest cuprum/unittests/test_stream_property_based.py -k
  read_size_invariance` passes at the current 4 KiB constant and again after the
  bump.
  Non-vacuity: a read size of 1 forces the maximum number of boundary crossings
  and is a witness that the multi-read path is genuinely exercised; assert
  within the test that at least one configured size is strictly smaller than the
  payload, so a degenerate all-single-read example cannot silently satisfy the
  property. The negative control is a seeded fault in `_drain` that drops the
  final chunk when it is shorter than the requested size, a mutation invisible
  at read sizes that happen to divide the payload evenly. The test must fail on
  that mutation and pass once reverted.

- Obligation V2: read-size independence of line emission. The sequence of lines
  delivered to a line callback is identical regardless of read size, including
  when a newline falls exactly on a read boundary.
  Method: Hypothesis property test over payloads with generated newline
  positions, compared across read sizes.
  Rationale: incremental decoding and line splitting are the parts of the drain
  loop most sensitive to where a chunk ends; moving the boundary by a factor of
  sixteen changes which byte positions are ever tested.
  Domain: payloads assembled from generated line lengths, with and without a
  trailing newline, including lines longer than the smallest configured read
  size and multi-byte characters straddling boundaries.
  Artefact: the same new test module section as V1.
  Evidence: the property passes at both constants.
  Non-vacuity: classify examples so that the run is rejected unless at least one
  example places a newline within one byte of a read boundary and at least one
  example splits a multi-byte character across a boundary. The negative control
  seeds a fault that flushes the incremental decoder per chunk instead of
  carrying its tail, which corrupts any character split across a boundary; the
  test must reject it.

- Obligation V3: byte-exactness through a real two-stage pipeline at the new
  boundary. Payloads sized around the new `_READ_SIZE` survive a real subprocess
  pipeline unchanged.
  Method: the existing Hypothesis boundary property test, retargeted.
  Rationale: V1 and V2 use in-process readers and therefore do not exercise real
  pipes, subprocess buffering, or the backend dispatcher. This obligation keeps
  end-to-end coverage at the new boundary.
  Domain: payload sizes in `[_READ_SIZE - 512, _READ_SIZE + 512]`, run against
  both the Python and Rust backends, subject to the A3 ceiling.
  Artefact: `test_stream_preserves_random_payloads_around_python_read_size_boundary`
  in `cuprum/unittests/test_stream_property_based.py`, plus a new module-level
  assertion that `_BOUNDARY_MAX_SIZE` stays within the argv budget.
  Evidence: the test passes for both backends after the bump; the new assertion
  fails at import time if a future read size exceeds the budget.
  Non-vacuity: the generated cut points guarantee multi-chunk writes upstream;
  assert the case's `chunk_count` exceeds one for at least one example. A
  smaller-scale companion case retains coverage at a payload size near 4 KiB, so
  that moving the boundary to 64 KiB does not vacate the small-payload
  multi-chunk region that the general test used to cover.

- Obligation V4: the two `_READ_SIZE` bindings agree.
  Method: parameterized unit test.
  Rationale: a finite, two-element partition; a property test would add nothing.
  This is the guard against the hazard that invalidated the original sweep.
  Domain: `cuprum._streams._READ_SIZE`, `cuprum._streams_pump._READ_SIZE`, and
  `cuprum._testing._READ_SIZE`.
  Artefact: a unit test in `cuprum/unittests/test_stream_property_based.py` or
  the nearest existing stream unit module.
  Evidence: passes after the bump; would fail if a future edit changed one
  module's value without the other.
  Non-vacuity: the test compares values fetched through each module's own
  namespace at call time, so it can fail; a mutation that reassigns only
  `cuprum._streams._READ_SIZE` must be rejected.

- Obligation V5: the chosen value satisfies the roadmap's stated range.
  Method: parameterized unit test pinning the value and its bounds.
  Rationale: pins a decision that a later change might casually undo. Recorded
  as a pinning test, not behavioural evidence (see decision D5).
  Domain: the single constant.
  Artefact: the same unit test module.
  Evidence: red before the bump, green after.
  Non-vacuity: trivially non-vacuous, since it asserts a concrete value.

- Obligation V6: behavioural coverage of the user-visible outcome.
  Method: pytest-bdd scenario.
  Rationale: `AGENTS.md` requires behavioural tests for user-observable
  behaviour; the observable behaviour here is that output is preserved through a
  pipeline whose payload spans many read boundaries.
  Domain: a payload substantially larger than the chosen read size, run through
  a real pipeline on each backend.
  Artefact: a scenario in `tests/features/stream_read_size.feature` with steps
  in `tests/behaviour/test_stream_read_size_behaviour.py`.
  Evidence: `uv run pytest tests/behaviour/test_stream_read_size_behaviour.py`
  passes on both backends.
  Non-vacuity: the step asserting payload size must require it to exceed the
  read size by at least a factor of four, so the scenario cannot degenerate into
  a single-read case.

- Obligation V7: the performance claim itself.
  Method: measured benchmark, medians of at least five repeats per point.
  Rationale: a performance claim is only discharged by measurement. No test or
  proof substitutes.
  Domain: the sweep points, on the `tee-devnull-nocb-s1` workload as the gated
  scenario, with `echo-devnull-nocb-s1`, `echo-devnull-nocb-s4-python`, and
  `echo-devnull-nocb-s4-rust` measured for regression.
  Artefact: `docs/read-size-sweep-<date>.json` and its companion Markdown.
  Evidence: median at the chosen value at least 20% below the same-session 4 KiB
  control, with no measured scenario more than 5% worse.
  Non-vacuity: the 4 KiB control is measured in the same session with the same
  code path and repeat count, so an environment-wide slowdown moves both sides
  and cannot manufacture a passing ratio. Record every raw sample, not just the
  median, so dispersion is inspectable.

No formal proof or bounded model check is proposed. There is no introduced
lemma or contractual business logic here: the change sets an integer, and the
obligations above are differential and statistical rather than deductive. A
Verus or Kani obligation would either restate "the loop copies its input", which
the property tests establish over a far wider domain than a bounded checker
could reach through real asyncio streams, or would require modelling the asyncio
runtime, which axiom A1 explicitly places outside the verification boundary.
Recording that judgement is itself part of discharging the plan.

## Plan of work

Stage A: understand and confirm, no production changes.

Confirm the current constant is still 4096 at head, confirm the target machine
is quiescent, and record the environment (commit, kernel, Python, pipe capacity,
whether the Rust extension is built in release mode). Regenerate the
deterministic fixtures if `dist/fixtures/` is absent. Go/no-go: proceed only
once no other gate run or build is active and the fixtures' manifest hashes
match the baseline document.

Stage B: sweep tooling.

Add a `--read-size` option to `benchmarks/tee_profile_worker.py` that applies
the value through one helper setting both `cuprum._streams._READ_SIZE` and
`cuprum._streams_pump._READ_SIZE`, then asserts they agree. Record the effective
read size in `TeeProfileWorkerResult` so every artefact is self-describing. Add
`benchmarks/read_size_sweep.py` to drive one scenario across a list of read
sizes with a configurable repeat count, emitting JSON containing every raw
sample plus per-point medians. Unit-test the new option and driver in the style
of the existing `benchmarks/` test modules, including a snapshot of the emitted
JSON shape. Go/no-go: new tests pass and the worker still produces an unchanged
result when `--read-size` is omitted.

Stage C: measurement.

Run the sweep over 4096, 8192, 16384, 32768, and 65536 bytes for
`tee-devnull-nocb-s1`, with at least five measured repeats per point. Repeat for
`echo-devnull-nocb-s1` and the two multi-stage scenarios to check for pump-path
regression. Identify the plateau and choose the smallest in-range value that
reaches it, preferring the smaller value where the difference is within noise,
because transient memory scales with the constant. Go/no-go: the chosen value is
within 16-64 KiB and its median is at least 20% below the same-session 4 KiB
control. If not, stop and escalate under the acceptance-gate tolerance.

Stage D: verification scaffold, before the constant changes.

Add the read-size invariance and line-emission property tests (V1, V2), the
binding-agreement test (V4), and the behavioural scenario (V6), all written so
they pass at the current 4096. Run each negative control described in the
`Verification plan`, capture the failure transcript, and revert the seeded
fault. Go/no-go: every new test passes at 4096 and every negative control was
observed to fail for the stated reason.

Stage E: the change.

Raise `_READ_SIZE` in `cuprum/_streams_pump.py` to the chosen value. Add the
pinning test (V5) and the argv-budget assertion (V3). Update
`_BOUNDARY_DELTA` handling only if the sweep chose a value whose window would
exceed the argv budget. Update the stale docstring in
`tests/helpers/parity.py:105`. Run the full suite on both backends. Re-run the
gated benchmark scenario to confirm the acceptance margin holds with the
constant compiled in rather than injected. Go/no-go: full suite green and the
gate met.

Stage F: documentation and roadmap.

Write `docs/read-size-sweep-<date>.md` and commit its JSON. Cross-link it from
`docs/tee-hotpath-profiling-baseline-2026-06-12.md`. Update
`docs/cuprum-design.md` §13.1 (the constant's value) and §13.5 (the plateau
rationale and the 64 KiB convergence), `docs/developers-guide.md` ("Canonical
stream-drain loop": the new value, the two-binding hazard, and the override
helper), and `docs/users-guide.md` ("Performance extensions": the per-stream
transient memory trade-off). Correct the stale citations at `docs/roadmap.md:236`
and `docs/adr-001-rust-extension.md:14`. Mark roadmap item 5.1.1 done. Go/no-go:
all gates green.

## Milestones and plateaus

- Identifier and outcome: EP-M1. The repository gains a reproducible read-size
  sweep capability and a committed measurement, with production behaviour
  unchanged. The constant is still 4096.
  Requirements and gaps: advances roadmap-5.1.1's "fresh read-size sweep"
  requirement; discharges V7's measurement.
  Acceptance evidence: `docs/read-size-sweep-<date>.md` and its JSON exist,
  `uv run pytest benchmarks/` passes, and the sweep JSON records the chosen
  value with its full sample set.
  Conformance check: no public interface changed; the only new surface is a
  benchmark command-line option; ADR-002 Proposal 3's requirement for profile
  data is now satisfied; trace links to the artefact are current.
  Recovery: the sweep is read-only with respect to production code and can be
  re-run at will; delete `dist/profiles/` and repeat.
  Remaining gaps: the constant is unchanged, so no user sees a speedup yet.
  Compatibility decision: none required.

- Identifier and outcome: EP-M2. The invariants that the change relies on are
  under test and demonstrated capable of failing, at the old constant.
  Requirements and gaps: discharges V1, V2, V4, and V6 at 4096.
  Acceptance evidence: the new tests pass; the negative-control transcripts in
  `Artefacts and notes` show each seeded fault rejected for its intended reason.
  Conformance check: tests only; no production change; no new dependency.
  Recovery: tests are additive and can be reverted independently.
  Remaining gaps: the constant is unchanged.
  Compatibility decision: none required.

- Identifier and outcome: EP-M3. `_READ_SIZE` is raised and the whole suite is
  green on both backends, with the acceptance margin re-confirmed.
  Requirements and gaps: discharges roadmap-5.1.1's primary requirement and V3,
  V5, and V7's gate.
  Acceptance evidence: `make test` passes; the re-run benchmark transcript shows
  the median at or below 80% of the same-session 4 KiB control.
  Conformance check: public API unchanged; the value is within the roadmap's
  range; no captured-output memory increase beyond the payload; no scenario
  regressed by more than 5%; ADR-002's acceptance thresholds met.
  Recovery: revert the one-line constant change; every test remains valid at
  4096 by construction, which is itself evidence the invariance tests are
  parameter-independent.
  Remaining gaps: documentation not yet updated.
  Compatibility decision: none required. `_READ_SIZE` is private, has no
  external consumer, and the project is pre-1.0, so no compatibility shim is
  warranted and none is prescribed.

- Identifier and outcome: EP-M4. Documentation and roadmap reflect the shipped
  state, and all gates pass.
  Requirements and gaps: discharges the roadmap's recording requirement and the
  documentation obligations in `AGENTS.md`.
  Acceptance evidence: `make check-fmt lint typecheck test markdownlint nixie`
  all pass; roadmap item 5.1.1 shows `- [x]`.
  Conformance check: design document, developers' guide, and users' guide agree
  with the code; stale citations corrected; no upstream assumption left
  falsified without record.
  Recovery: documentation-only; revert individually.
  Remaining gaps: none for 5.1.1. Step 5.2 remains open.
  Compatibility decision: none required.

## Concrete steps

Run everything from the repository root. Log every gate through `tee` so long
output survives truncation.

Stage A (environment capture):

```plaintext
git branch --show-current
sed -n '23p' cuprum/_streams_pump.py
uv run python -c "import fcntl,os;r,w=os.pipe();print('pipe capacity',fcntl.fcntl(w,1032))"
pgrep -af 'make (lint|test)|pytest' || echo "no gate running"
ls dist/fixtures/ 2>/dev/null || echo "fixtures absent"
```

Expected: the branch is `5-1-1-raise-read-size-to-profiled-plateau`, line 23
reads `_READ_SIZE = 4096`, pipe capacity is 65536, and no gate is running. If
fixtures are absent, regenerate them with the two
`benchmarks/deterministic_b64_fixture.py` invocations recorded in
`docs/tee-hotpath-profiling-baseline-2026-06-12.md` and confirm the manifest
hashes match.

Stage B (sweep tooling, tests first):

```plaintext
set -o pipefail; uv run pytest benchmarks/ -k read_size 2>&1 | tee /tmp/5-1-1-sweep-red.log
```

Expected: the new tests fail because `--read-size` does not yet exist. Implement,
then:

```plaintext
set -o pipefail; uv run pytest benchmarks/ 2>&1 | tee /tmp/5-1-1-sweep-green.log
```

Expected: all benchmark tests pass, including the unchanged-behaviour case where
`--read-size` is omitted.

Stage C (measurement):

```plaintext
export RUSTFLAGS="-C force-frame-pointers=yes"
uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml
set -o pipefail; uv run python -m benchmarks.read_size_sweep \
  --scenario tee-devnull-nocb-s1 \
  --read-sizes 4096,8192,16384,32768,65536 \
  --repeats 5 \
  --output dist/benchmarks/read-size-sweep.json 2>&1 | tee /tmp/5-1-1-sweep.log
```

Expected: one median per read size, monotonically improving then flattening. Then
repeat with `--scenario echo-devnull-nocb-s1`,
`--scenario echo-devnull-nocb-s4-python`, and
`--scenario echo-devnull-nocb-s4-rust` to check for regression. Compute the
acceptance ratio as the chosen value's median divided by the 4096 median from
the same file; it must be at or below 0.80.

Stage D (verification scaffold at the old constant):

```plaintext
set -o pipefail; uv run pytest cuprum/unittests/test_stream_property_based.py \
  tests/behaviour/test_stream_read_size_behaviour.py 2>&1 | tee /tmp/5-1-1-scaffold.log
```

Expected: all pass at `_READ_SIZE = 4096`. Then seed each negative control from
the `Verification plan`, re-run the same command, capture the failure, and
revert the fault before proceeding.

Stage E (the change):

```plaintext
set -o pipefail; make test 2>&1 | tee /tmp/5-1-1-test.log
```

Expected: the full suite passes. Note that on this machine two unrelated local
failures are known and are not caused by this branch: Rust trybuild snapshot
drift against the pinned 1.92.0 toolchain, and
`test_rust_pump_stream_propagates_io_errors` reporting `errno=None`. Confirm
they reproduce on the base branch before dismissing them.

Stage F (final gates):

```plaintext
set -o pipefail; make check-fmt 2>&1 | tee /tmp/5-1-1-check-fmt.log
set -o pipefail; make lint 2>&1 | tee /tmp/5-1-1-lint.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/5-1-1-typecheck.log
set -o pipefail; make test 2>&1 | tee /tmp/5-1-1-test-final.log
set -o pipefail; make markdownlint 2>&1 | tee /tmp/5-1-1-markdownlint.log
set -o pipefail; make nixie 2>&1 | tee /tmp/5-1-1-nixie.log
```

Expected: every gate passes. Run these sequentially, never in parallel; the
coverage database is shared and concurrent runs corrupt it.

## Validation and acceptance

A user can observe success by running a command that produces a large captured
output and seeing it complete measurably faster, with identical bytes. Concretely:

1. Run the gated benchmark scenario before and after the change and compare
   medians. Expect the post-change median to be at or below 80% of the
   pre-change median measured in the same session.
2. Run `make test` and expect no new failures on either stream backend.
3. Run the read-size invariance property test and expect it to pass both at
   4096 and at the chosen value, which is the observable form of the claim that
   the change is behaviour-preserving.

Red-Green-Refactor evidence, per decision D5:

- Red is not available in behavioural form for the constant change, because the
  change preserves behaviour by construction. The substitute is the measured
  wall-time transition in Stage C and the seeded-mutation negative controls in
  Stage D, each of which must be observed to fail before being reverted.
- Red is available for the two pinning obligations: the value-and-range test
  (V5) and, for the sweep tooling, the `--read-size` tests in Stage B. Both must
  be seen to fail before implementation.
- Green: `make test` passes after the constant is raised.
- Refactor: gates in Stage F pass after cleanup and documentation.

Quality criteria:

- Tests: `make test` passes; the new invariance, line-emission,
  binding-agreement, boundary, pinning, and behavioural tests all pass on both
  the Python and Rust backends.
- Verification: obligations V1 through V7 discharged as specified, with
  negative-control transcripts recorded in `Artefacts and notes`.
- Lint/typecheck: `make check-fmt`, `make lint`, and `make typecheck` pass.
- Documentation: `make markdownlint` and `make nixie` pass.
- Performance: the gated scenario's median is at least 20% below the
  same-session 4 KiB control, and no measured scenario regresses by more than
  5%.
- Memory: peak resident memory for the concurrent scenarios rises by no more
  than 5%.

Quality method: run the gates sequentially through the `scrutineer` subagent,
which captures each gate's output to a log under `/tmp` and returns a bounded
report, then read the cited log for any failure rather than re-running the gate.

## Idempotence and recovery

Every step is safe to repeat. The sweep writes only under `dist/`, which is
gitignored, and reads production code without mutating it. The constant change
is a single line; reverting it restores the previous behaviour exactly, and
because the new tests are written to hold at any read size, they remain valid
after a revert. The documentation changes are independent of the code change and
can be reverted individually.

If the sweep is interrupted, delete the partial JSON and re-run; the driver
writes its output only on completion. If a gate run collides with another
agent's build and corrupts the coverage database, delete the stale `.coverage*`
files and re-run the gate sequentially in the foreground.

The one irreversible-feeling step is committing a benchmark artefact under
`docs/`, which sets a precedent. That is a deliberate choice recorded as
decision D4 and can be undone by moving the JSON back under `dist/` and keeping
only the narrative table, at the cost of the acceptance criterion's "artefact"
wording.

## Artefacts and notes

Expected log paths: `/tmp/5-1-1-*.log` for each gate and stage, following the
`$ACTION-$(get-project)-$(git branch --show-current)` convention where the
shorter form above is ambiguous.

Committed artefacts: `docs/read-size-sweep-<date>.md` and
`docs/read-size-sweep-<date>.json`.

Measured environment facts, captured during planning on the target machine and
to be re-confirmed at Stage A:

```plaintext
python 3.14.4
io.DEFAULT_BUFFER_SIZE 131072
shutil.COPY_BUFSIZE 262144
asyncio.streams._DEFAULT_LIMIT 65536
pipe capacity (F_GETPIPE_SZ) 65536
/proc/sys/fs/pipe-max-size 1048576
```

The argv ceiling, measured by invoking `/bin/true` with a single oversized
argument:

```plaintext
payload=  66048  b64len=  88064  OK
payload=  98304  b64len= 131072  FAIL [Errno 7] Argument list too long
payload= 131072  b64len= 174764  FAIL [Errno 7] Argument list too long
```

Negative-control transcripts are to be pasted here at EP-M2, one per seeded
fault, each showing the failing assertion and the read size at which it fired.

The historical figures this plan is measured against, for continuity:
`docs/tee-hotpath-profiling-baseline-2026-06-12.md` Table 2 records
`tee-devnull-nocb-s1` at 14.45 s; Table 3 records tee at 13.50 s for 4 KiB and
10.98 s for 64 KiB, and echo at 5.91 s, 4.64 s, 4.64 s, and 4.65 s for 4, 16,
64, and 256 KiB respectively.

## Interfaces and dependencies

No new runtime dependency is introduced. No public interface changes.

In `cuprum/_streams_pump.py`, the constant changes value only:

```python
_READ_SIZE = 65536  # value to be confirmed by the Stage C sweep
```

In `benchmarks/tee_profile_worker.py`, add a benchmark-only option and the
helper that makes overriding safe:

```python
def apply_read_size_override(read_size: int) -> None:
    """Set the read size on both module bindings and verify they agree."""
```

The worker's `--read-size` option accepts a positive integer and defaults to the
compiled-in constant, leaving behaviour unchanged when omitted.
`TeeProfileWorkerResult` gains a `read_size` field so every artefact records the
value it was produced at.

In `benchmarks/read_size_sweep.py`, add a driver exposing:

```python
def run_sweep(
    *,
    scenario: str,
    read_sizes: tuple[int, ...],
    repeats: int,
) -> SweepReport:
    """Run one scenario at each read size and return medians with raw samples."""
```

New test artefacts:

- `cuprum/unittests/test_stream_property_based.py`: read-size invariance and
  line-emission properties, the binding-agreement test, the pinning test, and
  the argv-budget assertion.
- `tests/features/stream_read_size.feature` and
  `tests/behaviour/test_stream_read_size_behaviour.py`: the behavioural
  scenario.
- Benchmark tests for the new option and driver, alongside the existing
  `benchmarks/` test modules, including a syrupy snapshot of the sweep JSON
  shape.

## Signposted documentation and skills

Read before starting: `AGENTS.md` for the gate commands and language
conventions; `docs/documentation-style-guide.md` for Markdown, table caption,
and ADR rules; `docs/developers-guide.md` "Canonical stream-drain loop" for the
drain contract and "Profiling harness overview" for the benchmark harness;
`docs/cuprum-design.md` §13 for the stream architecture;
`docs/tee-hotpath-profiling-baseline-2026-06-12.md` for the prior measurement
and its reproduction commands; `docs/adr-002-additional-rust-components.md`
Proposal 3 and its acceptance thresholds; `.rules/python-00.md` and
`.rules/python-typing.md` for naming, typing, and docstring conventions.

Skills to load: `execplans` when revising this plan; `python-router` then
`hypothesis` for the property tests and `python-testing` for the behavioural
scenario; `rust-router` only if the Rust extension turns out to need attention,
which this plan does not anticipate; `en-gb-oxendict` for prose; `nextest` if
the Rust portion of `make test` needs investigation.

## Revision note

Initial draft, 2026-08-29. Records four findings that change the shape of the
task relative to the roadmap's wording: the constant lives in
`cuprum/_streams_pump.py`, not `cuprum/_streams.py`; it exists as two
independent bindings, one of which the original sweep never varied; the stated
20% gate is not met by the figures the roadmap cites, and so needs an
unambiguous baseline; and the boundary property test is bounded by a kernel
argv limit that caps the usable read size at 64 KiB. Decisions D1 through D5
resolve these; D1 and D4 in particular should be confirmed at approval time
before implementation begins.
