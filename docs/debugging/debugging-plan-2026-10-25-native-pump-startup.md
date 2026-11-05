# Diagnose native pump lifecycle startup ordering

Generated: 2026-10-25. Scope: post-rebase validation for PR #619. Falsification
sub-agent: alchemist. The planning agent prepares this plan; the alchemist runs
only the bounded experiment below.

## Observed failure

The full gate logged 4,256 passes, 96 skips, and one failure in
`test_deferred_grace_releases_paused_reader_before_loop_closes`. Its worker
never started, while both pipeline stages reported the configured 50 ms
deadline. The test is intended to examine cleanup after a native worker has
started, not a timeout that prevents startup. Evidence:
`/tmp/pr619-final-test.out`.

## Hypothesis and falsification

H1: the elapsed deadline races native pump startup. Delaying pump dispatch past
50 ms cancels it before the worker can signal readiness, making the lifecycle
assertion fail without exercising the intended cleanup state.

Use a temporary pytest plugin to wrap
`cuprum._pipeline_streams._pump_stream_dispatch` with an asynchronous 100 ms
delay before calling the original. Run only the failing lifecycle test, with
its unchanged 50 ms deadline and five-second worker-start bound. Do not edit
production files, widen deadlines, or run the full gate. The plugin must
restore its monkeypatch and the experiment must remain under the existing
30-second pytest timeout.

Prediction: the delayed test fails with an unset worker-start event and the
same deadline warnings. If the worker starts and the test passes despite the
inserted delay, H1 is falsified. Different failures are inconclusive. Record
the exact result and log, then stop.

## Follow-up boundary

If H1 survives, synchronize the lifecycle fixture so its short elapsed deadline
is armed only after native-worker readiness. Preserve the real production wait,
timeout mapping, cleanup grace, retained-descriptor checks, and eventual
callback cleanup. Add a delayed-start case that fails before the fixture
correction and passes afterwards. Any production change remains outside this
repair and requires separate approval of the parity plan.

## Experiment result

H1 was not falsified. The delayed dispatch entered its 100 ms wait and was
cancelled before calling the original dispatcher. The worker-start assertion
then failed with the same 50 ms elapsed-deadline warnings. The focused run
failed in 5.24 seconds. This demonstrates the race mechanism without proving it
was the only possible cause of the original gate failure. The log is
`/tmp/pr619-native-startup-red.out`.

The fixture repair applies the readiness barrier to both synchronous pipeline
lifecycle tests and retains the 100 ms delayed start as a named regression. The
public production timeout implementation is unchanged. Full post-rebase
validation passed, including all five lifecycle cases in the complete Python
suite. The [ExecPlan](../execplans/6-1-2-boundary-proptest-parity.md) records
the final gate evidence and the shared-compiler timeout followed by a
successful retry.

The first focused run after adding the readiness barrier passed the three
immediate-start cases but failed both delayed-start cases. The upstream fixture
exited immediately after writing, allowing its reader to close before the
delayed native handoff. The fixture therefore keeps both child stages alive
until the real timeout, with the upstream payload flushed explicitly. This
preserves an open pipe for the ownership scenario instead of depending on
subprocess exit scheduling. Evidence: `/tmp/pr619-native-startup-green.out`
(two failures, three passes).

After the fixture lifetime correction, all five lifecycle cases passed in 0.72
seconds, including both delayed-start cases. Evidence:
`/tmp/pr619-native-startup-green2.out` and its zero exit-status sidecar.
