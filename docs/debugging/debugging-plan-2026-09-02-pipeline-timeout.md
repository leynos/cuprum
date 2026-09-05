# Debugging plan: pipeline timeout gate failure

**Generated**: 2026-09-02 **Issue ID**: post-V3/V4 deterministic-gate
precondition **Severity**: medium **Falsification sub-agent**: alchemist
**Planning agent boundary**: This document was prepared by the planning agent.
Falsification must be executed by the named sub-agent, not by the planning
agent.

## Problem statement

The pre-correction full test gate failed only
`test_zero_timeout_reconciles_pipe_tasks` after 30 seconds in the selector
poll, although this test is expected to reap its deliberately live child
processes before `asyncio.run` closes. The V3/V4 correction has no production
timeout changes. The gate must be made reproducibly green before a CodeRabbit
review can be requested.

## Context summary

| Aspect              | Details                                                                        |
| ------------------- | ------------------------------------------------------------------------------ |
| First observed      | 2026-09-02, pre-correction full `make test` run                                |
| Reproduction rate   | one failure in one full run; focused V3/V4 tests pass                          |
| Affected components | `test_pipeline_timeouts`, pipeline teardown, subprocess waiters                |
| Recent changes      | Rebase preserved the test cleanup from `03a81c09`; V3/V4 only alter tests/docs |

### Error artefacts

```plaintext
FAILED cuprum/unittests/test_pipeline_timeouts.py::test_zero_timeout_reconciles_pipe_tasks
Failed: Timeout (>30.0s) from pytest-timeout
============ 1 failed, 1346 passed, 1 skipped in 134.29s ============
```

### Information gaps

The first run did not retain the timed-out test's local process identifiers or
its exact predecessor ordering. It is unknown whether the test fails in
isolation or only after preceding tests.

______________________________________________________________________

## Hypotheses

### H1: The test is order-dependent

**Claim**: an earlier unit test leaves pipeline state or child processes that
make this test's `asyncio.run` shutdown wait for 30 seconds.

**Plausibility**: Medium — the failure occurred during the full unit batch, not
the focused V3/V4 run.

**Prediction**: the test passes alone but fails when preceded by its immediate
neighbourhood from the full batch.

#### H1 falsification plan

| Step | Action                                                      | Expected Negative Result                          |
| ---- | ----------------------------------------------------------- | ------------------------------------------------- |
| 1    | Run the test alone twice using the project pytest command.  | A failure alone disproves order dependence.       |
| 2    | Run the immediately preceding timeout tests plus this test. | A pass disproves a local predecessor interaction. |

**Tooling**: `uv run pytest -v` against explicit test node ids.

**Confidence on falsification**: High for an isolated or local-order cause; the
test suite is serial.

______________________________________________________________________

### H2: The no-termination stub does not always capture the spawned processes

**Claim**: the immediate timeout can unwind before the patched termination
function records the processes, so the cleanup call receives an empty tuple and
the event loop retains the 30-second subprocess waiters.

**Plausibility**: Medium — the test intentionally alters the normal lifecycle
and depends on its patched seam being reached.

**Prediction**: a temporary assertion immediately before
`_terminate_all_shielded` finds an empty `timed_out_processes` tuple on a
failing path.

#### H2 falsification plan

| Step | Action                                                                | Expected Negative Result                                   |
| ---- | --------------------------------------------------------------------- | ---------------------------------------------------------- |
| 1    | Add a temporary assertion that the tuple has both pipeline processes. | It always contains two processes, disproving this claim.   |
| 2    | If empty, trace whether the mock was invoked before `TimeoutExpired`. | The mock invocation disproves an early-unwind explanation. |

**Tooling**: a minimal temporary test-only assertion; revert it immediately.

**Confidence on falsification**: High because it observes the exact cleanup
input without altering production timing.

______________________________________________________________________

### H3: Cleanup is correct but the child has not started when termination runs

**Claim**: process startup races with the zero timeout, causing the normal
termination helper to return before the subprocess transport has a waiter that
can be reaped.

**Plausibility**: Low — the production helper is awaited and the test's final
PID kill is intended as a backstop.

**Prediction**: instrumentation shows both recorded processes have no PID or
remain alive after `_terminate_all_shielded` completes.

#### H3 falsification plan

| Step | Action                                                                     | Expected Negative Result                       |
| ---- | -------------------------------------------------------------------------- | ---------------------------------------------- |
| 1    | Temporarily assert that both captured processes have exited after cleanup. | Both have exited, disproving the startup race. |

**Tooling**: a temporary test-only assertion; revert it immediately.

**Confidence on falsification**: Medium; an isolated failure would still need
the H1 ordering experiment.

______________________________________________________________________

## Recommended execution order

1. **H1** — cheapest and distinguishes an order interaction from this test.
2. **H2** — directly tests the cleanup premise changed by the mock.
3. **H3** — only if H2 is falsified and the test fails alone.

## Termination criteria

- **Root cause identified**: one hypothesis survives while the others are
  falsified by its stated negative result.
- **Escalation trigger**: all three hypotheses are falsified, or fixing the
  issue requires a production interface/lifecycle change beyond the ExecPlan.

## Notes for executing agent

Do not run repository-wide gates and do not edit production code. Report each
falsification verdict with the command, exit status, and whether the temporary
instrumentation was reverted. The current V3/V4 correction must remain intact.
