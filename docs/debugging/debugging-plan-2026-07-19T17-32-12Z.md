# Debugging plan: pipeline benchmark ratchet regression

**Generated**: 2026-07-19T17:32:12Z
**Issue ID**: PR #157 / issue #116
**Severity**: Medium
**Falsification sub-agent**: alchemist
**Planning agent boundary**: This document was prepared by the planning agent.
Falsification must be executed by the named sub-agent, not by the planning
agent.

## Problem statement

The CI pipeline benchmark ratchet reports a 39.5% relative regression against
its 30% threshold. Seven of eight candidate scenarios cluster near 324–353 ms,
while the Rust backend's 1 KiB, two-stage, no-line-callback scenario reports
417.9 ms with a wide 34.4 ms standard deviation. The branch splits
`cuprum/context.py` into a package but does not directly modify the benchmark,
backend dispatcher, Python stream pump, or Rust stream pump. The investigation
must determine whether the outlying ratio is reproducible and, if so, locate
the indirect cost before changing production code.

## Context summary

*Table 1. Benchmark context and affected components.*

| Aspect | Details |
| --- | --- |
| First observed | PR #157 CI output supplied on 2026-07-19 |
| Reproduction rate | One reported CI ratchet run; three hyperfine samples per scenario |
| Affected components | Pipeline benchmark worker, Rust/Python backend ratio, CI ratchet |
| Recent changes | Context module split; main also contains stream-drain and Rust-availability refactors |

### Error artefacts

```plaintext
Rust 1 KiB, two-stage, no callbacks: 417.9 ms ± 34.4 ms
Python equivalent: 324.4 ms ± 2.5 ms
benchmark ratchet failed: worst_regression_ratio=0.395037,
max_regression=0.300000
```

### Information gaps

The baseline artefact and per-scenario ratio report were not included in the
supplied log. Memtrace is not exposed in this session, so temporal and blast
radius evidence must come from Git and Leta. CI runner contention cannot be
recreated exactly on the local host.

---

## Hypotheses

### H1: the reported regression is a noisy outlier

**Claim**: The Rust 1 KiB no-callback result is not reproducibly slower; three
samples are insufficient to distinguish runner interference from a real
regression.

**Plausibility**: High — the failing scenario has much higher variance than
the Python pair and the other Rust scenarios, while the branch does not change
stream backend code.

**Prediction**: Repeated paired runs with more samples and alternating order
will place the Rust/Python ratio within the ratchet threshold most of the time.

#### H1 falsification plan

*Table 2. H1 falsification plan — reproducing the reported outlier.*

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

#### H2 falsification plan

*Table 3. H2 falsification plan — isolating Rust backend initialization cost.*

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Measure import/backend-resolution startup separately from the 20-iteration loop | Startup is negligible relative to the observed roughly 94 ms gap |
| 2 | Compare one worker process with and without an unmeasured pipeline warm-up | Warm-up does not materially change the Rust/Python ratio |

**Tooling**: Existing worker, shell timing or a temporary external harness;
do not edit production code during falsification.

**Confidence on falsification**: Medium-high.

---

### H3: the context package split adds indirect per-iteration overhead

**Claim**: Import plumbing or context access introduced by the package split
adds measurable work to each pipeline iteration, with greater impact on the
Rust dispatch path.

**Plausibility**: Low — `scoped(ScopeConfig(...))` surrounds the whole loop,
and both backends traverse the same command/context path.

**Prediction**: A paired checkout comparison between `origin/main` and this
branch shows the branch slower only when the context split is present, and a
profile attributes the delta to context symbols.

#### H3 falsification plan

*Table 4. H3 falsification plan — testing context-split per-iteration overhead.*

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Compare the same benchmark commands on `origin/main` and the branch in equivalent built environments | No repeatable branch-only slowdown appears |
| 2 | Profile a reproducibly slow run and inspect context-related frames | Context symbols do not account for a material share of elapsed time |

**Tooling**: Existing worktree or temporary Git worktree, hyperfine, available
profiling harnesses.

**Confidence on falsification**: High if paired builds use the same toolchain
and run order.

---

### H4: the Rust pump falls back or takes a slower descriptor path

**Claim**: The forced Rust backend is selected, but descriptor extraction,
buffer draining, or splice support causes repeated fallback or a slower path
for the failing scenario.

**Plausibility**: Medium — backend selection is cached, but the dispatch still
tests descriptor suitability on each inter-stage stream.

**Prediction**: Existing pathway observability or a non-invasive trace shows
the failing scenario choosing a different path from the other Rust scenarios.

#### H4 falsification plan

*Table 5. H4 falsification plan — checking for a slower Rust descriptor path.*

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Use existing backend/pathway tests or instrumentation to confirm native pump selection and fallback counts | The native path is selected consistently with no exceptional fallback |
| 2 | Compare system-call profiles for the failing and neighbouring Rust scenarios | Their pump system-call patterns and counts are equivalent after payload scaling |

**Tooling**: Existing pathway tests, logging/profile harness, `strace` if
needed.

**Confidence on falsification**: Medium.

---

## Recommended execution order

1. **H1** — cheapest and most decisive given the reported variance.
2. **H2** — isolates fixed startup cost without production edits.
3. **H4** — checks whether the requested Rust backend actually takes its
   intended path.
4. **H3** — paired-branch profiling is more expensive and least plausible.

## Investigation outcome

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

### Evidence recorded

The figures below come from two distinct sources that must not be conflated:

- **Investigation-reported figures** (Table 6) were produced by the
  falsification runs summarized above. Their raw per-sample timings were *not*
  retained, and the exact CI run they were read from cannot now be pinned
  down: the failing attempt passed on re-run (so that run's final status is
  success, not failure), and this branch was later rebased, rewriting the
  investigated commit SHAs. They are recorded here as reported by the
  investigation, not re-derived.
- **Retained CI evidence** (Tables 7–8) is a real, still-downloadable
  `benchmark-ratchet` artefact from a *sibling* failure on the same pull
  request. It is not the run that produced the 0.395 headline regression, but
  it exercises the identical pre-remediation configuration and independently
  corroborates the diagnosis.

*Table 6. Investigation-reported figures (raw per-sample timings not
retained).*

| Measurement | Value |
| --- | --- |
| CI ratio, first (failing) attempt | 1.288 |
| CI ratio, passing re-run (same commit) | 1.133 |
| Local ten-run ratio, Python-first order | 1.005 |
| Local ten-run ratio, Rust-first order | 0.971 |
| Order-dependent shift within the Python sample block | ~30 ms |
| Backend-resolution delta (Python vs Rust) | ~1 ms |
| First-run pipeline timing delta | ~0.2 ms |
| Profile attribution to `current_context()`/`resolve_env()` | <0.2 ms |
| Native-backend selections vs Python fallbacks (20 iterations) | 20 / 0 |

#### Retained CI artefact (corroborating sibling failure)

The `benchmark-ratchet` artefact from the PR #157 CI failure on commit
`d73f8be` (workflow run `29641966622`, 2026-07-18, retained until
2026-10-16) holds the plan, the ratchet report, and the hyperfine
`--export-json` output with per-run timings. It used the immediate
predecessor profile `pipeline-worker-release-ratio-v2`, three hyperfine runs
per command, and the non-adjacent ordering (all four Python commands, then all
four Rust commands) that the remediation replaces.

- Run: <https://github.com/leynos/cuprum/actions/runs/29641966622>
- Ratchet verdict: failed, `worst_regression_ratio = 0.520` on
  `rust-small-single-cb` (`candidate_ratio 1.141` vs `baseline_ratio 0.751`);
  four Rust scenarios compared.

**Exact commands.** hyperfine wrapped eight worker commands:

```plaintext
hyperfine --export-json <throughput.json> --warmup 1 --runs 3 <command...>
```

Each `<command>` forced the backend through an environment prefix and ran the
worker, for example:

```plaintext
CUPRUM_STREAM_BACKEND=rust .venv/bin/python3 benchmarks/pipeline_worker.py \
  --payload-bytes 1024 --stages 2 --iterations 20
CUPRUM_STREAM_BACKEND=rust .venv/bin/python3 benchmarks/pipeline_worker.py \
  --payload-bytes 1024 --stages 2 --iterations 20 --line-callbacks
```

The full set covers `{python, rust} × {1024, 65536} bytes × {no-cb, cb}` at
`--stages 2` and `--iterations 20`. These commands come from the retained v2
plan and filter (`pipeline_throughput.py --smoke --dry-run` and
`ci_benchmark_ratchet_profile.py` as they stood for that run); the current CI
ratchet raises `--runs` to 10 and interleaves each Python/Rust pair adjacently.

*Table 7. Per-run hyperfine samples from the retained artefact (seconds; three
runs per command).*

| Backend | Payload | Callbacks | Run 1 | Run 2 | Run 3 | Mean | Std dev |
| --- | --- | --- | --- | --- | --- | --- | --- |
| python | 1 KiB | no | 0.4015 | 0.4101 | 0.3795 | 0.3971 | 0.0158 |
| python | 1 KiB | yes | 0.3657 | 0.3689 | 0.3653 | 0.3666 | 0.0020 |
| python | 64 KiB | no | 0.3742 | 0.3705 | 0.3686 | 0.3711 | 0.0028 |
| python | 64 KiB | yes | 0.3867 | 0.3699 | 0.3811 | 0.3792 | 0.0086 |
| rust | 1 KiB | no | 0.3626 | 0.4009 | 0.4136 | 0.3924 | 0.0266 |
| rust | 1 KiB | yes | 0.4185 | 0.4401 | 0.3969 | 0.4185 | 0.0216 |
| rust | 64 KiB | no | 0.3780 | 0.3737 | 0.3671 | 0.3729 | 0.0055 |
| rust | 64 KiB | yes | 0.3676 | 0.3738 | 0.3714 | 0.3709 | 0.0032 |

The two Rust 1 KiB commands carry by far the highest variance (std dev 26.6 ms
and 21.6 ms) while every Python command stays under 16 ms, matching the plan's
account of a high-variance small-payload Rust outlier driven by host load
rather than a stream-code regression.

**Command sequence.** The retained plan lists all four Python commands first
and all four Rust commands second, so the Python and Rust blocks are measured
minutes apart — the ordering artefact the remediation targets. The current
`select_ci_ratchet_scenarios` instead sorts by
`(payload_bytes, with_line_callbacks, python-before-rust)`, interleaving each
Python/Rust pair. The local Python-first/Rust-first A/B reordering behind
Table 6 was manual and its exact invocation order was not retained.

*Table 8. Toolchain and execution environment.*

| Aspect | Value | Source |
| --- | --- | --- |
| Ratchet runner | `ubicloud-standard-4-ubuntu-2404` (Ubuntu 24.04, 4 vCPU) | `.github/workflows/ci.yml` |
| Python | 3.13 (`actions/setup-python`) | `.github/workflows/ci.yml` |
| Rust (lint/typecheck jobs) | pinned `1.92.0` | `.github/workflows/ci.yml` |
| Rust (ratchet build) | runner default via `maturin develop --release` | `.github/workflows/ci.yml` |
| hyperfine | Ubuntu apt package, version not pinned | `.github/workflows/ci.yml` |
| Retained-artefact commit | `d73f8be` (GitHub runner, paths under `/home/runner/work`) | run `29641966622` |
| Current branch tip | `0cc33db` (CI success) | run `29785825476` |

**Native-backend vs fallback.** Backend selection is forced per command by the
`CUPRUM_STREAM_BACKEND` environment variable (`rust` or `python`) shown above,
so each measured process runs exactly one backend. The plan's "20 native
selections, 0 fallbacks" describes the 20 worker iterations of the Rust
command; the per-iteration observation method was not retained alongside the
plan and is not reproducible from the artefact, which records only aggregate
hyperfine timings.

#### Limitations

- The individual per-sample timings behind Table 6 — both the local ten-run
  ratios and the CI attempt ratios — were not retained; only the aggregates
  are on record.
- The exact CI run that produced the 0.395 headline regression cannot be
  linked: it passed on re-run (final status success) and its commit SHA was
  rewritten by the later rebase. Tables 7–8 link a retained sibling failure
  that corroborates, but does not reproduce, those figures.
- The precise hyperfine version and the ratchet job's Rust build version are
  not captured in the artefact.
- The native-versus-fallback count's per-iteration provenance was not
  retained.

## Termination criteria

- **Root cause identified**: One hypothesis survives repeated falsification
  while the others are eliminated, with a measurable mechanism matching the
  observed regression.
- **Escalation trigger**: All hypotheses are falsified or the result cannot be
reproduced locally and no CI baseline artefact is available; report the
  evidence and recommend a measurement-only mitigation rather than changing
  runtime code.

## Notes for executing agent

Keep the worktree unchanged during falsification. Prefer repeated paired
measurements and report raw samples, ratios, command order, toolchain, and
whether the native backend was used. Do not raise the ratchet threshold or
silence the failure. If a production fix is indicated, return the smallest
candidate change and the test/benchmark needed to prove it.
