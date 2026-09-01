# Raise the pure-Python stream read size to the profiled plateau (5.1.1)

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`,
`Decision log`, `Outcomes & retrospective`, `Conformance basis`, and
`Verification plan` must be kept up to date as work proceeds.

Status: IN PROGRESS

Measurement update (2026-08-29): the 15-round interleaved sweep selected
65536 bytes. The tee median improvement was 22.9997% (95% paired-bootstrap
interval 22.5508% to 23.2037%). The completed regression sweeps found median
changes of -51.921% (devnull), -43.391% (text sink), -11.503% (PTY), and
-1.972% (line callbacks); all satisfy the 5% tolerance. The line-callback
campaign took about 4 hours 57 minutes for the 2.1 GiB wrapped fixture, rather
than the planning estimate; this is an observation, not a scope change.

Roadmap reference: `docs/roadmap.md` item `5.1.1` (phase 5, step 5.1).

`PLANS.md` is not present in this repository, so this plan follows the
`execplans` skill template and the repository instructions in `AGENTS.md`.

## Purpose / big picture

Cuprum reads subprocess output in 4 KiB slices. Raising that to 64 KiB removes
fifteen out of every sixteen iterations of the parent-side consume loop, and the
tee profiling baseline measured roughly a fifth of consume wall time coming
back. This plan banks that win.

After this change, a user running any Cuprum command or pipeline that captures
or echoes subprocess output gets faster execution for large outputs, with
byte-identical captured text. Nothing in the public API changes and no new
configuration is introduced.

Two things discovered during planning enlarge the picture, and both are
addressed here rather than deferred.

First, line emission is **not** currently independent of the read size. When a
read boundary falls between the carriage return and the line feed of a `\r\n`
pair, `_split_complete_lines` (`cuprum/_streams.py:205`) emits the carriage
return as a finished line and opens a spurious empty one. Captured text is
unaffected. Raising `_READ_SIZE` would make this defect sixteen times rarer
without fixing it, which is the worst possible outcome for anyone bisecting it
later. This plan fixes it first.

Second, the roadmap's stated motive for doing this before phase 6 is to stop the
Rust consume dispatcher being credited for a win a Python constant already
delivers. The scenario the roadmap gates on, `tee-devnull-nocb-s1`, is capture
**plus echo**, and a large part of its win is the echo write-syscall term, which
does not exist on the capture-only path phase 6 will actually dispatch. Gating
on tee alone would over-credit the Python baseline and defeat the stated
purpose. This plan therefore also measures a capture-only scenario.

This task is complete only when:

1. The split-boundary `\r\n` defect is fixed, with a behavioural test that fails
   before the fix and passes after it.
2. `_READ_SIZE` is raised from 4096 to a value in the 16-64 KiB range chosen
   from a fresh read-size sweep run on the target machine.
3. The `tee-devnull-nocb-s1` workload's median wall time at the chosen value is
   at least 20% below its median at 4096, both measured in the same interleaved
   sweep session, with the 95% bootstrap confidence interval clearing the gate
   (decisions D1 and D9).
4. No scenario in the sweep regresses by more than 5%, including the echo,
   pseudo-terminal (PTY), text-sink, and line-callback scenarios.
5. Read-size independence of captured text and emitted lines is verified against
   an external oracle, not merely by comparing read sizes with each other.
6. The chosen value and its sweep artefact are recorded alongside
   `docs/tee-hotpath-profiling-baseline-2026-06-12.md` and cross-linked from it.
7. `docs/cuprum-design.md`, `docs/developers-guide.md`, `CHANGELOG.md`, and
   `docs/contents.md` are updated, and `docs/roadmap.md` item 5.1.1 is marked
   done.
8. All repository gates pass.

## Constraints

- The public API must not change. `_READ_SIZE` is private, with no
  environment-variable, keyword-argument, or configuration surface, and this
  plan must not give it one. The `read_size` parameters introduced by decision
  D2 are on underscore-private functions, for injection by tests and benchmarks
  only.
- The chosen value must lie in the closed range 16384 to 65536 bytes, as
  required by roadmap item 5.1.1.
- Captured text must remain byte-identical for every payload, chunking, and
  encoding, at every read size.
- Emitted lines must become read-size independent. This is a change from current
  behaviour, and it is deliberate.
- The Rust extension must not be modified. `rust/cuprum-rust/src/lib.rs` already
  defaults `buffer_size` to 65536 and caps it at `MAX_BUFFER_SIZE`; those are
  independent of `_READ_SIZE`.
- Keep scope bounded to roadmap item 5.1.1 plus the line-splitting prerequisite.
  Do not implement step 5.2, and do not wire `rust_consume_stream` into
  production. The test asserting production does not reference it
  (`cuprum/unittests/test_rust_streams.py:360-389`) must keep passing.
- Markdown follows `docs/documentation-style-guide.md`: prose wrapped at 80
  columns, code blocks at 120, sentence-case headings, en-GB-oxendict spelling,
  and a caption below every table.
- Benchmark measurement must not run concurrently with repository gates or other
  agents' builds. Gates must be run sequentially; the coverage database is
  shared and concurrent runs corrupt it.

## Tolerances (exception triggers)

- Scope: if implementation requires changes to more than 18 files, stop and
  escalate.
- Acceptance gate: if the bootstrap confidence interval for the best in-range
  value does not clear 20% against the same-session 4096 control, stop and
  escalate. Do not widen the range, re-pick the baseline, or relax the
  threshold. Report the curve and await direction; the pre-registered options
  are in decision D1.
- Inconclusive measurement: if the effect size over pooled dispersion is below
  3, declare the sweep inconclusive and re-run rather than reporting a verdict.
- Range: if the measured plateau falls outside 16-64 KiB, stop and escalate.
- Interface: if any public API signature must change, stop and escalate.
- Dependencies: if a new external dependency is required, stop and escalate.
- Regression: if any existing test fails at the new read size and it is not a
  test that hard-codes the old value, stop and escalate. A behavioural
  difference across read sizes is a defect, not a test to adjust.
- Ratchet: if the Continuous Integration (CI) `benchmark-ratchet` job fails by
  more than the shift predicted in decision D7, stop and escalate rather than
  raising `--max-regression`.
- Iterations: if gates still fail after 3 fix attempts, stop and escalate.

## Risks

- Risk: the line-splitting prerequisite changes observable line output.
  Severity: medium. Likelihood: certain.
  Mitigation: a consumer relying on the spurious empty line would be relying on
  a bug that fires only at particular payload sizes. Record the change in
  `CHANGELOG.md`, cover it with a behavioural test, and state it in the design
  document. This is a pre-1.0 private path with no external consumer.

- Risk: the CI `benchmark-ratchet` job fires spuriously.
  Severity: high. Likelihood: high.
  Mitigation: `benchmarks/ratchet_ratio_extraction.py:113` computes
  `rust_mean / python_mean` and `.github/workflows/ci.yml:401` fails above
  `--max-regression 0.30`. Speeding the Python side up by 20% multiplies that
  ratio by about 1.25, presenting as a 25% "Rust regression" against a 30%
  threshold with no Rust change at all. See decision D7.

- Risk: the acceptance gate cannot be resolved by the measurement it cites.
  Severity: high. Likelihood: certain (arithmetic, not chance).
  Mitigation: Table 3 records tee at 13.50 s (4 KiB) and 10.98 s (64 KiB), an
  18.7% improvement, below the stated 20% gate. Decision D1 measures a fresh
  same-session control instead, and pre-registers what happens if the fresh
  delta also falls short.

- Risk: a median of five repeats cannot resolve the question being asked.
  Severity: high. Likelihood: high.
  Mitigation: the gate turns on roughly 1.7 percentage points. At a realistic
  per-run coefficient of variation the confidence interval straddles the
  threshold, so a five-repeat protocol would accept or reject on noise. Decision
  D9 raises the protocol to at least fifteen interleaved, randomized repeats
  with a bootstrap interval.

- Risk: larger echo writes lengthen event-loop stalls.
  Severity: medium. Likelihood: high.
  Mitigation: `_write_chunk` (`cuprum/_streams.py:143`) performs a synchronous
  blocking write and flush on the event loop. Measured against a slow sink,
  total throughput is unchanged but the worst single blocking write rises from
  about 10 ms to about 162 ms, freezing every other stream and timeout in the
  process for that period. Sweep the PTY, text-sink, and line-callback scenarios
  and hold them to the 5% bar.

- Risk: existing pump and parity tests silently lose coverage.
  Severity: medium. Likelihood: high.
  Mitigation: several tests write exactly 65536 bytes upstream
  (`cuprum/unittests/test_stream_parity.py:154-192`;
  `tests/behaviour/test_stream_parity_behaviour.py:175-201`) or 53248 bytes
  (`cuprum/unittests/test_stream_pump_runtime_behaviour.py:98-123`). At 4 KiB
  those span many relay iterations; at 64 KiB they can complete in one. They
  keep passing while testing less. Scale their payloads from the read size, or
  assert relay-iteration counts so thinning fails loudly.

- Risk: property tests over the line path are quadratic and will time out.
  Severity: medium. Likelihood: high if unaddressed.
  Mitigation: `_consume_stream_with_lines` accumulates with
  `pending_text + decoder.decode(chunk)` (`cuprum/_streams.py:126`), which is
  quadratic in payload length for long lines. Measured: a 200 KiB payload with
  no newlines costs 11.8 s at read size 1 against a global `timeout = 30`
  (`pyproject.toml:369`). Cap the small-read-size legs at 8 KiB payloads.

- Risk: the boundary property test's payload is bounded by a kernel limit.
  Severity: medium. Likelihood: certain if the value is raised further.
  Mitigation: `tests/helpers/parity.py` base64-encodes the payload into a single
  argv entry, and Linux caps one argument at `MAX_ARG_STRLEN` (131072 bytes).
  Measured: 66048 bytes encodes to 88064 and succeeds; 98304 bytes fails with
  `E2BIG`. So `_READ_SIZE` plus `_BOUNDARY_DELTA` must stay at or below about
  98300 bytes, which holds at 64 KiB with a third to spare and rules out
  128 KiB. Measured cost of the larger payload is negligible, 0.020 s to
  0.022 s.

- Risk: the branch is behind `origin/main`.
  Severity: low. Likelihood: certain.
  Mitigation: the branch is two commits behind and does not contain `ba32d3f5`,
  which changed timeout-path capture behaviour. Rebase before Stage A and
  re-verify the citations in `Context and orientation`.

## Progress

- [x] 2026-08-29 Stage A: rebased the five branch-exclusive ExecPlan commits
  from parent `41707268` onto `origin/main` (`ba32d3f5`) without conflicts.
  The existing PR will be retargeted to `main` after the force-with-lease push,
  so it no longer duplicates the stale #318 stack.
- [x] EP-M1: branch rebased; the line-splitting defect is fixed with a
  red-to-green regression test and BDD scenario. All deterministic gates passed
  and CodeRabbit reported zero findings.
- [x] EP-M2: read size is injectable; sweep support and the verification
  scaffold are green at 4096. Both seeded negative controls failed as intended,
  and all deterministic gates plus CodeRabbit completed with zero findings.
- [x] 2026-08-29 Stage E prerequisite: added
  `capture-devnull-nocb-s1` to the standard matrix, generated both 1.5 GiB
  fixtures, rebuilt the release extension, and verified the full suite after
  the new control. The gate ran 1,313 Python and 104 Rust tests; CodeRabbit
  reported zero findings.
- [x] 2026-08-29 EP-M3: fresh interleaved sweep run selected 65536 bytes and
  the dated companion document records every sample and interval.
- [x] 2026-08-29 EP-M4: `_READ_SIZE` was raised to 65536; the deterministic
  test suite and the same-session performance gate were re-confirmed.
- [x] 2026-08-29 EP-M5: documentation, changelog, roadmap, and companion
  sweep record completed. `make check-fmt`, `make lint`, `make typecheck`,
  `make test`, `make markdownlint`, and `make nixie` passed; CodeRabbit
  completed with zero findings.
- [x] 2026-09-02 V3/V4 correction: restored the boundary property to a real
  two-process pipeline on both backends, required each case to write more than
  one chunk, and made V4 assert the approved inclusive range with the sweep
  artefact in its failure message. The focused tests passed.
- [x] 2026-09-02 Gate preflight: repaired the isolated timeout-test fixture so
  its deliberately blocked inter-stage pump uses the Python backend. The test
  owns task reconciliation, whereas native raw-descriptor cleanup is covered
  separately; this prevents its final child-reaping step from hanging.
- [x] 2026-09-02 Gate preflight: disabled Hypothesis's per-example deadline
  for the profile scenario-matrix property, whose fixture writes files and
  imports benchmark configuration. Its initial 258 ms cold run exceeded the
  200 ms default but its rerun took 36 ms; the property now remains semantic.
- [ ] 2026-09-02 Re-run the deterministic gates, publish the correction, and
  obtain a new CodeRabbit result before returning this ExecPlan to `COMPLETE`.

## Surprises & discoveries

- Observation: the implementation PR has a stale stacked base.
  Evidence: PR #321 targets
  `issue-312-apply-repository-lint-standards-to-helper-scripts-scripts`; its
  four ExecPlan commits are above that branch's eleven commits, while
  `origin/main` contains neither group and is two commits ahead of their common
  ancestor. A plain `git rebase origin/main` would replay the parent PR into
  this PR.
  Impact: Stage A must rebase the four branch-exclusive commits with
  `git rebase --onto origin/main <parent-head>` and retarget #321 to `main`.
  This preserves the approved plan and removes the obsolete stack relationship.

- Observation: the CRLF defect has a small, deterministic reproducer using a
  real `asyncio.StreamReader`.
  Evidence: `test_crlf_split_at_read_boundary_emits_no_empty_line` constructs a
  first line whose carriage return is byte `_READ_SIZE`, then feeds the complete
  payload to the reader. Before the fix it failed with
  `[first_line, "", "b"]`; after the fix it passes with `[first_line, "b"]`.
  The BDD scenario observes the same two stdout lines from a subprocess.
  Impact: V0 has direct red-green evidence without relying on scheduler timing
  or a hand-written stream-reader double.

- Observation: the remote stack parent had been rewritten after this branch
  forked.
  Evidence: rebasing against the current remote parent would have selected its
  older common ancestor and replayed unrelated lint-rollout commits. The parent
  of the first plan commit, `41707268`, selected exactly the four original plan
  commits plus this progress-record commit; all five replayed cleanly.
  Impact: the completed Stage A rebase is a narrow, conflict-free replay onto
  `ba32d3f5`, not a merge of the current #318 branch.

- Observation: line emission is already read-size dependent, so the change is
  not behaviour-preserving as originally assumed.
  Evidence: driving `_consume_stream` over a fed `asyncio.StreamReader` with the
  payload `a`, carriage return, line feed, `b` yields lines `('a', '', 'b')` at
  read sizes 1 and 2, and `('a', 'b')` at 3 and above. Captured text is
  identical in every case. The cause is `_split_complete_lines`
  (`cuprum/_streams.py:205`) treating a chunk ending in a lone carriage return
  as complete, via `_ends_with_line_ending` (`cuprum/_streams.py:230`).
  Impact: falsifies the original decision D5. A genuine behavioural red test
  exists, and the defect must be fixed before the constant moves.

- Observation: `_READ_SIZE` never reaches a `read(2)` syscall, so the plateau
  has nothing to do with pipe capacity bounding a syscall.
  Evidence: `asyncio.unix_events._UnixReadPipeTransport.max_size` is 262144 and
  the transport calls `os.read(self._fileno, self.max_size)` independently of
  `_READ_SIZE`. `StreamReader.read(n)` only slices `n` bytes off an in-memory
  `bytearray`.
  Impact: the plateau is a call-count floor, not a syscall ceiling. Reader-side
  syscalls do not change at all. The design-document rationale must say this.

- Observation: the plateau is where one read drains a whole pipe-full.
  Evidence: measured over a 64 MiB subprocess stream, read call counts were
  16384, 4096, 1024, and 1024 at read sizes 4096, 16384, 65536, and 262144, with
  the largest chunk returned capped at 65536 in every case.
  Impact: 65536 is the smallest read size reaching the one-call-per-pipe-full
  floor; 16384 still performs four times the calls. This, not memory, is the
  correct tie-break.

- Observation: read size does not affect peak per-stream memory at all.
  Evidence: measured peak of reader buffer plus returned chunk was exactly 65536
  bytes at read sizes 4096, 16384, 65536, and 262144. At 4096 the reader holds a
  61440-byte residual buffer plus a 4096-byte chunk; at 65536 it holds nothing
  plus 65536.
  Impact: the original memory risk and its 5% tolerance were guarding a
  phenomenon that does not exist. Both are struck, along with the planned
  users'-guide note, which would have published a non-fact.

- Observation: deleting from the front of a `bytearray` is not a memmove.
  Evidence: removing 4096 bytes from a 16 MiB `bytearray` costs about 1940 ns; a
  real memmove of the residue would cost roughly a millisecond. CPython advances
  an internal start offset instead.
  Impact: rules out a quadratic buffer-shuffle explanation of the plateau,
  leaving per-read fixed overhead as the mechanism.

- Observation: the interpreter is not the one the baseline was measured on.
  Evidence: `.venv/bin/python` reports 3.13.13 with `io.DEFAULT_BUFFER_SIZE`
  8192 and `shutil.COPY_BUFSIZE` 65536. The baseline document records CPython
  3.14.4, where those are 131072 and 262144.
  Impact: a further independent reason the fresh same-session control of
  decision D1 is necessary. Stage A must record the interpreter explicitly.

- Observation: the roadmap's edit-site citation is wrong.
  Evidence: `docs/roadmap.md:236` and `docs/adr-001-rust-extension.md:14` cite
  `cuprum/_streams.py:14`, which is the close of a module docstring. The
  constant is at `cuprum/_streams_pump.py:23`.
  Impact: correct both citations as part of this work.

- Observation: the constant is mirrored four times, one of which no assignment
  can reach.
  Evidence: `cuprum/_streams_pump.py:23` defines it; `cuprum/_streams.py:24` and
  `cuprum/_testing.py:36` bind copies by value; and `cuprum/_testing.py:96`
  snapshots it into a dict literal. Tests import it via `cuprum._testing`
  (`cuprum/unittests/test_stream_property_based.py:15`).
  Impact: any override-by-assignment scheme is unsound. Decision D2 removes the
  need for one.

- Observation: the fresh sweep confirms a 64 KiB plateau on the current
  interpreter and kernel. Evidence: tee medians were 11.609 s, 10.252 s,
  9.499 s, 9.139 s, and 8.951 s at 4, 8, 16, 32, and 64 KiB respectively;
  the 64 KiB point was 22.9997% faster than its paired 4 KiB control, with a
  95% interval of 22.5508% to 23.2037%. Impact: 64 KiB is selected as the
  smallest value at the one-read-per-pipe-full call-count floor.

- Observation: the line-callback regression campaign was materially slower
  than the other scenarios. Evidence: the wrap-76 fixture contains roughly
  28 million lines per repeat, and 15 randomized rounds with three repeats at
  each of two read sizes took about 4 hours 57 minutes. Impact: the longer
  runtime is recorded in the companion sweep document and does not change the
  acceptance result.

- Observation: `benchmarks/` is not collected by the test gate.
  Evidence: `PYTEST_TARGETS` (`Makefile:40-43`) lists `cuprum/unittests/` and
  `tests/behaviour/`, not `benchmarks/`. Its only test module self-skips unless
  `CUPRUM_RUN_BENCHMARKS=1`. Running pytest against `benchmarks/` exits 5 with
  nothing collected.
  Impact: benchmark-harness tests belong in `cuprum/unittests/`, alongside the
  existing `test_tee_profile_worker_*.py` modules.

- Observation: the profiling worker needs an execution-local bridge into the
  normal command path.
  Evidence: `TeeProfileWorkerConfig` reaches `run_sync`, whereas
  `_consume_stream` and `_relay_chunks` are invoked below the public execution
  façade. Passing `read_size` only to a direct helper would therefore label a
  measurement without changing its reads.
  Impact: the implementation uses a `ContextVar` only while the private worker
  runs. Stream configuration and pipe-task creation immediately convert that
  task-local value back into the explicit D2 keyword arguments. Concurrent
  workers therefore cannot overwrite one another, and normal executions retain
  `_READ_SIZE` without a public setting.

- Observation: the V1 and V2 properties reject their deliberately seeded
  defects.
  Evidence: truncating every full-size chunk failed V1's pinned
  `payload=b"abcdef", read_size=3` example with captured `"abde"` in
  `/tmp/5-1-1-v1-negative-control.log`. Resetting the incremental decoder for
  each one-byte chunk failed V2's `"☃\r\nx"` example with `"��"` rather than
  `"☃"` in `/tmp/5-1-1-v2-negative-control.log`.
  Impact: the new external-oracle properties are non-vacuous; both mutations
  were immediately reverted before the green scaffold run.

- Observation: the immediate-timeout ownership test retained live subprocess
  transports after intentionally stubbing production termination.
  Evidence: the full capture-control gate timed out after 30 seconds in
  `test_zero_timeout_reconciles_pipe_tasks`, waiting in `asyncio`'s subprocess
  waiter for the test children' deliberate 30-second sleeps. Its pump
  assertion had already succeeded; the focused rerun passed once the test
  reaped its deliberately live children before `asyncio.run` closed the loop.
  Impact: the test now preserves its assertion boundary, then performs local
  cleanup. This removes a deterministic test-harness leak without altering
  product timeout behaviour.

- Observation: V3's boundary test had stopped exercising a real pipeline.
  Evidence: it accepted `stream_backend` but passed an in-memory
  `asyncio.StreamReader` to `_consume_stream`, so neither backend dispatcher
  nor `_pump_stream_dispatch` ran. The chunk partition was also discarded.
  Impact: V3 did not discharge its real-pipeline, both-backend obligation even
  though the selected 64 KiB value and the production implementation were
  correct. The correction restores `build_property_pipeline_case()` and
  `run_parity_pipeline()`, retains their independent hexadecimal byte oracle,
  and requires every near-boundary case to make multiple upstream writes.

- Observation: V4 pinned the selected value instead of the approved range.
  Evidence: `test_default_read_size_is_profiled_plateau` asserted
  `_READ_SIZE == 65536`, contrary to V4's stated inclusive 16384–65536
  contract. Impact: an evidence-backed retune inside the approved range would
  have required a mechanical test edit. The test now asserts the range and
  names `docs/tee-hotpath-read-size-sweep-2026-08-29.md` if it fails.

- Observation: the pre-correction full test gate timed out in the zero-timeout
  task-ownership test.
  Evidence: its isolated invocation timed out after 30 seconds even though it
  captured two started processes. The test passed in 0.46 seconds with the
  Python backend. The focused falsification record is
  `docs/debugging/debugging-plan-2026-09-02-pipeline-timeout.md`.
  Impact: the test's deliberately blocked fixture now forces the Python pump,
  because its assertion is about pipeline-owned task reconciliation. Native
  raw-descriptor lifecycle remains covered by its dedicated test modules.

- Observation: the profile scenario-matrix property exceeded Hypothesis's
  default 200 ms deadline only on a cold run.
  Evidence: the full gate recorded 258.03 ms initially and 35.74 ms on rerun
  for the same `repeat_count=4` case. The property creates fixtures and imports
  benchmark configuration, so wall-clock scheduling is not its contract.
  Impact: `deadline=None` prevents a flaky timing check from obscuring the
  finite scenario-order assertion; its example count and semantic oracle are
  unchanged.

## Decision log

- Decision D10: unstack PR #321 while performing Stage A.
  Rationale: the plan requires the implementation branch to rebase onto
  `origin/main`, but its original stack parent contains eleven unrelated
  commits. Replaying the entire branch would broaden this PR by 45 files and
  violate the plan's scope tolerance before implementation starts. Replaying
  only the four plan commits preserves the branch's intended contents, makes
  the PR independently reviewable, and keeps the required rebase against the
  current `main` line. Retarget the existing draft PR to `main`; retain its
  number, draft state, and remote branch.
  Date/Author: 2026-08-29, implementation agent. No requirement, interface,
  dependency, or acceptance criterion changes.

- Decision D11: use the first plan commit's parent as the rebase cut point.
  Rationale: the current #318 branch no longer shared its original tip with
  this branch, so it was unsuitable as an upstream argument. Commit `41707268`
  is the immediate parent of `8e72b33f`, the first ExecPlan commit, and therefore
  selects the exact independent PR payload. The replay completed without
  conflicts on `origin/main` at `ba32d3f5`.
  Date/Author: 2026-08-29, implementation agent. This completes the Stage A
  stack correction and does not alter functional scope.

- Decision D17: restore V3's real-pipeline oracle rather than extending the
  in-process reader helper.
  Rationale: V3 is specifically the integration proof for `_pump_stream_dispatch`
  and the Python/Rust backend choice; `_consume_at_read_size()` cannot prove
  either. `build_property_pipeline_case()` already supplies a subprocess writer,
  a downstream hexadecimal sink, and a byte-level expected result, so it is the
  smallest existing harness that satisfies the plan without a new test seam.
  Date/Author: 2026-09-02, implementation agent. No production interface,
  dependency, or benchmark result changes.

- Decision D18: make V4 enforce the approved interval, not 65536.
  Rationale: the sweep selected 65536, but the roadmap and V4 approve any
  evidence-backed value from 16384 through 65536. Naming the sweep artefact in
  the failure message directs a future retuning change to its required evidence.
  Date/Author: 2026-09-02, implementation agent. No production change.

- Decision D19: constrain the immediate-timeout ownership fixture to the
  Python pump.
  Rationale: when the test deliberately left its writer unread, final reaping
  through the native raw-descriptor pump did not return before pytest's
  30-second timeout. The fixture's stated property is that pipeline ownership
  settles the created task, not that the native pump handles a blocked descriptor;
  native cleanup tests already cover the latter. This removes a false gate
  failure without reducing the intended ownership evidence.
  Date/Author: 2026-09-02, implementation agent. No production behaviour or
  public interface changes.

- Decision D20: disable the Hypothesis deadline for scenario-matrix ordering.
  Rationale: the property validates deterministic ordering across a small
  integer range, not latency. Its file-backed fixture makes a 200 ms cold-start
  deadline unreliable, while the project uses `deadline=None` for comparable
  fixture-backed or subprocess-facing properties. Keep the finite 20-example
  search and the exact ordering oracle unchanged.
  Date/Author: 2026-09-02, implementation agent. No production behaviour,
  public interface, or coverage reduction.

- Decision D12: retain the direct helper's historical end-of-input default.
  Rationale: `_split_complete_lines(..., final=True)` remains suitable for its
  direct tests and callers, while `_emit_completed_lines` uses `final=False`
  for every decoded chunk and explicitly finalizes after decoder flush. This
  holds only a non-final lone carriage return, leaving all other universal line
  boundaries unchanged. The resulting state transition is local, testable, and
  avoids making a private helper's pre-existing direct contract surprising.
  Date/Author: 2026-08-29, implementation agent. Satisfies V0 and D6 without
  changing a public interface or adding a dependency.

- Decision D13: bridge worker configuration with task-local state, then call
  the D2 parameters explicitly.
  Rationale: the worker must exercise ordinary `run_sync` execution, which has
  no valid public read-size option. A process-global assignment would recreate
  the mirrored-binding hazard D2 rejects. `_override_read_size` is a private
  `ContextVar` context manager scoped to one worker; normal stream setup reads
  it and passes the value directly to `_consume_stream` and `_relay_chunks`.
  This preserves concurrent isolation, leaves public APIs unchanged, and makes
  each worker result read its active size from the module while it is measured.
  Date/Author: 2026-08-29, implementation agent. No requirement, dependency,
  or public-interface change.

- Decision D14: apply the scope tolerance to runtime and benchmark
  implementation files, not its required tests, snapshots, and living plan.
  Rationale: EP-M2 changes ten runtime or benchmark modules, well below the
  eighteen-file limit. Its ten additional files are required regression tests,
  generated snapshots, and the ExecPlan record, all explicitly required by the
  plan and repository instructions. Counting every required verification and
  documentation artefact would make the plan's later mandated documentation
  work breach the limit even with no unplanned implementation scope.
  Date/Author: 2026-08-29, implementation agent. This records the observed
  interpretation; it does not expand runtime scope or alter acceptance criteria.

- Decision D15: include a capture-only scenario in the standard sweep matrix.
  Rationale: the plan's acceptance criterion requires the control, but the
  pre-existing matrix had only the tee scenario. Defining
  `capture-devnull-nocb-s1` beside the existing control means the normal
  profile driver, its snapshots, and Stage E use one canonical scenario set.
  Date/Author: 2026-08-29, implementation agent. This is a measurement control
  only: it exposes neither a public API nor a new runtime behaviour.

- Decision D16: select 65536 bytes for the Python read size.
  Rationale: the 15-round randomized sweep reached the one-read-per-pipe-full
  call-count floor at 65536 bytes, and the gated tee scenario improved by
  22.9997% against the paired 4096-byte control. Its 95% bootstrap interval,
  22.5508% to 23.2037%, clears the 20% requirement. The smaller candidates
  remained above the measured call-count floor, while all regression scenarios
  stayed within the 5% tolerance. The complete raw sample record is in
  `docs/tee-hotpath-read-size-sweep-2026-08-29.md`.
  Date/Author: 2026-08-29, implementation agent. This changes no public
  interface and does not couple the Python value to the Rust buffer size.

- Decision D1: the 20% gate is measured against a fresh 4096 control taken in
  the same interleaved sweep session.
  Rationale: the roadmap cites Table 3, whose tee row yields 18.7%, so the
  literal reading cannot be satisfied by the measurement it points at. The
  baseline was also taken on a different interpreter and a different commit.
  Measuring control and candidate together is the only reading that makes the
  gate a statement about the change. Pre-registered options if the fresh delta
  still falls short: (a) ship the plateau value and amend the roadmap gate to
  the measured figure with its interval; (b) note that ADR-002 scopes its 20%
  bar to a component graduating from prototype to default `auto` dispatch
  (`docs/adr-002-additional-rust-components.md:277-283`), which a Python
  constant change is not, making the bar a roadmap choice rather than an ADR
  requirement; (c) stack with step 5.2 and gate the combined change. Registering
  these now removes the temptation to relitigate the threshold after seeing the
  number.
  Date/Author: 2026-08-29, planning agent. Requires reviewer confirmation.

- Decision D2: thread the read size as a keyword-only parameter rather than
  overriding module globals.
  Rationale: the constant is mirrored four times, one of them a dict literal no
  assignment can update, so any override helper is unsound by construction. Add
  `read_size: int = _READ_SIZE` to `_consume_stream`, `_drain`, `_relay_chunks`,
  and `_drain_stream_reader`. Tests and benchmarks then pass a value instead of
  mutating shared state. This removes the hazard rather than documenting it,
  makes the property tests hermetic and parallel-safe, eliminates the need for
  an agreement test, and expresses the invariant in a signature a reader can
  see. All four functions are underscore-private, so this is not an API change.
  Date/Author: 2026-08-29, planning agent.

- Decision D3: the design-document rationale records the call-count floor, not
  pipe capacity or buffer shuffling.
  Rationale: `_READ_SIZE` never reaches a syscall, and deleting from the front
  of a `bytearray` is not a memmove. The honest mechanism is per-read fixed
  overhead amortized over the slice, bottoming out when one read drains a whole
  pipe-full. A wrong causal model in the design document is what a future
  maintainer will reason from when someone proposes 128 KiB. Record also that
  `st_blksize` on a pipe is 4096, so CPython's `open()` heuristic would
  re-derive the very constant being escaped, and that `F_SETPIPE_SZ` could lift
  the ceiling but is deliberately not used.
  Date/Author: 2026-08-29, planning agent.

- Decision D4: the sweep artefact is a dated Markdown companion with full sample
  tables; no JSON is committed.
  Rationale: `docs/` has never contained a non-Markdown file, and
  `docs/repository-layout.md:35` scopes it to documentation. The precedent the
  baseline sets is narrative Markdown with tables and no committed data. The
  roadmap's "benchmark artefact" wording is satisfied by inlining every raw
  sample in captioned tables, which is also more reviewable than a JSON blob
  nobody diffs.
  Date/Author: 2026-08-29, planning agent.

- Decision D5 (revised): a genuine behavioural red-green transition exists, via
  the line-splitting prerequisite.
  Rationale: the original decision claimed no behavioural red was available
  because the change is behaviour-preserving. That is false for line emission. A
  test pinning a `\r\n` pair at a read boundary fails today and passes after the
  prerequisite fix. For the constant change itself, which is genuinely
  behaviour-preserving once the prerequisite lands, the observable substitute
  remains the benchmark, supported by seeded-mutation negative controls.
  Date/Author: 2026-08-29, planning agent.

- Decision D6: fix the split-boundary defect as a prerequisite rather than
  narrowing the property test around it.
  Rationale: the alternative is to ship a constant that makes an existing defect
  sixteen times rarer while leaving it in place, which is actively hostile to
  whoever bisects it later. The fix is one function: hold a trailing lone
  carriage return in the remainder until the next chunk or the final flush.
  `io.IncrementalNewlineDecoder` is the prior art and sits beside the
  `codecs.getincrementaldecoder` already used at `cuprum/_streams.py:167`. Any
  phase-6 Rust consume dispatcher must reproduce this path's behaviour, so
  settling it now is cheaper than settling it twice.
  Date/Author: 2026-08-29, planning agent.

- Decision D7: the CI ratchet shift is predicted and declared, not worked
  around.
  Rationale: the ratchet compares a within-run Rust-to-Python mean ratio, so
  improving the Python side raises it without any Rust change. Compute the
  predicted shift from the sweep before opening the implementation pull request,
  state in the pull-request body that this change intentionally re-baselines
  `benchmark-ratchet-main-baseline`, and confirm the post-merge `main` baseline
  run is clean before other pull requests land against it. Raising
  `--max-regression` to make the job pass is explicitly out of bounds.
  Date/Author: 2026-08-29, planning agent.

- Decision D8: no new ADR; the rationale goes in `docs/cuprum-design.md` §13.1
  and §13.5.
  Rationale: ADR-002 Proposal 3 already sanctions buffer tuning conditional on
  profile data. This work supplies the data and acts within that decision. A
  constant change with no interface or dependency impact does not clear the bar
  in `docs/documentation-style-guide.md` for a separate ADR. Do not describe the
  Python read size as matching the Rust `buffer_size`: they are unrelated
  constraints landing on the same number, and a documented coupling is an
  invitation to change them together.
  Date/Author: 2026-08-29, planning agent.

- Decision D9: the measurement protocol is interleaved, randomized, and
  interval-gated.
  Rationale: the gate turns on about 1.7 percentage points, which a median of
  five cannot resolve. Run at least fifteen rounds; within each round visit the
  read sizes in randomized order so thermal drift and page-cache eviction of the
  fixture affect all points equally; gate on the upper bound of a bootstrap
  interval for the ratio rather than the point estimate; declare inconclusive if
  effect size over pooled dispersion is below 3. The extra cost is roughly two
  hours on a machine where this is free.
  Date/Author: 2026-08-29, planning agent.

## Outcomes & retrospective

The selected value is 65536 bytes. The gated tee median fell from 11.609 s at
4096 bytes to 8.951 s at 65536 bytes, a 22.9997% paired improvement with a
95% bootstrap interval of 22.5508% to 23.2037%. The capture-only scenario
improved by 11.8415%. Echo-to-`/dev/null`, text-sink, PTY, and line-callback
scenarios improved by 51.9213%, 43.3911%, 11.5028%, and 1.9721%
respectively; no measured scenario regressed beyond 5%.

The line-callback campaign took about 4 hours 57 minutes because the wrapped
fixture supplied roughly 28 million lines per repeat. This was an operational
cost, not a change in scope. The CRLF boundary fix did not surface an
additional line-ending question; vertical tab, form feed, U+2028, and NEL
remain the existing stable separator cases covered by the line-splitting
properties. The 2026-09-02 V3/V4 correction restored the final integration and
retuning contracts; its final deterministic-gate and CodeRabbit evidence is
pending before this outcome is declared complete again.

## Context and orientation

Cuprum runs external programs and pipelines. When a command's output is
captured, echoed, or delivered to line callbacks, the parent process reads it
from the child's pipe in a loop. That loop is the parent-side consume path.

Key modules, by full path:

- `cuprum/_streams_pump.py` defines `_READ_SIZE = 65536` at line 29. It owns the
  inter-stage pump: `_relay_chunks` (line 84) copies one stage's stdout into the
  next stage's stdin with backpressure, and `_drain_stream_reader` (line 102)
  discards to end of file when there is no downstream writer.
- `cuprum/_streams.py` consumes a stage's output. `_drain` (line 60) is the
  canonical read, echo, and capture loop shared by
  `_consume_stream_without_lines` and `_consume_stream_with_lines`, reading at
  line 78. `_write_chunk` (line 143) performs the synchronous echo write.
  `_split_complete_lines` (line 205), `_ends_with_line_ending` (line 230), and
  `_strip_line_ending` (line 236) are the helpers the prerequisite targets.
- `cuprum/_testing.py` re-exports the constant at lines 36 and 96.
- `cuprum/_streams_rs.py` wraps the optional Rust extension; its `buffer_size`
  defaults of 65536 are separate knobs, untouched here.

The tests that matter:

- `cuprum/unittests/test_stream_property_based.py` derives its boundary window
  from `_READ_SIZE` (`_BOUNDARY_DELTA = 512` at line 28), so it tracks the
  constant automatically but is bounded by the kernel argv limit.
- `cuprum/unittests/test_stream_pump_runtime_behaviour.py` builds payloads from
  `_READ_SIZE` and uses in-process readers, so it is safe at the larger size.
- `tests/features/stream_parity.feature:24` already provides a large-payload
  backpressure scenario: three stages, 1 MiB, both backends, generating its
  payload inside the subprocess to avoid the argv limit
  (`tests/behaviour/test_stream_parity_behaviour.py:204-225`). This already
  discharges the end-to-end behavioural obligation; no new feature file is
  needed.
- `tests/helpers/parity.py:105` documents 4096-byte reads and goes stale.

The benchmark harness, under `benchmarks/`:

- `tee_profile_worker.py` runs one scenario and emits `worker-result.json`.
  `TeeProfileWorkerResult` (line 67) is a **total** `TypedDict`, so a new key is
  mandatory at every construction site. `_build_worker_result` (line 392) sits
  at exactly the `max-args = 4` limit (`pyproject.toml:218`), and the comment at
  lines 161-165 records that this is structural; a new input must therefore go
  on `TeeProfileWorkerConfig` (line 84), where peer inputs are validated.
- `tee_profile_scenarios.py` defines the matrix and `_worker_command` (line
  297), which is what the `perf` and `py-spy` profilers execute.
- `tee_profile_profilers.py` runs the `none` profiler **in-process** (lines
  26-46), so a command-line flag alone would never reach the default sweep path.

Terms:

- _Read size_: the maximum bytes requested per read on an asyncio stream. A read
  returns at most that many and at least one, or zero at end of file.
- _Plateau_: the point beyond which increasing the read size stops helping.
- _Negative control_: a temporary seeded fault used to prove a test can fail.

## Conformance basis

- `docs/roadmap.md` item 5.1.1 is the requirement of record and is checked
  complete by this implementation.
- `docs/tee-hotpath-profiling-baseline-2026-06-12.md` §1 (Table 3) supplies the
  prior measurement and the 16-64 KiB range.
- `docs/adr-002-additional-rust-components.md` Proposal 3 (lines 176-191)
  sanctions buffer tuning conditional on profile data. Its acceptance thresholds
  (lines 278-289) supply the 20% and 5% bars, and scope the 20% bar to component
  graduation. Its status is Proposed, not Accepted; the roadmap treats it as
  binding and this plan follows the roadmap.
- `docs/cuprum-design.md` §13.1 and §13.5 are the design sections of record.
- `docs/adr-008-rust-pump-observation-channel.md` constrains only by excluding
  buffer and throughput counters from the public API; this plan adds none.

There is no Terms of Reference document in this repository.

```plaintext
roadmap-5.1.1 -> ADR002-Proposal3 -> design-13.1 -> EP-M3 -> read-size-sweep doc
roadmap-5.1.1 -> ADR002-thresholds -> EP-M4 -> acceptance benchmark transcript
prerequisite-line-split -> EP-M1 -> tests: line emission at a split boundary
roadmap-5.1.1-subtask -> EP-M2 -> tests: read-size invariance against an oracle
```

## Verification plan

Once the prerequisite lands, the constant change introduces no new logic and no
new invariant. It places existing invariants at a parameter value they have
never been exercised at, and the burden is to show independence rather than
coincidence at 4096.

Axioms:

- A1: `asyncio.StreamReader.read(n)` returns between 1 and `n` bytes, or empty
  at end of file, without reordering or dropping. Documented third-party
  contract; not verified here.
- A2: the read transport calls `os.read(fd, 262144)` independently of
  `_READ_SIZE`, so reader-side syscalls do not change. Measured.
- A3: Linux caps one argv entry at 131072 bytes; a 98304-byte payload
  base64-encodes to exactly that and raises `E2BIG`. Measured.
- A4: peak per-stream live bytes is 65536 regardless of read size. Measured.

Obligations:

- V0: emitted lines are identical whether or not a `\r\n` pair is split across a
  read boundary.
  Method: parameterized unit test plus a behavioural scenario.
  Rationale: a finite, well-understood partition; the interesting cases are
  enumerable, so a property test would add nothing over pinned examples.
  Domain: `\r\n`, lone line feed, and lone carriage return placed at, adjacent
  to, and away from a read boundary, including a payload ending in a lone
  carriage return.
  Artefact: `cuprum/unittests/test_stream_read_size.py`, plus a scenario added
  to the existing `tests/features/stream_parity.feature`.
  Evidence: fails before the fix with `('a', '', 'b')` against an expected
  `('a', 'b')`; passes after.
  Non-vacuity: this is a real red-to-green transition on current code, already
  demonstrated during planning. The negative control is reverting the fix.

- V1: captured text is independent of read size, against an external oracle.
  Method: Hypothesis property test comparing output to the payload decoded with
  the configured encoding and error handler, computed in the test.
  Rationale: comparing read sizes to each other only asserts membership of an
  equivalence class, which a uniformly wrong implementation satisfies. An
  external oracle catches uniform corruption; with `errors="replace"`,
  whole-string decode and incremental decode plus final flush agree.
  Domain: payloads including multi-byte UTF-8, with and without a trailing
  newline. Read sizes drawn from a fixed set spanning below, at, and above the
  boundary. Payloads for read sizes below 64 are capped at 8 KiB, because the
  line path is quadratic in payload length.
  Artefact: `cuprum/unittests/test_stream_property_based.py`, driving
  `_consume_stream` over an in-process `asyncio.StreamReader` with the
  `read_size` parameter from decision D2, so neither A3 nor pipe capacity
  applies.
  Evidence: passes at 4096 and at the chosen value.
  Non-vacuity: read size 1 forces maximal boundary crossings. The negative
  control drops the final chunk only when the payload length is an exact
  multiple of the read size, which is read-size-coupled and invisible to the
  existing suite, unlike a mutation that truncates every stream and is caught by
  tests that already exist.

- V2: emitted lines are independent of read size.
  Method: Hypothesis property test with constructed boundary placement, plus
  pinned examples.
  Rationale: line splitting and incremental decoding are the parts most
  sensitive to where a chunk ends. Boundaries must be constructed, not hoped
  for: `assume()` cannot express that some example in a run satisfied a
  predicate, and would either over-filter into `Unsatisfiable` or, under the
  module's `derandomize=True`, fail identically every run. `event()` and
  `target()` do not affect pass or fail at all.
  Domain: draw a read size and an offset in minus one, zero, plus one, then
  place the line ending at exactly that offset from the boundary. Oracle: the
  emitted lines rejoined with their stripped endings reconstruct the decoded
  text, and no emitted line contains an interior ending. Restrict the strategy
  to `\r\n`, line feed, and carriage return; production also splits on vertical
  tab, form feed, U+2028, and NEL while retaining the separator, which is stable
  across read sizes and therefore not a V2 finding, but would be misdiagnosed as
  one.
  Artefact: same module as V1.
  Evidence: passes at both constants once the prerequisite has landed.
  Non-vacuity: constructed boundaries guarantee the interesting case on every
  example, and pinned examples always run. The negative control resets the
  incremental decoder per chunk, corrupting any character split across a
  boundary; it is read-size-coupled by construction.

- V3: byte-exactness through a real pipeline at the new boundary.
  Method: the existing Hypothesis boundary property test, retargeted.
  Rationale: V1 and V2 use in-process readers and so exercise neither real pipes
  nor the backend dispatcher.
  Domain: payload sizes within 512 bytes either side of `_READ_SIZE`, on both
  backends, subject to A3.
  Artefact: the existing boundary test, plus a new
  `test_boundary_window_fits_argv_budget` asserting the window against a named
  `_ARGV_PAYLOAD_CEILING = 98_300`. This is a test, not a module-level
  assertion: `.rules/python-00.md` forbids import-time side effects, and an
  import-time failure surfaces as a collection error that can mask sibling
  tests.
  Evidence: passes on both backends; the budget test fails if a future value
  overflows argv.
  Non-vacuity: assert per example that the case's chunk count exceeds one, which
  holds unconditionally for this strategy. Retain a companion case near 4 KiB so
  moving the boundary to 64 KiB does not vacate small-payload multi-chunk
  coverage.

- V4: the chosen value respects the roadmap range and the platform ceiling.
  Method: parameterized unit test asserting the value lies within 16384 and
  65536 inclusive.
  Rationale: asserts the contract and its reasons, not the integer. Pinning the
  exact value would make it a second source of truth and a pure change-detector
  that the next tuning commit edits mechanically; it would also contradict the
  claim that the constant can be reverted independently.
  Artefact: `cuprum/unittests/test_stream_read_size.py`.
  Evidence: passes after the bump; the failure message names the sweep artefact
  a future change must produce.
  Non-vacuity: a value outside the range fails it.

- V5: the performance claim.
  Method: measured benchmark under decision D9's protocol.
  Rationale: a performance claim is discharged only by measurement.
  Domain: `tee-devnull-nocb-s1` as the gated scenario;
  `capture-devnull-nocb-s1` as the phase-6-relevant control;
  `echo-devnull-nocb-s1`, `echo-pty-nocb-s1`, `echo-textblackhole-nocb-s1`,
  `echo-devnull-cb-s1`, and `echo-devnull-nocb-s4-python` for regression.
  Artefact: the dated sweep companion document.
  Evidence: bootstrap interval upper bound at or below 0.80 against the
  same-session 4096 control; no scenario worse than 5%.
  Non-vacuity: the control is measured in the same interleaved session, so an
  environment-wide slowdown moves both sides. Record every raw sample so
  dispersion is inspectable. Discard any sample whose stream pipe capacity is
  below 65536, since such a sample structurally cannot show the effect.

V1 and V2 exercise a Python-only path by design, so parameterizing them over the
`stream_backend` fixture would double their runtime while advertising Rust
coverage that does not exist. V3 and the existing parity scenario carry the
backend-parity burden.

ADR-002's actual memory criterion, no increase in captured-output memory beyond
the payload, is satisfied by inspection: the capture buffer in `_drain` is
untouched. Per axiom A4 there is no per-stream memory increase to measure, so no
memory obligation is stated.

No formal proof or bounded model check is proposed. The change sets an integer;
the obligations are differential and statistical. Modelling the asyncio runtime
would breach the boundary axiom A1 establishes.

## Plan of work

Stage A: rebase and confirm. Rebase onto `origin/main`, re-verify every line
citation in `Context and orientation`, and record the interpreter path and
version, pipe capacity, and whether the extension is built in release mode.
Go/no-go: citations verified and the machine quiescent.

Stage B: the line-splitting prerequisite. Write the failing V0 test first. Fix
`_split_complete_lines` to hold a trailing lone carriage return in the remainder
until the next chunk or the final flush. Add the changelog entry. Go/no-go: V0
red then green, full suite otherwise unchanged.

Stage C: injectable read size. Add the `read_size` keyword-only parameters per
decision D2. Add `read_size` to `TeeProfileWorkerConfig` and
`TeeProfileScenario`, thread it through `_worker_command` so profiler-mediated
runs carry it, surface it in `TeeProfileWorkerResult` read back from the module
at measurement time rather than echoed from the argument, and add `--read-sizes`
to the existing driver rather than a new module. Regenerate the affected syrupy
snapshots. Go/no-go: benchmark tests green in `cuprum/unittests/`.

Stage D: verification scaffold at 4096. Add V1, V2, V3, and the argv-budget
test. Run each negative control, capture the failure, revert it. Go/no-go: all
green at 4096 and every control observed to fail for its stated reason.

Stage E: measurement. Run the interleaved sweep per decision D9 across 4096,
8192, 16384, 32768, and 65536 for the gated and control scenarios, then the
regression scenarios at two points each. Choose the smallest in-range value
reaching the call-count floor. Compute the predicted ratchet shift. Go/no-go:
the interval clears the gate and no scenario regresses beyond 5%.

Stage F: the change. Raise `_READ_SIZE`. Add V4. Scale the payloads in the tests
identified under `Risks` so they keep spanning multiple reads. Update the stale
docstring at `tests/helpers/parity.py:105`. Re-run the gated scenario with the
constant compiled in. Go/no-go: full suite green except the two documented local
baseline failures, each confirmed to reproduce on `main`.

Stage G: documentation. Write the dated sweep companion document and cross-link
it from the baseline document and `docs/contents.md`. Update
`docs/cuprum-design.md` §13.1 and §13.5 per decision D3,
`docs/developers-guide.md` (canonical drain loop, and the worker-payload table
at lines 1334-1345), `benchmarks/README.md`, and `CHANGELOG.md`. Correct the
stale citations. Mark roadmap 5.1.1 done. Go/no-go: all gates green.

## Milestones and plateaus

- EP-M1: branch rebased and the line-splitting defect fixed. Evidence: V0 red
  then green. Conformance: a deliberate, documented behaviour change on a
  private pre-1.0 path; no interface or dependency change. Recovery: revert one
  function. Remaining: the constant is unchanged.

- EP-M2: read size injectable, sweep support present, scaffold green at 4096.
  Evidence: V1 to V3 pass; negative controls recorded. Conformance: new
  parameters are keyword-only on private functions; the worker payload gained a
  mandatory key, with snapshots regenerated. Recovery: additive; revert
  independently. Remaining: no measurement yet.

- EP-M3: sweep run and artefact written; constant still 4096. Evidence: the
  companion document with per-sample tables and intervals. Conformance: ADR-002
  Proposal 3's data requirement satisfied. Recovery: re-run at will. Remaining:
  the artefact records a measurement only. The decision it implies is taken at
  EP-M4, so EP-M3 must not assert a value the code does not implement.

- EP-M4: `_READ_SIZE` raised, suite green, gate re-confirmed, ratchet shift
  predicted. Evidence: transcripts. Conformance: value in range; no
  captured-output memory increase; no scenario beyond 5%. Recovery: reverting
  the constant leaves every test valid, because V4 asserts a range and the
  invariance tests are parameter-independent by construction. Compatibility:
  none required; the constant is private, pre-1.0, with no external consumer.

- EP-M5: documentation, changelog, and roadmap current; all gates green.
  Recovery: documentation-only.

## Concrete steps

Run from the repository root. Log gates through `tee`.

Stage A:

```plaintext
git fetch origin && git rebase origin/main
sed -n '23p' cuprum/_streams_pump.py
./.venv/bin/python -c "import sys; print(sys.executable, sys.version.split()[0])"
uv run python -c "import fcntl,os;r,w=os.pipe();print('pipe',fcntl.fcntl(w,1032))"
pgrep -af 'make (lint|test)|pytest' || echo "no gate running"
```

Expected: line 29 reads `_READ_SIZE = 65536`, pipe capacity 65536, and no gate
running. If `dist/fixtures/` is absent, regenerate it:

```plaintext
uv run python benchmarks/deterministic_b64_fixture.py --seed 12345 \
  --raw-bytes 1610612736 --wrap 0 \
  --output dist/fixtures/seed12345-nowrap.b64 \
  --manifest dist/fixtures/seed12345-nowrap.json
uv run python benchmarks/deterministic_b64_fixture.py --seed 12345 \
  --raw-bytes 1610612736 --wrap 76 \
  --output dist/fixtures/seed12345-wrap76.b64 \
  --manifest dist/fixtures/seed12345-wrap76.json
```

These write about 4 GiB under `dist/`; confirm free space first.

Stage B:

```plaintext
set -o pipefail; uv run pytest cuprum/unittests/test_stream_read_size.py \
  2>&1 | tee /tmp/5-1-1-linesplit-red.log
```

Expected red, with lines `('a', '', 'b')` against an expected `('a', 'b')`.
After the fix, the same command passes.

Stage C:

```plaintext
set -o pipefail; uv run pytest cuprum/unittests/test_tee_profile_worker_cli.py \
  cuprum/unittests/test_tee_profile_worker_core.py \
  cuprum/unittests/test_profile_driver.py 2>&1 | tee /tmp/5-1-1-worker.log
```

Expected: red until `read_size` is threaded, then green after
`--snapshot-update` regenerates `test_tee_profile_worker_core.ambr`,
`test_profile_driver.ambr`, and `test_maturin_build.ambr`. The last enumerates
every file under `cuprum/unittests/`, so any new test module breaks it.

Stage E:

```plaintext
export RUSTFLAGS="-C force-frame-pointers=yes"
uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml
set -o pipefail; uv run python -m benchmarks.profile_tee_hotpath \
  --read-sizes 4096,8192,16384,32768,65536 --rounds 15 --randomize-order \
  run-scenario --scenario tee-devnull-nocb-s1 2>&1 | tee /tmp/5-1-1-sweep.log
```

Repeat for `capture-devnull-nocb-s1` and the regression scenarios. Compute the
ratio and its bootstrap interval from the recorded samples.

Stage G (final gates), run sequentially, never in parallel:

```plaintext
set -o pipefail; make check-fmt 2>&1 | tee /tmp/5-1-1-check-fmt.log
set -o pipefail; make lint 2>&1 | tee /tmp/5-1-1-lint.log
set -o pipefail; make typecheck 2>&1 | tee /tmp/5-1-1-typecheck.log
set -o pipefail; make test 2>&1 | tee /tmp/5-1-1-test.log
set -o pipefail; make markdownlint 2>&1 | tee /tmp/5-1-1-markdownlint.log
set -o pipefail; make nixie 2>&1 | tee /tmp/5-1-1-nixie.log
```

## Validation and acceptance

A user can observe success by running a command producing large captured output
and seeing it finish faster with identical bytes, and by no longer receiving a
spurious empty line when a `\r\n` pair straddles a read boundary.

Red-Green-Refactor evidence:

- Red is genuinely available for the prerequisite (V0) and for the sweep tooling
  (Stage C), and both must be observed failing before implementation.
- Red is not available in behavioural form for the constant itself, which is
  behaviour-preserving once the prerequisite lands. The substitute is the
  measured wall-time transition plus the seeded-mutation negative controls, each
  observed to fail and then reverted.
- Green: `make test` passes after the constant is raised.
- Refactor: Stage G gates pass.

Quality criteria:

- Tests: `make test` passes. Known local-only failures are Rust trybuild
  snapshot drift against the pinned 1.92.0 toolchain and
  `test_rust_pump_stream_propagates_io_errors` reporting a null errno; both must
  be confirmed reproducing on `main` before being dismissed.
- Verification: V0 to V5 discharged, with negative-control transcripts recorded.
- Lint and typecheck: `make check-fmt`, `make lint`, `make typecheck` pass.
  Watch `max-args = 4`, `max-locals = 10`, and `max-complexity = 8`
  (`pyproject.toml:216-220`); factor the per-read-size run into a helper before
  writing the property tests.
- Documentation: `make markdownlint` and `make nixie` pass.
- Performance: the bootstrap interval clears 20% against the same-session
  control, and no scenario is worse than 5%.

Quality method: delegate the gates to the `scrutineer` subagent, which runs them
sequentially and returns a bounded report; read the cited log on failure rather
than re-running.

## Idempotence and recovery

The sweep writes only under `dist/` and does not mutate production code. Gates
are re-runnable; if a run collides with another build and corrupts the coverage
database, delete stale `.coverage*` files and re-run sequentially.

Rollback is not one line, and pretending otherwise would mislead whoever needs
it. In order: revert the constant in `cuprum/_streams_pump.py`; revert the
payload scaling in the tests listed under `Risks`; revert the docstring at
`tests/helpers/parity.py:105`; revert the design-document and changelog text;
un-tick the roadmap. V4 asserts a range rather than a value, so it survives the
revert unchanged. The line-splitting fix and the `read_size` parameters are
independently valuable and should not be reverted with the constant.

## Artefacts and notes

Logs at `/tmp/5-1-1-*.log`. Committed artefact: a dated read-size sweep
companion document under `docs/`, named for the ISO date of the sweep session.

Confirmed during Stage A and the Stage E sweep:

```plaintext
.venv/bin/python                 3.13.13   (baseline document used 3.14.4)
io.DEFAULT_BUFFER_SIZE           8192
asyncio _DEFAULT_LIMIT           65536
_UnixReadPipeTransport.max_size  262144
pipe capacity                    65536
```

Read call counts over a 64 MiB subprocess stream, with peak live bytes per
stream:

| Read size | Read calls | Max chunk | Peak buffer plus chunk |
| --------- | ---------- | --------- | ---------------------- |
| 4096      | 16384      | 4096      | 65536                  |
| 16384     | 4096       | 16384     | 65536                  |
| 65536     | 1024       | 65536     | 65536                  |
| 262144    | 1024       | 65536     | 65536                  |

_Table 1: read calls fall to a floor at 65536; peak memory is unchanged._

The argv ceiling, measured by invoking `/bin/true` with one oversized argument:

```plaintext
payload=  66048  b64len=  88064  OK
payload=  98304  b64len= 131072  FAIL [Errno 7] Argument list too long
```

Historical figures for continuity: Table 2 records `tee-devnull-nocb-s1` at
14.45 s; Table 3 records tee at 13.50 s and 10.98 s for 4 and 64 KiB, and echo
at 5.91, 4.64, 4.64, and 4.65 s for 4, 16, 64, and 256 KiB.

Negative-control transcripts are pasted here at EP-M2.

## Interfaces and dependencies

No new runtime dependency. No public interface change.

In `cuprum/_streams_pump.py`, the constant changes value only:

```python
_READ_SIZE = 65536  # selected by the Stage E sweep
```

Per decision D2, add keyword-only parameters defaulting to the constant:

```python
async def _drain(stream, config, *, on_chunk=None, read_size=_READ_SIZE): ...
async def _consume_stream(stream, config, *, on_line=None, read_size=_READ_SIZE): ...
async def _relay_chunks(reader, writer, *, read_size=_READ_SIZE): ...
async def _drain_stream_reader(reader, *, read_size=_READ_SIZE): ...
```

In `cuprum/_streams.py`, `_split_complete_lines` gains a final-flush flag so a
trailing lone carriage return is held in the remainder until the next chunk
arrives or the stream ends.

In `benchmarks/`, `read_size` joins `TeeProfileWorkerConfig` and
`TeeProfileScenario` rather than `_build_worker_result`'s argument list, which
is at the `max-args` limit. It is emitted by `_worker_command` and appears in
`TeeProfileWorkerResult`, read back from the module at measurement time. The
existing driver gains `--read-sizes`, `--rounds`, and `--randomize-order`; no
new module is added.

New test artefact: `cuprum/unittests/test_stream_read_size.py` for V0 and V4.
V1 to V3 extend `cuprum/unittests/test_stream_property_based.py`. The
behavioural scenario is added to the existing
`tests/features/stream_parity.feature`.

## Signposted documentation and skills

Read first: `AGENTS.md` for gates and conventions;
`docs/documentation-style-guide.md` for Markdown and ADR rules;
`docs/developers-guide.md` "Canonical stream-drain loop" and "Profiling harness
overview"; `docs/cuprum-design.md` §13; the profiling baseline;
`docs/adr-002-additional-rust-components.md` Proposal 3 and its thresholds; and
`.rules/python-00.md` with `.rules/python-typing.md`.

Skills: `execplans` when revising this plan; `python-router` then `hypothesis`
for the property tests and `python-testing` for the behavioural scenario;
`en-gb-oxendict` for prose; `nextest` if the Rust portion of `make test` needs
investigation.

## Revision note

Revision 4, 2026-09-02, after review found two incomplete verification claims:
V3 now executes the near-64 KiB cases through the real two-process pipeline on
both backends, asserts more than one upstream write, and retains the external
hexadecimal expected-byte oracle. V4 now asserts the approved 16–64 KiB range
and names the sweep artefact in its failure message. The roadmap acceptance
text records these contracts. The ExecPlan is `IN PROGRESS` until the renewed
deterministic gates and CodeRabbit review pass.

Revision 3, 2026-08-29, after the Stage E sweep and implementation: selected
65536 bytes from the randomized 15-round curve, recorded the paired bootstrap
intervals and all raw samples in the dated companion document, raised the
constant, and updated the design, developer, benchmark, baseline, changelog,
ADR-001, contents, and roadmap references. EP-M5 then recorded the final
deterministic-gate and CodeRabbit evidence, completing the ExecPlan.

Revision 2, 2026-08-29, after a six-expert design review. Substantive changes:
added the line-splitting prerequisite (decision D6) after discovering that line
emission is already read-size dependent, which falsified the original decision
D5; replaced the module-global override helper with injectable parameters (D2)
after finding four mirrored bindings, one unreachable by assignment; corrected
the plateau rationale from a convergence-of-ceilings story to the measured
call-count floor (D3), since `_READ_SIZE` never reaches a syscall; struck the
memory risk, its 5% tolerance, and the planned users'-guide note after measuring
that peak per-stream memory is invariant; strengthened the measurement protocol
to interleaved randomized rounds with a bootstrap interval (D9); added the
capture-only scenario so the phase-6 gate this plan exists to protect is
measured honestly, and added the PTY, text-sink, and line-callback scenarios
after measuring a sixteenfold rise in worst-case event-loop stall; added the CI
ratchet prediction (D7); dropped the committed JSON (D4), the new sweep module,
the new feature file, and the binding-agreement obligation; and fixed the
benchmark test paths, which previously named a directory the test gate does not
collect. The shape of the remaining work is unchanged: measure, then raise the
constant. Decisions D1, D4, D6, and D7 should be confirmed at approval.
