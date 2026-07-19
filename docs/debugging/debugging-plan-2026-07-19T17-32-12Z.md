# Debugging Plan: Pipeline benchmark ratchet regression

**Generated**: 2026-07-19T17:32:12Z
**Issue ID**: PR #157 / issue #116
**Severity**: Medium
**Falsification sub-agent**: alchemist
**Planning agent boundary**: This document was prepared by the planning agent.
Falsification must be executed by the named sub-agent, not by the planning
agent.

## Problem Statement

The CI pipeline benchmark ratchet reports a 39.5% relative regression against
its 30% threshold. Seven of eight candidate scenarios cluster near 324–353 ms,
while the Rust backend's 1 KiB, two-stage, no-line-callback scenario reports
417.9 ms with a wide 34.4 ms standard deviation. The branch splits
`cuprum/context.py` into a package but does not directly modify the benchmark,
backend dispatcher, Python stream pump, or Rust stream pump. The investigation
must determine whether the outlying ratio is reproducible and, if so, locate
the indirect cost before changing production code.

## Context Summary

| Aspect | Details |
| --- | --- |
| First observed | PR #157 CI output supplied on 2026-07-19 |
| Reproduction rate | One reported CI ratchet run; three hyperfine samples per scenario |
| Affected components | Pipeline benchmark worker, Rust/Python backend ratio, CI ratchet |
| Recent changes | Context module split; main also contains stream-drain and Rust-availability refactors |

### Error Artefacts

```plaintext
Rust 1 KiB, two-stage, no callbacks: 417.9 ms ± 34.4 ms
Python equivalent: 324.4 ms ± 2.5 ms
benchmark ratchet failed: worst_regression_ratio=0.395037,
max_regression=0.300000
```

### Information Gaps

The baseline artefact and per-scenario ratio report were not included in the
supplied log. Memtrace is not exposed in this session, so temporal and blast
radius evidence must come from Git and Leta. CI runner contention cannot be
recreated exactly on the local host.

---

## Hypotheses

### H1: The reported regression is a noisy outlier

**Claim**: The Rust 1 KiB no-callback result is not reproducibly slower; three
samples are insufficient to distinguish runner interference from a real
regression.

**Plausibility**: High — the failing scenario has much higher variance than
the Python pair and the other Rust scenarios, while the branch does not change
stream backend code.

**Prediction**: Repeated paired runs with more samples and alternating order
will place the Rust/Python ratio within the ratchet threshold most of the time.

#### H1 Falsification Plan

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Run the exact paired 1 KiB commands repeatedly with additional warm-up and at least ten samples | A stable Rust/Python ratio remains above the baseline threshold with low variance |
| 2 | Repeat with reversed command order | The same regression persists independent of ordering |

**Tooling**: Existing benchmark worker and hyperfine; no code changes.

**Confidence on falsification**: High if paired results are stable across
orders.

---

### H2: Rust backend initialization dominates the small-payload scenario

**Claim**: Native extension loading, backend resolution, or first-use executor
setup adds a fixed per-process or per-worker cost that is disproportionately
visible in the 1 KiB scenario.

**Plausibility**: Medium — the failing scenario is the first Rust command in
the matrix and processes are restarted for every hyperfine sample.

**Prediction**: Separating cold and warm invocations, or adding an in-process
warm-up before measurement, removes most of the gap without changing steady
state pipeline work.

#### H2 Falsification Plan

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Measure import/backend-resolution startup separately from the 20-iteration loop | Startup is negligible relative to the observed roughly 94 ms gap |
| 2 | Compare one worker process with and without an unmeasured pipeline warm-up | Warm-up does not materially change the Rust/Python ratio |

**Tooling**: Existing worker, shell timing or a temporary external harness;
do not edit production code during falsification.

**Confidence on falsification**: Medium-high.

---

### H3: The context package split adds indirect per-iteration overhead

**Claim**: Import plumbing or context access introduced by the package split
adds measurable work to each pipeline iteration, with greater impact on the
Rust dispatch path.

**Plausibility**: Low — `scoped(ScopeConfig(...))` surrounds the whole loop,
and both backends traverse the same command/context path.

**Prediction**: A paired checkout comparison between `origin/main` and this
branch shows the branch slower only when the context split is present, and a
profile attributes the delta to context symbols.

#### H3 Falsification Plan

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Compare the same benchmark commands on `origin/main` and the branch in equivalent built environments | No repeatable branch-only slowdown appears |
| 2 | Profile a reproducibly slow run and inspect context-related frames | Context symbols do not account for a material share of elapsed time |

**Tooling**: Existing worktree or temporary Git worktree, hyperfine, available
profiling harnesses.

**Confidence on falsification**: High if paired builds use the same toolchain
and run order.

---

### H4: The Rust pump falls back or takes a slower descriptor path

**Claim**: The forced Rust backend is selected, but descriptor extraction,
buffer draining, or splice support causes repeated fallback or a slower path
for the failing scenario.

**Plausibility**: Medium — backend selection is cached, but the dispatch still
tests descriptor suitability on each inter-stage stream.

**Prediction**: Existing pathway observability or a non-invasive trace shows
the failing scenario choosing a different path from the other Rust scenarios.

#### H4 Falsification Plan

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Use existing backend/pathway tests or instrumentation to confirm native pump selection and fallback counts | The native path is selected consistently with no exceptional fallback |
| 2 | Compare system-call profiles for the failing and neighbouring Rust scenarios | Their pump system-call patterns and counts are equivalent after payload scaling |

**Tooling**: Existing pathway tests, logging/profile harness, `strace` if
needed.

**Confidence on falsification**: Medium.

---

## Recommended Execution Order

1. **H1** — cheapest and most decisive given the reported variance.
2. **H2** — isolates fixed startup cost without production edits.
3. **H4** — checks whether the requested Rust backend actually takes its
   intended path.
4. **H3** — paired-branch profiling is more expensive and least plausible.

## Investigation Outcome

H1 survived falsification; H2, H3, and H4 were falsified. The same commit
failed its first CI attempt with a 1.288 Rust/Python ratio and passed unchanged
with a 1.133 ratio. Local ten-run measurements produced ratios of 1.005 with
Python first and 0.971 with Rust first. Reversing command order also exposed a
roughly 30 ms shift within the Python sample block, confirming time-dependent
host load that three sequential samples cannot cancel.

Backend resolution differed by about 1 ms between Python and Rust, first-run
pipeline timing differed by about 0.2 ms, and the native Rust path was selected
for all 20 observed iterations with no Python fallback. Profiles attributed
less than 0.2 ms to `current_context()` and `resolve_env()` in either backend;
the benchmark and stream implementations are unchanged from `origin/main`.

The remediation therefore changes only the CI measurement protocol: collect
ten samples, order matched Python/Rust commands adjacently, and bump the
benchmark profile version so older baselines are rejected. Production context
and stream code remain unchanged.

Full-suite validation exposed an independent deadlock in three pipe-test
helpers: they fed or drained only one side concurrently, so payloads larger
than the host's pipe capacity could block forever. The helpers now feed and
drain concurrently around the synchronous native pump. This test-only repair
does not alter stream behaviour or the benchmark conclusion.

## Termination Criteria

- **Root cause identified**: One hypothesis survives repeated falsification
  while the others are eliminated, with a measurable mechanism matching the
  observed regression.
- **Escalation trigger**: All hypotheses are falsified or the result cannot be
reproduced locally and no CI baseline artefact is available; report the
  evidence and recommend a measurement-only mitigation rather than changing
  runtime code.

## Notes for Executing Agent

Keep the worktree unchanged during falsification. Prefer repeated paired
measurements and report raw samples, ratios, command order, toolchain, and
whether the native backend was used. Do not raise the ratchet threshold or
silence the failure. If a production fix is indicated, return the smallest
candidate change and the test/benchmark needed to prove it.
