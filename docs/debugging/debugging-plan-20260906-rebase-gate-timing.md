# Debugging Plan: Rebase gate timing failures

**Generated**: 2026-09-06 **Issue ID**: PR #352 rebase validation **Severity**:
Medium **Falsification sub-agent**: alchemist **Planning agent boundary**: This
document was prepared by the planning agent. Falsification must be executed by
the named sub-agent, not by the planning agent.

## Problem Statement

The full post-rebase gate run passed formatting and type checking, but failed
two generated tests and one spelling-render property while the shared host was
running several other Cargo-heavy jobs. The worker concurrency property
exceeded pytest's 30-second whole-test timeout, and the two properties exceeded
Hypothesis's 200 ms per-example deadline before passing on replay. The expected
behaviour is deterministic validation that still detects worker stalls and
rendering defects without failing solely because the host is temporarily busy.

## Context Summary

| Aspect              | Details                                                          |
| ------------------- | ---------------------------------------------------------------- |
| First observed      | Post-rebase validation on 2026-09-06                             |
| Reproduction rate   | One full run; each affected failure was timing-sensitive         |
| Affected components | Worker concurrency, timeout telemetry, and typos rendering tests |
| Recent changes      | Rebase onto `origin/main`; no behavioural change to these tests  |

### Error Artefacts

```plaintext
test_generated_concurrent_workers_complete: Timeout (>30.0s) from pytest-timeout
test_timeout_expiry_reports_agree_across_channels: 262.99ms > 200ms deadline
test_render_is_canonical_and_scopes_markdown_patterns: 280.83ms > 200ms deadline
```

### Information Gaps

- The failed run shared CPU and Cargo caches with unrelated worktrees.
- The affected selectors have not yet been rerun independently after the load
  subsides.

______________________________________________________________________

## Hypotheses

### H1: Shared-host load produced transient timing failures

**Claim**: The failures were caused by temporary scheduler and cache
contention, not an incorrect worker, telemetry, or renderer result.

**Plausibility**: High — Hypothesis replayed both deadline failures
successfully, and the worker failure was pytest's outer timeout rather than an
assertion.

**Prediction**: Each exact selector passes when run alone without concurrent
full-repository gates.

#### H1 Falsification Plan

| Step | Action                                                         | Expected Negative Result                                           |
| ---- | -------------------------------------------------------------- | ------------------------------------------------------------------ |
| 1    | Run each failed selector sequentially with `uv run pytest -q`. | A reproducible assertion, worker failure, or timeout disproves H1. |

**Tooling**: `uv run pytest -q` for the three exact failing selectors.

**Confidence on falsification**: High for deterministic source regressions;
inconclusive for load sensitivity if another unrelated resource spike occurs.

**Result**: Inconclusive for the worker selector because its first isolated
capture did not complete, but not-falsified for the timeout telemetry and typos
rendering selectors, which both passed without a semantic failure.

______________________________________________________________________

### H2: The properties have inappropriate timing policy for their work

**Claim**: The generated worker and rendering properties test correctness
rather than latency, but inherit timing limits that are too small for their
bounded work under supported shared-host execution.

**Plausibility**: Medium — the worker test already disables Hypothesis's
deadline but remains subject to pytest's 30-second whole-test timeout, and the
rendering property retains Hypothesis's default deadline.

**Prediction**: The generated worker property passes with a bounded 90-second
test timeout, while its internal 15-second worker-stall assertion remains in
force.

#### H2 Falsification Plan

| Step | Action                                       | Expected Negative Result                                |
| ---- | -------------------------------------------- | ------------------------------------------------------- |
| 1    | Run the worker selector with `--timeout=90`. | A semantic failure or a 90-second overrun disproves H2. |

**Tooling**: The exact worker selector and pytest's `--timeout=90` option.

**Confidence on falsification**: High for ruling out a deterministic worker
failure; a passing run establishes that the outer 30-second timeout, rather
than the worker assertion, caused the observed failure.

**Result**: Not falsified. The worker selector passed with `--timeout=90` in
31.1 seconds and did not trigger its internal 15-second worker-stall assertion.

______________________________________________________________________

## Recommended Execution Order

1. **H1** — the exact isolated selectors are the cheapest decisive test for a
   deterministic source regression. The worker selector was inconclusive; the
   telemetry and spelling selectors passed.
2. **H2** — use a bounded outer timeout to distinguish worker correctness from
   an undersized test-time budget.

## Termination Criteria

- **Root cause identified**: A selector reproduces a semantic failure, or all
  selectors pass alone and their timing policy is shown to be incompatible with
  their correctness-only purpose.
- **Escalation trigger**: Repeat isolated selectors still time out while the
  host is otherwise idle.

## Notes for Executing Agent

Run no full repository gates and make no edits. Report one verdict for H1:
falsified, not-falsified, or inconclusive. Include each selector's result and
whether the observed outcome is an assertion failure or a timing failure.

## Resolution

Retain the worker helper's 15-second internal stall bound. Give the generated
worker property a 90-second pytest timeout because it executes 20 concurrent
examples. The root test configuration disables Hypothesis's host-sensitive
per-example deadline for correctness properties; pytest-timeout continues to
bound every test. The final gate run validates these policy changes under the
normal repository configuration.
