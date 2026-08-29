# Debugging plan: Rust pump timeout reconciliation hang

## Problem statement

`make test` timed out in
`cuprum/unittests/test_pipeline_timeouts.py::test_zero_timeout_reconciles_pipe_tasks`
after `make develop` installed the debug Rust extension. The focused Rust
pipeline, descriptor-lifecycle, and debug-wheel regressions pass. Determine
whether this is a production descriptor-lifetime regression or an existing
test assumption that only holds for the asyncio-based Python pump.

## Context summary

The test deliberately replaces `_terminate_timed_out_stages` with a no-op. Its
large first stage can therefore remain blocked writing to a pipe while the
second stage sleeps. The Rust pump performs blocking I/O on an executor thread;
on coroutine cancellation, it shields the native future rather than cancelling
that thread. The Python pump has different cancellation mechanics.

## Error artefacts

- Full-suite gate log:
  `/tmp/test-3b15f27d-3626-416f-874b-8eb1537591c3-issue-279-current.out`
- Result: 1305 passed, 1 skipped, 1 timed out after 214.8 seconds.

## Evidence gaps

The failure has not yet been reproduced in a focused process while explicitly
selecting either backend. That comparison is needed before changing source or
test code.

## Hypotheses

### H1: the test depends on Python-pump cancellation semantics

**Prediction:** The focused test passes with `CUPRUM_STREAM_BACKEND=python`
and times out with `CUPRUM_STREAM_BACKEND=rust`. This indicates its stubbed
termination deliberately leaves the blocking Rust worker unable to settle, not
that the descriptor handoff races.

**Falsification plan:** In separate processes, run the single test once with
each explicit backend. Do not modify tracked files. The Python run must finish
within its ordinary test timeout; then run the Rust case with the same command.

### H2: backend selection cache or prior-suite state causes the hang

**Prediction:** A fresh focused Rust process may pass, but repeated
zero-deadline Rust runs occasionally time out. The boundary race is whether the
pump task reaches native executor submission before cancellation; Python's
asynchronous pump can always be cancelled, whereas a submitted native blocking
worker cannot settle while this test suppresses stage termination.

**Falsification plan:** Repeat the forced-Rust focused test in fresh processes
enough times to catch the scheduling variation. If every run passes, compare it
with the immediately preceding full-suite segment before treating the timeout
as an environmental flake.

### H3: the ownership fix leaves a Rust pump blocked independently of the test's termination stub

**Prediction:** A normal forced-Rust timeout scenario also hangs or fails to
close output.

**Falsification plan:** If H1 and H2 are false, re-run the existing forced-Rust
timeout regression without stubbing termination and inspect its worker and
descriptor lifecycle.

## Recommended experiment order

1. Delegate H1's two isolated focused runs to an alchemist.
2. H1 is falsified: both explicit Python and explicit Rust focused processes
   pass (0.14 s and 3.15 s respectively). Investigate H2, then H3 before
   proposing a fix.
3. H2 is falsified at the available sample size: five further fresh,
   forced-Rust processes pass in 0.03–0.08 s. The full-suite occurrence is not
   presently reproducible as a backend, zero-deadline, or simple
   fresh-process scheduling defect.
4. H3 is not supported by the completed focused Rust pipeline and debug-wheel
   timeout regressions, both of which exercise normal stage termination.
   Re-run the failed deterministic gate before considering a production change.

## Termination conditions

Stop once one hypothesis explains the divergent backend behaviour with a
reproducible focused result and the resulting minimal fix or no-change decision
is covered by the applicable deterministic gates.

## Notes

The Rust reader remains borrowed. This investigation must not alter its
ownership contract.

## Experiment record

On 2026-08-30, isolated executions of
`test_zero_timeout_reconciles_pipe_tasks` passed with both
`CUPRUM_STREAM_BACKEND=python` and `CUPRUM_STREAM_BACKEND=rust`. H1 is
therefore falsified: merely selecting the Rust pump does not reproduce the
full-suite timeout.

Review of the failure stack shows that pytest timed out during `asyncio.run()`
shutdown, after `pipeline.run()` had returned `TimeoutExpired`. That is
consistent with a submitted native worker still blocking while the test has
intentionally disabled its normal stage-termination escape hatch. H2 now
tests whether zero-deadline scheduling makes that state intermittent.

Five additional fresh forced-Rust processes passed in 0.03–0.08 seconds, so H2
is falsified at this sample size. Existing forced-Rust pipeline and debug-wheel
timeout regressions with ordinary termination already pass, which does not
support H3. The remaining action is a clean deterministic-gate retry; no source
change is justified by the non-reproducible timeout alone.
