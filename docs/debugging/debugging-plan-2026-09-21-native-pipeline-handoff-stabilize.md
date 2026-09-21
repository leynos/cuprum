# Debugging plan: stabilize native pipeline hand-off

- Generated: 2026-09-21.
- Issue: [#425](https://github.com/leynos/cuprum/issues/425).

## Problem statement

`test_auto_backend_repeated_native_pipeline_hand_off` repeats a real two-stage
native pipeline 16 times to expose an intermittent hand-off defect. An attempt
that stops making progress must produce an evidence-based result. Host load
must not turn a progressing pipeline into a test failure, and a missed close
must not be concealed by a larger wall-clock timeout.

## Candidate explanations

### Host starvation

The host may deschedule the pipeline under oversubscription. A child that is
still runnable or in uninterruptible I/O state can be awaiting host service;
that state makes the deadline a non-verdict and the test skips.

### Missed native file-descriptor hand-off

The native path can leave the parent holding a write end for the downstream
child's stdin pipe after the upstream writer has exited. The child then sleeps
in `pipe_read` because EOF cannot arrive. This is a stalled hand-off and fails
the test.

## Discriminator

The test observes `start`, `stdout`, `stderr`, and `exit` events through the
public `ScopeConfig.observe_hooks` seam. Each event refreshes a progress clock.
When no event arrives for the liveness interval, the test captures evidence
before cancelling the pipeline:

- Child process state and exit status from `/proc/<pid>/stat`.
- Child kernel wait channel from `/proc/<pid>/wchan`.
- Readable bytes on each child pipe via `FIONREAD`.
- Parent descriptors that still write to a child's stdin pipe.
- Names of the live asyncio tasks awaiting completion.

| Evidence at stall                                                                                                                                          | Verdict                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| Parent writer remains open while a child waits in `pipe_read`; exited children leave unread pipe output; or pending tasks outlive every non-runnable child | `HUNG_HANDOFF`: fail    |
| A present, non-zombie child is `R` or `D`                                                                                                                  | `HOST_STARVATION`: skip |

The 30-second aggregate deadline is only a suite-safety backstop. It triggers
the same evidence capture and classifier; it does not decide the verdict.

## Evidence already obtained

On commit `d7cc4435`, a hung downstream child was sleeping in `pipe_read` while
the Cuprum parent held the sole remaining write end of its stdin pipe. In a
separate 400-attempt probe, eight hangs occurred at load averages 2.89–3.26 on
six cores; the slowest healthy attempt took 0.018 seconds. The observed state
therefore supports a missed close, not host starvation.

`fd_delta` and `thread_delta` are external telemetry that characterize a
failure. They are not repository metrics and do not determine the verdict.

## Related work

- [#321](https://github.com/leynos/cuprum/pull/321) introduced the repeated
  Linux hand-off regression detector.
- [#364](https://github.com/leynos/cuprum/pull/364) extracted the stress-test
  support and raised its former wall-clock deadline.
- [#366](https://github.com/leynos/cuprum/issues/366) established the precedent
  of reproducing a parallel race and retaining the assertion after identifying
  its cause.
