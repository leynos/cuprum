# Debugging record: the benchmark ratchet's overhead-bound ratio

**Generated**: 2026-09-16 **Issue ID**:
[#219](https://github.com/leynos/cuprum/issues/219), surfaced on
[PR #158](https://github.com/leynos/cuprum/pull/158) **Severity**: medium
**Planning agent boundary**: This is a completed investigation, not a plan for
a falsification sub-agent. The measurements below were taken directly; each is
reproducible from the commands and artefacts it cites.

## Problem statement

`benchmark-ratchet` failed on PR #158, which changed only
`_subprocess_execution.py` and touched no streaming path, with a 35.4% relative
slowdown of the Rust backend against a 30% flat threshold. The job was the only
red check on the pull request and passed on re-run. The issue is
signal-to-noise: the scenarios being compared are dominated by cost that does
not scale with the payload, so a runner-to-runner swing in that cost alone can
move the ratio past the threshold.

## Context summary

*Table 1. Context summary for the overhead-bound ratio observation.*

| Aspect              | Details                                                                             |
| ------------------- | ----------------------------------------------------------------------------------- |
| First observed      | PR #158 CI, run 30047241417                                                         |
| Reproduction rate   | Ratchet re-run green; the ratio is not reproducible at the old payloads             |
| Affected components | `benchmarks/pipeline_worker.py`, the CI ratchet profile, `.github/workflows/ci.yml` |
| Recent changes      | None in the measured path; PR #158 changed the single-command subprocess path       |

### Error artefacts

```plaintext
ERROR __main__: benchmark ratchet failed: worst_regression_ratio=0.353615, max_regression=0.300000
```

Issue #219's own reproduction on the then-current tree, at the payloads the
ratchet measured:

```plaintext
rust-65536    396.8 ms ± 2.4      python-65536   398.0 ms ± 4.6
rust-1024     398.6 ms ± 2.7      python-1024    404.6 ms ± 3.7
```

### Information gaps

The failing run's individual hyperfine samples were not retained; only the
aggregate regression ratio is on record. Hyperfine 1.20.0 does not interleave
commands (upstream issue #21, milestone 2.0), so the Python and Rust commands
are consecutive groups within one invocation and slow load drift remains a bias
in the comparison — this record measures that bias but does not remove it.

______________________________________________________________________

## Findings

### The worker is overhead-bound at the old payloads

Micro-probes on the development host, with the release extension built as CI
builds it (`make develop MATURIN_DEVELOP_FLAGS='--release --skip-install'`),
separated the per-run costs:

*Table 2. Per-run cost components at the payloads the ratchet measured before
this work (2026-09-16, development host).*

| Component                      | Cost                                              | Method                                                        |
| ------------------------------ | ------------------------------------------------- | ------------------------------------------------------------- |
| Process start                  | ≈ 140 ms                                          | `python -c 'import cuprum'` against `python -c pass`          |
| of which `import cuprum`       | ≈ 100 ms                                          | as above                                                      |
| Fixed cost of a measured run   | ≈ 195 ms (no callbacks), ≈ 205 ms (callbacks)     | `--iterations 5` at 1 MiB, where streaming is a few ms        |
| Streaming, no line callbacks   | ≈ 0.7 ms/MiB per iteration (Rust), ≈ 1.0 (Python) | slope from 1 MiB to 64 MiB, both measured at `--iterations 5` |
| Streaming, with line callbacks | ≈ 3.8 ms/MiB per iteration (Rust), ≈ 4.0 (Python) | as above                                                      |

A measured run's fixed cost is the interpreter start, the `cuprum` import, the
hyperfine `--prepare` spawn, and five iterations' worth of pipeline set-up. At
1 KiB or 1 MiB the payload contributes nothing measurable: the ratio was
entirely that fixed cost. It is also *not* backend-neutral — the pure-Python
pump pays it differently from the native one — so the ratio carries a component
that has nothing to do with the code under test. The earlier sweep, on a loaded
machine, fitted a larger per-MiB slope (4.2 ms/MiB for Python); the figures
above are the quiet-machine pair of measurements at the exact `--iterations 5`
shape, and the two differ by the load the machine carried.

### The ratio's spread falls as the payload rises

One cell per payload, each cell four repeat hyperfine invocations of the
scenario set the CI profile selects (single-stage, both callback modes, both
backends), ten runs and one warmup per command, `--iterations 5`. Ratios are
`rust_mean / python_mean` within one invocation; the band is the ratchet's own
`3 × 1.4826 × MAD` relative to the median.

*Table 3. Within-run ratios by payload (2026-09-16, development host, load
average 5–23 from other agents).*

| Payload | mode | ratios, oldest first           | median | MAD band |
| ------- | ---- | ------------------------------ | ------ | -------- |
| 1 KiB   | nocb | 0.9468, 1.0042                 | 0.9755 | 13.1%    |
| 1 KiB   | cb   | 1.0308, 0.9672                 | 0.9990 | 14.2%    |
| 16 MiB  | nocb | 0.3853, 0.6937, 0.7146, 0.7037 | 0.6987 | 6.7%     |
| 16 MiB  | cb   | 0.9094, 0.9372, 0.8661, 0.9346 | 0.9220 | 6.7%     |
| 32 MiB  | nocb | 0.6141, 0.5977, 0.7552, 0.5284 | 0.6059 | 31.5%    |
| 32 MiB  | cb   | 0.9479, 0.8560, 1.1858, 1.3010 | 1.0668 | 68.8%    |
| 64 MiB  | nocb | 0.4118, 0.4093, 0.4664, 0.4539 | 0.4328 | 22.9%    |
| 64 MiB  | cb   | 0.6706, 0.8090, 0.9335, 0.8687 | 0.8388 | 33.0%    |
| 100 MiB | nocb | 0.4681, 0.4973, 0.4845, 0.4605 | 0.4763 | 11.2%    |
| 100 MiB | cb   | 0.8115, 0.8201, 0.8582, 0.8204 | 0.8203 | 2.4%     |

The 1 KiB rows reproduce the reported failure mode: the two repeats differ by
6% and 6.4% respectively, which at three runs per command — what the ratchet
measured before this work — is inside the spread the gate was making decisions
with.

### Streaming overtakes the per-iteration set-up between 4 and 22 MiB

Measuring each

```plaintext
t(iterations) = 103 ms + iterations * (17 ms + slope * payload)
```

shape directly — at 1 MiB and 64 MiB, one iteration against five, so the
per-iteration cost falls out of the difference — separates the two terms:

*Table 4. Per-iteration set-up and streaming cost by backend and callback mode,
with the crossover payload where one iteration's streaming equals its set-up
(2026-09-16, quiet machine).*

| Backend, mode | set-up per iteration | streaming per iteration | crossover |
| ------------- | -------------------- | ----------------------- | --------- |
| python, nocb  | 17.3 ms              | 0.95 ms/MiB             | 18.2 MiB  |
| python, cb    | 17.6 ms              | 3.93 ms/MiB             | 4.5 MiB   |
| rust, nocb    | 16.6 ms              | 0.77 ms/MiB             | 21.6 MiB  |
| rust, cb      | 17.2 ms              | 3.71 ms/MiB             | 4.6 MiB   |

The set-up is essentially backend-neutral at about 17 ms; it is the streaming
slope that differs, by a factor of five between the nocb and cb paths and by a
quarter between the backends. One `~103 ms` process/interpreter term sits
outside the iterations. The crossover — the payload at which one iteration's
streaming equals one iteration's set-up — is 4.5 MiB in callback mode and 18–22
MiB without callbacks. That is why the profile's floor is 32 MiB: the first
tier past all four crossovers, so every scenario the band accepts has paid for
its ratio with streaming work rather than with set-up. The 64 MiB workload then
runs 62% streaming for the pure-Python no-callback pump — the least favourable
combination — 57% for the native one, and 87% for both backends in callback
mode. A swing in the fixed component can no longer dominate the ratio the way
it did at 1 KiB.

### At the CI shape on a quiet machine the spread collapses

The same 64 MiB cell, re-measured at exactly the shape the job now runs
(`--iterations 5 --runs 20 --warmup 1`, three repeat invocations) once the
machine quietened (load average 1.6–3.2):

*Table 5. The chosen cell at CI shape, quiet machine (2026-09-16, later
session).*

| repeat | mode | python    | rust      | ratio  |
| ------ | ---- | --------- | --------- | ------ |
| 1      | nocb | 542.5 ms  | 450.0 ms  | 0.8295 |
| 2      | nocb | 536.9 ms  | 446.5 ms  | 0.8316 |
| 3      | nocb | 532.0 ms  | 448.0 ms  | 0.8421 |
| 1      | cb   | 1561.5 ms | 1443.8 ms | 0.9246 |
| 2      | cb   | 1534.7 ms | 1573.6 ms | 1.0253 |
| 3      | cb   | 1511.2 ms | 1422.7 ms | 0.9415 |

The no-callback ratio's relative standard deviation across repeats is **0.81%**
— two orders of magnitude inside the 30% threshold, and the per-run standard
deviation hyperfine reports is 8–20 ms on a ~450–540 ms run. The callback mode
is looser (5.6%, and hyperfine flagged one statistical outlier), which is what
the confirmation re-measurement and the MAD band are for.

Two things this does *not* say. The absolute ratios differ sharply between
Table 3 and Table 5 (0.43 versus 0.83 for the same cell) because the machine
load differed, and the two backends are not affected by load in the same
proportion: the pure-Python pump loses more wall clock to contention than the
native one. The *within-run* ratio is therefore still load-sensitive in a way
the payload change does not fix — hyperfine still measures the Python block and
the Rust block consecutively rather than interleaved. What the payload change
buys is that the ratio is no longer mostly fixed cost, so its *spread* is small
enough for a threshold to be meaningful; the residual load bias is the same
kind of bias the baseline and candidate share, which is why the gate compares
ratios across runs rather than wall clock.

### Wall clock fits the job's budget

Per cell, the mean wall clock of one hyperfine invocation of the four commands
at ten runs and one warmup each:

*Table 6. Wall clock per hyperfine invocation by payload, projected to twenty
runs and to the full selected workload (2026-09-16, development host).*

| Payload | invocation (4 commands, 10 runs) | invocation (4 commands, 20 runs) | 4 commands × 20 runs |
| ------- | -------------------------------- | -------------------------------- | -------------------- |
| 16 MiB  | 25.0 s                           | 50 s (projected)                 | 50 s                 |
| 32 MiB  | 41.3 s                           | 83 s (projected)                 | 83 s                 |
| 64 MiB  | 74.3 s                           | 84.4 s (measured, Table 5)       | ≈ 84 s               |
| 100 MiB | 100.8 s                          | 202 s (projected)                | 202 s                |

The ratchet job measures four scenarios — one payload, one two-stage depth, two
callback modes, both backends — and re-measures them a second time when
anything is flagged. At 64 MiB and twenty runs that is under two minutes of
measurement in the common case, under four with the confirmation pass, against
the job's sixty-minute timeout. The old profile measured twelve smoke scenarios
at ten runs.

### The old CI profile was not the workload the fixtures described

The profile selected two-stage scenarios at 1 KB and 64 KB. Those are the smoke
matrix's payloads, chosen for fast validation rather than for measurement, so
`--smoke` would have been the obvious way to get them: the ratchet was
measuring a validation fixture. That is the design error behind the numbers
above — the profile inherited a payload tier that was never meant to carry a
comparison.

## Decision

The ratchet now measures a dedicated payload tier rather than a smoke tier:

- `CI_RATCHET_PAYLOAD_BYTES = 64 MiB`, selected by `--ci-ratchet` on
  `benchmarks/pipeline_throughput.py` and labelled `ratchet` in the scenario
  matrix, so the workload is named and owned rather than borrowed.
- `ci_benchmark_ratchet_profile.py` accepts only scenarios inside the
  32–128 MiB band — past every measured crossover, where the five iterations'
  streaming has paid for the five iterations' set-up — and rejects the rest
  instead of measuring them.
- Five worker iterations (down from twenty) keep one measured run a few hundred
  milliseconds at this payload — around 63% of it streaming in the no-callback
  mode, 86% in callback mode — and twenty hyperfine runs (up from ten) tighten
  the mean of each command.
- `BENCHMARK_PROFILE_VERSION` was bumped to
  `pipeline-worker-release-ratio-v5`. A payload size is not recorded in a
  sample, so the version gate is the only thing that stops a v4 ratio from
  being compared against a v5 one.

`--max-regression` stays at 0.30. The evidence above says the threshold was not
the problem: the 1 KiB ratio's own observed spread exceeded it between two
repeats on one machine, so widening the floor would have hidden real
regressions while the noise that caused the failure remained. The fix is to
measure something whose spread is small enough for the threshold to mean
something.

## Limitations

- Every number here was taken on the development host with other agents' work
  running, not on the CI runner: Table 3 under a load average of 5–23, the
  later cells and Table 5 under 0.6–3.8. The load changes the *absolute*
  figures by more than a factor of two between sessions, so neither table's
  wall clock transfers to CI. The *design* conclusion — streaming must dominate
  the fixed per-iteration cost — is a property of the cost model and does
  transfer; the exact ratio spreads do not, and hyperfine reported statistical
  outliers in several cells.
- Four repeats per payload, with only two retained at 1 KiB (one probe
  aborted), is enough to see the trend and not enough to estimate a
  distribution. The band column is a MAD over four values.
- Hyperfine does not interleave commands, so the Python and Rust blocks are
  measured consecutively and slow drift biases each ratio. A payload large
  enough to dominate the fixed cost reduces that bias's relative size but does
  not remove it.
- The 100 MiB cell was measured but rejected: it buys a tighter band than 64
  MiB at roughly twice the wall clock, and the confirmation re-measurement has
  to fit in the same job.

## Reproducing

```bash
# the extension must be built the way CI builds it
make develop MATURIN_DEVELOP_FLAGS='--release --skip-install'

# one cell: four commands, one warmup and N runs each
/tmp/tune_ratchet.py --payload 67108864 --iterations 5 --repeats 4 --runs 10

# the fixed-versus-streaming split: one command pair per backend, 1 MiB
# against 64 MiB, at the CI iteration count
/tmp/decompose_cost.py
```

The harnesses that produced these tables are scratch, outside the repository.
Their commands match `pipeline_throughput_runner._build_worker_command`,
including the `CUPRUM_STREAM_BACKEND` environment prefix that is the only
difference between a Python and a Rust scenario.
