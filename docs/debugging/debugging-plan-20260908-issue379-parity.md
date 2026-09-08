# Debugging plan: issue 379 validation failures

**Generated:** 2026-09-08 **Issue:** #379 **Severity:** high (possible
captured-output loss) **Falsification sub-agent:** `alchemist`

The planning agent prepared this document. The alchemist executes the bounded
experiment; it must not run full repository gates or edit production code.

## Current state

Round 33 local checks are green: Python recorded 1,568 passes and one skip,
Rust recorded 112/112 tests with no skips, and the extension-required suite
recorded 80 passes and one skip. The nine compiler probes, Windows cross-
Clippy, development build, and cached Kani setup also passed. The formal and
native evidence remains 2 Verus functions, 7 native plus 11 safe Kani
harnesses, 13 Miri tests, and 4 detected fault mutations. Hosted Windows/macOS
runtime checks and CodeRabbit review remain pending.

Round 33's `fmt`, `check-fmt`, `lint`, `typecheck`, `test`, `markdownlint`, and
`nixie` gates all passed.

The historical native payload mismatch remains unexplained and is not claimed
fixed. H7 reproduced an empty `HELLO` result on the archived baseline, while H9
identified and fixed a separate closing-transport reader-lease gap. Baseline
comparison and the bounded investigations below retain their original scope.

## Problem and context

An earlier full native-enabled Python suite reported 1,565 passes, one skip,
and two failures in `/tmp/issue379-round28-test.log`. The Rust two-stage
backpressure case returned something other than its expected 1 MiB ASCII
payload; the three-stage and Python cases passed. A separate fail-fast test
took 1.089 seconds against a one-second assertion. This shared host was heavily
loaded. The exact captured byte count and EOF-grace observations are not in the
failure report. Cuprum's Rust boundary extraction and Python pre-transfer
validation were uncommitted at the time; the starting revision is
`34a59eac4cd13de82c3730d4b569eeaefbb5ee61`.

## H1: capture EOF-grace expiry truncates the parity output

The Python capture consumer can miss its fixed EOF-grace deadline under load,
causing cancellation before all already-produced output is captured. This is
independent of a native descriptor-transfer error.

Prediction: a reproduced truncation coincides with a capture EOF-grace expiry
observation. The same mechanism can occur with the starting revision's backend.
A truncated run with no expiry observation falsifies this proposed explanation
for that run. No reproduction is inconclusive, not a pass or falsification.

### Minimal experiment

1. Use a scratch runner under `.cache/issue379-parity-experiment`; do not edit
   tracked files. Import the existing parity pipeline builder and run helper.
   Force the Rust backend in a fresh interpreter and record the imported Python
   package and native extension paths. Record their file hashes.
2. Run only the existing two-stage, 1 MiB ASCII pipeline, at most 20 times.
   Capture the existing EOF-grace reporting hook/log events without changing
   its scheduling, deadline, or return value. Report each captured byte count,
   equality to the expected payload, stage exit codes, and expiry observations.
   Bound each pipeline to 30 seconds. Stop on the first reproduced truncation.
3. If truncation has no expiry, stop: H1 is falsified for that execution.
   If no truncation occurs, stop and report inconclusive.
4. Only if truncation coincides with expiry, build the starting revision's
   native wheel from a `git archive` source copy under `.cache`, with shared
   Cargo registry and build output under the original `rust/target`. Do not
   install it into the working environment. Extract it into a scratch import
   root, then run the identical bounded experiment in a fresh interpreter.
   Record provenance and whether the baseline reproduces the same mechanism.

Save stdout, stderr, and observations to `/tmp/issue379-parity-h1.log`. Return
falsified, not-falsified, or inconclusive, with exact evidence. Do not add
load, change timeouts in production, broaden the experiment, or classify an
intermittent non-reproduction as proof of correctness.

## H2: the fail-fast assertion measures host scheduling

The one-second wall-clock assertion includes child startup and scheduling,
although the property under test is cancellation of a pending command. This
hypothesis is not authorized for execution in the H1 packet. A subsequent
bounded experiment must distinguish successful cancellation from waiting for
the slow command, using completion evidence rather than relaxing the timer.

## Termination and next action

Stop after the H1 packet and report to the planning agent. The planner must
revise the hypothesis or authorize another specific experiment before further
investigation. Production fixes require regression evidence and all commit
gates. The first failed full run must remain recorded.

## H1 result

The alchemist's 20 fresh-interpreter trials all returned 1,048,576 matching
bytes, stage exits `[0, 0]`, and no EOF-grace expiry. Evidence:
`/tmp/issue379-parity-h1.log`. H1 is inconclusive; this does not explain the
full-suite mismatch. The imported native extension hash was
`e04a682ef69e8aa17bab98d4f2b6fb9dc7a29e31afa4d4be58ca2dd274d70850`.

## H2 authorized experiment

The same alchemist may now execute exactly two real concurrent runs in a
scratch script, using the existing catalogue and `run_concurrent_sync` API. The
first child waits 1.1 seconds before exiting 42; the second waits 10 seconds
before printing a completion marker. The delay deliberately represents latency
before the first failure becomes observable; it is not a claim about the cause
of the historical timing measurement.

Run once with fail-fast enabled, then once disabled. Bound each run to 20
seconds. Record elapsed time, submission indices, failure indices, exit codes,
and completion-marker presence. Prediction: enabled cancellation excludes the
slow result even though total elapsed time exceeds one second; disabled mode
includes the slow completion. If the enabled run includes the slow completion,
this counterexample to the timer's validity is not established. If the two
modes differ as predicted, the one-second assertion is not a valid standalone
cancellation criterion. Save `/tmp/issue379-fail-fast-h2.log` and stop. Do not
edit tracked code, change production deadlines, or run other tests.

## H2 result

Fail-fast returned only submission zero after 1.390 seconds, with exit 42 and
no slow completion marker. Collect-all returned both submissions after 10.270
seconds, including the slow command's successful marker. Evidence:
`/tmp/issue379-fail-fast-h2.log`. The hypothesis is not falsified. The
regression now checks exclusion of the pending submission directly, with a
ten-second child delay; it no longer equates startup latency with failed
cancellation. This does not attribute the original run's delay to a particular
cause.

## H3: the pre-buffer hand-off loses bytes before native pumping

The reader's buffered prefix can change across the awaited writer drain, or the
writer can retain pending bytes when native pumping begins. This could produce
a mismatch even when native reads and writes conserve their own bytes.

Prediction: a failing run has a prefix/native/capture accounting discrepancy at
this seam. Equal accounting on a reproduced mismatch falsifies this
explanation. No reproduction remains inconclusive.

The alchemist may run at most 40 trials in one interpreter, alternating the
existing two-stage and three-stage 1 MiB pipeline with the Rust backend
explicitly selected. Use scratch wrappers around the actual
`_drain_reader_buffer` and `rust_pump_stream`: record reader buffer lengths
before and after the original drain, writer transport pending bytes after
drain, native returned byte counts, captured length and stage exits. Do not
alter return values, add scheduling delays, or replace the actual operations.
Confirm the native wrapper was called; an unused imported extension is not
evidence of native execution. Bound each pipeline to 30 seconds, stop at the
first mismatch, and save `/tmp/issue379-parity-h3.log`. Record file provenance.
Do not run full gates or edit tracked code. Stop and report the verdict.

## H3 result and H4 authorization

All 40 trials matched, with 60 confirmed native pump calls. Every pre-buffer
and pending writer buffer was empty. Evidence: `/tmp/issue379-parity-h3.log`.
H3 is inconclusive and did not exercise its proposed buffered-prefix condition.

H4 narrows the hypothesis: a non-empty asyncio prefix is mishandled while the
native pump independently transfers the remainder. The alchemist may use the
same scratch instrumentation for at most eight two-stage trials, delaying the
second actual subprocess creation by 0.1 seconds with an awaited sleep. This is
an explicit scheduling intervention to allow the first process's reader buffer
to fill before hand-off, not artificial host load. All subprocess and native
operations remain real. Use the existing 1 MiB payload and 30-second per-trial
limit. Confirm a non-zero prefix; otherwise report inconclusive. Record prefix
length, remaining prefix, writer pending bytes, native count, captured length
and exit statuses. Stop on first mismatch. A reproduced mismatch with lost
prefix bytes supports H4; preserved non-zero prefixes falsify the proposed loss
for the exercised schedules only. Save `/tmp/issue379-parity-h4.log`; no
tracked edits or full gates.

## H4 result and gate resumption

All eight delayed-start trials preserved the full payload. Seven prefixes
contained 196,608 bytes and one contained 139,264 bytes. Each prefix plus its
native return count equalled 1,048,576, with zero pending writer bytes and
stage exits `[0, 0]`. Evidence: `/tmp/issue379-parity-h4.log`. Prefix loss is
falsified for these exercised schedules; the historical mismatch remains
unexplained. No native implementation change follows from these experiments.

The full gate sequence may resume with the improved parity failure diagnostic
and direct fail-fast cancellation assertion. A repeated mismatch must be
investigated using its new byte counts and stage exits; the earlier failure
must not be erased by a later passing run.

## Reproduction in round 29 and H5 authorization

Round 29 passed all 1,567 unit cases (one skip), including all four large
payload parity cases. Its behavioural stream parity case then failed with a
payload mismatch and no pipeline failure index. That scenario has three stages.
The behavioural assertion now also records lengths and stage exits. The
discrepancy is recurring and gates are paused again.

H5 tests whether pytest's surrounding scenario sequence exposes the missing
schedule: a mismatch should reproduce under the real behavioural module with
passive hand-off accounting, even though scratch pipelines passed. The
alchemist may run only `tests/behaviour/test_stream_parity_behaviour.py`, at
most ten fresh pytest invocations, stopping on the first mismatch. Use an
ignored scratch pytest plugin to wrap the original drain and native pump and
record the same prefix/pending/native counts as H3, associated with each node
ID. Preserve fixtures, pytest capture and return values. Each invocation has a
60-second external bound; do not add delays or host load. Record native module
provenance and actual calls. Save `/tmp/issue379-parity-h5.log` and stop; no
tracked edits or full gates. Non-reproduction remains inconclusive.

## H5 result and H6 authorization

Nine instrumented module runs passed all eight cases each. Every three-stage
native transfer accounted for 1 MiB per hop, including one 8 KiB prefix. One
earlier scratch plugin registration error consumed the remaining invocation
budget without collecting tests. Evidence: `/tmp/issue379-parity-h5.log`. H5 is
inconclusive.

H6 tests surrounding-module state or instrumentation sensitivity: the exact
26-case `tests/behaviour/test_[s-z]*.py` group from the failed Makefile batch
may reproduce where the instrumented parity module does not. The alchemist may
run this group without its scratch plugin at most ten times in fresh pytest
interpreters, using the Makefile's serial `-v -n 0` flags. Each run has a
60-second external bound. Stop on the first mismatch, retaining the new
length/status assertion and full captured failure output in
`/tmp/issue379-parity-h6.log`. Do not invoke the full repository gate, add
load, change environment beyond the Makefile's existing test settings, or edit
tracked files. A reproduction supports this narrower context dependence;
non-reproduction is inconclusive and does not establish correctness.

## H6 result and diagnostic rerun

The fifth exact batch reproduced a Rust UTF-8 mismatch, after four passing
batches. Pytest then exceeded its 30-second timeout inside `difflib` while
rendering the repetitive-string comparison, obscuring the length diagnostic.
Evidence: `/tmp/issue379-parity-h6.log`. H6 is not falsified, but this result
does not identify a cause or the first differing byte.

The existing assertions now compare into a boolean before asserting, retaining
exact equality while preventing automatic large-string diff rendering. The
alchemist may repeat the H6 packet for at most ten fresh batches, stopping at
the first failure, with the same 60-second bound per batch and no plugin. Save
`/tmp/issue379-parity-h6-diagnostic.log`. This is a diagnostic refinement of
H6, not a new production hypothesis or a relaxation of the payload contract.

## Diagnostic rerun result and bounded sampling extension

All ten diagnostic batches passed (260 cases), leaving the failure intermittent
and unexplained. Evidence: `/tmp/issue379-parity-h6-diagnostic.log`. Because
the earlier reproduction lacked usable data, the alchemist may extend this same
unchanged diagnostic experiment by at most 30 fresh batches, stopping at the
first failure. Retain the same per-batch bound and settings, no plugin, no
artificial load, and no tracked edits. Save
`/tmp/issue379-parity-h6-extended.log`. This extension seeks failure evidence;
passing repetitions cannot be used as evidence that the defect was fixed.

## Extended result and H7 baseline control

All 30 additional batches passed (780 cases). Evidence:
`/tmp/issue379-parity-h6-extended.log`. The current tree still has no proposed
production fix for the observed mismatch.

H7 tests whether the same mismatch predates extraction. The alchemist may
export starting revision `34a59eac4cd13de82c3730d4b569eeaefbb5ee61` into an
ignored `.cache/issue379-parity-baseline` source directory, build its native
extension with the normal pinned toolchain and shared Cargo registry, and place
build output under the original `rust/target/parity-baseline`. Do not install
into or mutate the active virtual environment. Run the baseline Python package
and native extension together from that source directory with the existing
interpreter. Confirm imported paths and native hash. Add only the current
boolean/length diagnostic to the scratch behavioural assertion.

Run the same 26-case behavioural group at most ten times, 60 seconds each,
stopping on first mismatch. Save build and trial logs under
`/tmp/issue379-parity-h7-*`. A matching baseline failure establishes that the
extraction is not necessary for this symptom, but does not discharge the
underlying contract. No reproduction remains inconclusive. No verifier build,
tracked edits, environment installation, or full repository gate is authorized.

## H7 result and H8 syscall localization

The archived baseline reproduced an empty result from the two-stage
`echo -n hello | python uppercase` pipeline, with both stages exiting zero. The
baseline extension SHA-256 was
`bd7fcfbc971460bc9b1d510bf0b5164b1a6818ed5199b73a2b107e896ec927e2`. Evidence:
`/tmp/issue379-parity-h7-build.log` and `/tmp/issue379-parity-h7-trials.log`.
This establishes a pre-extraction native output-loss symptom, not that every
mismatch has the same cause.

A documentation inspection accidentally invoked an additional `make test`
through shell substitution during this period. Its own process group was
terminated after discovery; no incomplete output counts as gate evidence. The
H7 workload therefore was not isolated from other local test activity. That
limits scheduling comparisons, but does not change its archived-code provenance
or the observed empty output from two successful processes.

H8 tests whether asyncio reads upstream bytes that never reach the downstream
pipe during hand-off. Prediction: a failing small-payload trace contains an
upstream `read("hello")` without a corresponding downstream write/splice of
those bytes. If downstream reads `hello` and writes `HELLO`, the proposed
inter-stage loss is falsified for that trace and capture is the next boundary.

The alchemist may run at most 200 current-tree two-stage uppercase pipelines in
one Rust-selected interpreter under `strace -f -yy`, tracing only
`read,write,splice,close,dup,dup2,dup3,fcntl,pipe,pipe2`. Use a scratch runner,
record each trial's stage PIDs, exits, captured value, and native provenance,
and stop at first mismatch. A 300-second outer timeout and 30-second
per-pipeline limit bound this experiment. Do not add delays or replace stream
operations. Syscall tracing is the only instrumentation; no Python hand-off
wrappers. Save `/tmp/issue379-parity-h8.log` and
`/tmp/issue379-parity-h8-syscalls.log`. No tracked edits or full gates.

## H9: a closing transport cannot establish the reader lease

Live CPython 3.13.13 source shows that `_UnixReadPipeTransport.pause_reading`
returns silently when `_closing` is true. EOF handling sets that flag and queues
`_call_connection_lost`, which later closes the pipe even if the
`StreamReader` remains reachable. Cuprum currently interprets that silent
return as successful hand-off. This is a specific lifetime-contract gap; its
relationship to the observed output mismatch is not established.

After H8 stops, the alchemist may execute one Unix-only real-transport case:
create a real pipe and attach its reader via `loop.connect_read_pipe` and
`StreamReaderProtocol`; close the writer; synchronously invoke the transport's
actual `_read_ready` once to observe real EOF and queue connection loss. Before
yielding, record `is_closing`, descriptor validity, and Cuprum's actual
`_pause_reader_transport` result. Yield once for queued callbacks, retain the
reader object, then record descriptor validity again. Do not call native Rust
with a released descriptor. Close only owned test resources, guarding against
double close. Bound the experiment to ten seconds and save
`/tmp/issue379-reader-lease-h9.log`. Prediction: hand-off is accepted while the
descriptor is then closed despite object retention. A rejected hand-off or a
descriptor that remains open falsifies that prediction for this case. No
tracked edits, substitute transport implementation, or full gates.

## H9 result and regression authorization

H9 observed an open, closing transport with `may_hand_off=True`; after one
yield its descriptor was closed despite retaining the reader object. Evidence:
`/tmp/issue379-reader-lease-h9.log`. The hypothesis is not falsified. The
planner added `test_pipeline_streams_closing_reader.py`, which uses the real
EOF protocol callback rather than manually invoking private transport methods.
The alchemist may run only this test once before the fix, capturing
`/tmp/issue379-reader-lease-before.log`. The expected failure is acceptance of
the closing descriptor. Stop for the planner's fix; no full gates or edits.

The regression failed as predicted: a real closed pipe was accompanied by
`may_hand_off=True`. Evidence: `/tmp/issue379-reader-lease-before.log`. The
pause helper now rejects `is_closing()` transports with the existing
`READER_PAUSE_FAILED` decline, leaving buffered bytes for the Python fallback.
The alchemist may rerun that one test once after the fix, recording
`/tmp/issue379-reader-lease-after.log`; no broader testing is authorized here.

The focused regression passed after the guard: one test passed, preserving the
buffered prefix and refusing the invalid native borrow. Full gates may resume.
This fixes the demonstrated lifetime gap; the historical payload mismatches
have not been traced to that gap and must not be described as proven fixed.

H8 completed 200 traced small pipelines without a mismatch. The trace confirms
valid transfer on its passing examples only; it did not localize a failing
execution. Its first scratch launch had a runner indexing error, repaired
without tracked changes before the bounded traced run.
