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

A pipeline emits its `exit` event for every stage only once the whole run has
settled (`_build_pipeline_stage_results` in `cuprum/_pipeline_results.py`), so
a stalled run never emits one. The upstream-stage condition in the first
hand-off rule is therefore evaluated from that stage's process state in
`/proc`, not from its `exit` event, which would not have arrived in the
situation the rule exists to catch.

`_classify_stall` evaluates the rules in the order below, and every
`HUNG_HANDOFF` rule is tested before the host-starvation fallback. A runnable
child is therefore not evidence in its own right: `HOST_STARVATION` is reported
only when no positive hand-off evidence was captured.

| Evidence at stall                                                                                                        | Verdict                 |
| ------------------------------------------------------------------------------------------------------------------------ | ----------------------- |
| A parent write end for a child's stdin pipe survives while that child waits in `pipe_read` and its upstream stage exited | `HUNG_HANDOFF`: fail    |
| Every tracked child has exited and a parent-owned read end still reports queued output bytes                             | `HUNG_HANDOFF`: fail    |
| Tasks are still pending, children were tracked, and none of them is runnable                                             | `HUNG_HANDOFF`: fail    |
| No rule above matched                                                                                                    | `HOST_STARVATION`: skip |

A zombie and a reaped pid both count as exited, so the fail rules accept
either. The 30-second aggregate deadline reached inside `_monitor_progress` is
only a suite-safety backstop: it triggers the same evidence capture and
classifier, and it does not decide the verdict. The same deadline reached
before an attempt starts is a plain comparison with no classifier at all —
`pre_attempt_backstop_reached` runs before any child exists, so there is no
state to classify and the attempt is skipped unclassified.

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

## Rebase note

The branch was rebased onto `main` at `7db76e6d` ("Add lightweight
group/annotate flags to `RunOutputOptions`"). That commit changes the echo and
presentation-sink layer — the `redirects_echo` gate in `_resolve_stream_sink`,
the sink lifecycle, and the GitHub Actions sink — and leaves the native
hand-off path alone. `ScopeConfig.observe_hooks` and the `start`, `stdout`,
`stderr`, and `exit` `ExecPhase` values this support observes are unchanged, so
the rebase required no code change. The rebased tree was verified against the
`git merge-tree` oracle for the branch.

The Makefile's `PYTEST_TARGETS` names
`tests/test_native_pipeline_hand_off_support.py`,
`tests/test_native_pipeline_liveness.py`,
`tests/test_native_pipeline_stdout_capture.py`, and
`tests/test_process_state_helper.py`, so `make test-python` runs them directly.
The coverage job separately invokes `pytest` with no path targets and so
collects the repository root as well.

A later `make lint` run failed on 15 pylint findings, and only two were real.
The other 13 sat in `benchmarks/` and `cuprum/` files this branch never edits.
They were stale-base drift: the branch was nine commits behind `main`, and
pylint walks the whole working tree rather than the diff, so it judged
already-superseded files. `cuprum/sh.py` and `cuprum/_line_stream.py` — both
flagged — do not exist on `main` at all. A pristine `origin/main` checkout
scores 10.00/10, and the hosted `lint-test` job had passed on this branch
throughout, because it lints the merge ref, which carries `main`'s commits. Two
lessons: a local pylint failure on an unrebased branch may be judging `main`'s
own debt, not the branch's; and a local `--python pypy` had moved from 3.11 to
3.12, which newly parses PEP 695 `type` aliases that the older interpreter
silently skipped, surfacing findings lint had never reported before.

## Two local-only gate traps

Neither of the next two failures is a defect in this branch, and both are worth
recognizing again rather than re-diagnosing.

The release-workflow tests (`test_release_github_steps.py`,
`test_release_reconciliation.py`) fail 13 times locally while the same files
pass in CI. The tests install a fake `gh` on a `PATH` they control, but this
host exports `BASH_ENV=/home/leynos/.lody/bashenv`, which prepends
`/home/leynos/.lody/bin` to `PATH` in every non-interactive bash. That
directory holds Lody's own `gh` wrapper, which runs `git` and so reports
`fatal: not a git repository` from the test's scratch directory. Running
`env -u BASH_ENV make test` gives `3644 passed` with the 13 included; the fix
properly belongs to [#434](https://github.com/leynos/cuprum/issues/434), whose
branch scrubs `BASH_ENV` from these helpers but is not yet merged.

The `typecheck` and `lint` gates failed on the same four lines, for opposite
reasons. `ty` 0.0.74 does not honour a code-specific
`# type: ignore[arg-type]` — a bare `# type: ignore` or a `# ty: ignore[...]`
is honoured, the coded `type:` form is not — while df12-python-lints C9107
requires every suppression to carry an explanation. A suppression that
satisfies one gate therefore fails the other. The resolution was to remove the
suppressions instead of explaining them: the liveness and stdout-capture tests
now build real `ExecEvent` values through small factories, so the tracker is
fed the genuine type its hook receives. The stall module's private
`_LivenessProgress` copy was dropped in favour of the `LivenessProgress` the
liveness module already defines.
