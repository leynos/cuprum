# Tee hot-path observe-hook dispatch (5.2.2)

This document records the measurement for roadmap item 5.2.2, which removes the
per-hook `inspect.isawaitable` call from the per-line event dispatcher. It
supplements
[`tee-hotpath-line-event-emission-5-2-1.md`](tee-hotpath-line-event-emission-5-2-1.md)
and keeps every sample count, share, and wall-time pair reviewable in
Markdown. The raw captures and result documents are in
[`profiling/5-2-2-observe-hook-dispatch/`](profiling/5-2-2-observe-hook-dispatch/README.md).

## Result

The change is implemented, correct, and removes the detector from the hot path
entirely. Every candidate capture reports zero sampled weight on
`inspect.isawaitable` under the per-line emitter, and every matched control
reports about 1,900.

| Quantity                                         | Control (`ccc970116`) | Candidate (`2dd643995`) | Change         |
| ------------------------------------------------ | --------------------- | ----------------------- | -------------- |
| `isawaitable` weight under `emit_line`, 3 pairs  | 1,887 – 1,964         | **0**                   | −100%          |
| anchored weight, 3 pairs                         | 21,425 – 22,591       | 18,217 – 19,675         | −10.3%         |
| construction share, median of three pairs        | 30.7670%              | 33.0662%                | +2.2992 points |
| wall time, median, profiled callback scenario    | 413.4 s               | 390.3 s                 | −5.59%         |
| wall time, geometric-mean ratio, unprofiled `cb` | 1.0 (reference)       | 0.9529                  | −4.71%         |
| 95% interval for that ratio, six pairs           | —                     | [0.9199, 1.0001]        | —              |
| sync-hook dispatch, median of four runs          | 385.91 µs / 1,000     | 113.92 µs / 1,000       | −70.5%         |

The census separates the variants cleanly. In all three control captures the
target weights agree exactly — the location-specific and name-only counts are
identical — and no target sample lies outside the anchor, so the detector is
reachable only from the per-line path. In all three candidate captures both
target weights are zero while the anchor carries at least 18,217 samples, well
above the 14,000 floor, so the zero is an observed absence rather than an
unobserved one.

The construction share **rises**, and that is the expected direction: the
metric is `N/D`, the removed work is denominator-only, and a smaller
denominator raises the share. The measured rise is larger than the 5.2.1
forecast; see "Reconciling the share with the 5.2.1 forecast".

The change is also faster in fact, not only in profile. Across six
load-admitted unprofiled pairs the candidate's wall time is 4.71% below the
control's at the geometric mean, and the 95% interval's upper bound, 1.0001, is
well inside the 1.05 tolerance. The microbenchmark isolates the mechanism: a
hook that returns `None` now costs about 114 ns per event instead of 386 ns,
while an `async def` hook, whose cost is `asyncio.Task` creation, is unchanged.

## Protocol and environment

Every run used one interpreter, CPython 3.14.4
(`3.14.4 (main, Apr 14 2026, 14:26:14) [Clang 22.1.3]`), and py-spy 0.4.2 at
100 Hz. The fixture is the 5.2.1 line-oriented fixture, regenerated from the
committed generator and verified byte-identical by checksum:

| property     | value                                                              |
| ------------ | ------------------------------------------------------------------ |
| path         | `dist/fixtures/seed12345-wrap76.b64` (not committed)               |
| generator    | `benchmarks/deterministic_b64_fixture.py --seed 12345 --wrap 76`   |
| raw bytes    | 1,610,612,736                                                      |
| output bytes | 2,175,740,011                                                      |
| lines        | 28,256,364                                                         |
| SHA-256      | `51394f18e57972a681a2eb97c7c477d02d1d15b175247f518a60c331319774cc` |

The control is the EP-M1 head, `ccc970116`, which adds the red tests and leaves
production code untouched; its `_emit_exec_event` still calls
`inspect.isawaitable(result)` unconditionally. The candidate is `2dd643995`,
the EP-M4 head, which carries the result-guarded dispatcher. Each capture ran
from inside its own worktree so that `python -m` imported that tree's `cuprum`;
`variant.txt` in every capture directory records the interpreter, the resolved
package directory, and the revision, and the two variants resolve to different
directories and different revisions.

The host is a shared six-core machine running other agents' builds, so load
varied between runs (one-minute load average 6.3 to 22.0 at capture start). The
profiled pairs alternate order — control first in round 1, candidate first in
round 2, control first in round 3 — and the unprofiled pairs use an ABBA
schedule, so drift in host load affects both variants rather than one.

### Profiled capture command

```bash
py-spy record --format raw --rate 100 \
  --output <capture>/stacks.folded -- \
  <python> -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 --mode echo \
  --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 2 --read-size 65536 --output <capture>/worker-result.json
```

The repeat count is 2 rather than 5.2.1's 1 so that each capture clears the
census's 14,000-sample anchor floor with margin; the share is a ratio, so the
doubling cancels.

### Census and classification commands

```bash
python -m benchmarks.summarize_hot_path_frames <capture>/stacks.folded \
  --anchor emit_line@cuprum/_line_callbacks.py \
  --target isawaitable@inspect.py \
  --min-anchor-weight 14000 --output <capture>/frame-census.json

python -m benchmarks.summarize_line_event_profile <capture>/stacks.folded \
  --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output <capture>/construction-share.json
```

### Unprofiled command

```bash
<python> -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 --mode echo \
  --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 1 --read-size 65536 --output <capture>.json
```

## Profiled pairs

Every weight counts a stack once. `target` matches `isawaitable` at its own
rendered location; `any` matches the function name alone; `outside` counts
target samples that do not pass through the anchor.

| capture        | anchor | target | any   | outside | total  | census exit | status                |
| -------------- | ------ | ------ | ----- | ------- | ------ | ----------- | --------------------- |
| `r1-control`   | 21,572 | 1,899  | 1,899 | 0       | 41,458 | 1           | `fail_target_present` |
| `r1-candidate` | 19,346 | 0      | 0     | 0       | 39,089 | 0           | `pass`                |
| `r2-control`   | 21,425 | 1,887  | 1,887 | 0       | 41,338 | 1           | `fail_target_present` |
| `r2-candidate` | 18,217 | 0      | 0     | 0       | 36,459 | 0           | `pass`                |
| `r3-control`   | 22,591 | 1,964  | 1,964 | 0       | 43,845 | 1           | `fail_target_present` |
| `r3-candidate` | 19,675 | 0      | 0     | 0       | 39,378 | 0           | `pass`                |

The control's target share of its anchored weight is 8.80%, 8.81%, and 8.69%
across the three rounds, so the floor of 14,000 anchored samples would have
exposed a surviving detector with near-certainty. The candidate's anchored
weight is 10.3% lower than the control's median, which is the removed detector
work itself plus the sampling variation of a shorter run.

## Construction share

The 5.2.1 classifier was re-run on every capture, unchanged, with the committed
rules:

| capture        | construction share | classifier exit | status             |
| -------------- | ------------------ | --------------- | ------------------ |
| `r1-control`   | 30.8470%           | 1               | `fail_above_limit` |
| `r1-candidate` | 33.0662%           | 1               | `fail_above_limit` |
| `r2-control`   | 30.2489%           | 1               | `fail_above_limit` |
| `r2-candidate` | 33.1515%           | 1               | `fail_above_limit` |
| `r3-control`   | 30.7670%           | 1               | `fail_above_limit` |
| `r3-candidate` | 33.0309%           | 1               | `fail_above_limit` |

This gate is 5.2.1's, not 5.2.2's, and its exit status is not a 5.2.2
acceptance criterion. It is reported because the plan requires the classifier
to be re-run on the same stacks, and because the direction of the change is the
one 5.2.1 predicted.

### Reconciling the share with the 5.2.1 forecast

5.2.1 forecast that removing the detector would move the share from 29.9414% to
30.9686%, a rise of 0.9686 points, and labelled that a conditional forecast
because no capture existed with the call absent. The measured rise is 2.30
points, from a control median of 30.7670% to a candidate median of 33.0662%.

The forecast was built from a leaf-only count of 589 `inspect.isawaitable`
samples. The census counts every stack that *reaches* the detector, which
includes the `abc.__instancecheck__` work beneath it, and that is 1,887 to
1,964 samples per capture at repeat count 2 — 787 to 1,065 per repeat, matching
the range the plan's purpose section quotes. The denominator therefore loses
more than the forecast assumed, and the share rises further. The forecast's
direction was right and its size was low; the measured figure replaces it.

The control's own share, 30.25% to 30.85%, also sits above 5.2.1's recorded
29.9087% median. That is a property of this host and this collection, not of
5.2.2: the control is the post-5.2.1 tree with production code untouched. It
does mean 5.2.1's 0.09-point margin over its 30% bar is thinner than the
run-to-run variation observed here, which is worth knowing before that gate is
re-judged.

## Unprofiled paired rounds

Six ABBA pairs of the `cb` scenario (echo to a devnull sink, one stage, Python
backend, line callbacks, repeat count 1) were admitted under the load rule of
Decision D9: a pair counts only when neither arm's sampled one-minute load
average exceeds 20. Rounds 1-3 ran control first and rounds 4-6 candidate
first, so a slow drift in host conditions cannot favour one arm.

| round | control (s) | candidate (s) | ratio  | max load control | max load candidate |
| ----- | ----------- | ------------- | ------ | ---------------- | ------------------ |
| 1     | 173.65      | 159.82        | 0.9203 | 8.49             | 3.90               |
| 2     | 171.93      | 163.83        | 0.9529 | 4.67             | 3.83               |
| 3     | 176.25      | 187.90        | 1.0661 | 6.57             | 16.73              |
| 4     | 183.51      | 166.33        | 0.9064 | 13.42            | 7.77               |
| 5     | 182.29      | 168.48        | 0.9243 | 10.10            | 7.71               |
| 6     | 175.19      | 167.48        | 0.9560 | 8.30             | 7.48               |

The geometric mean ratio is 0.9529, with a bootstrap 95% interval of [0.9199,
1.0001] over 10,000 resamples (seed 20261105). The candidate is 4.7% faster at
the geometric mean, and the interval's upper bound is 1.0001, so this
collection does not support a slowdown and clears the 1.05 tolerance.

Round 3 is the only pair whose ratio exceeds 1, and it is also the pair with
the largest load asymmetry: its candidate arm ran while the one-minute average
rose to 16.73, against 6.57 for its control. It is retained because the
admission rule reads load, not the ratio; discarding it after seeing the result
would be selecting on the outcome.

### Rejected attempts

`timing-attempts.tsv` records every attempt of both load-gated passes, and
`load-samples.json` holds the full sampled series for each arm that survived on
disk. Two earlier collections are not part of the accepted set. The first two
reached load 33.0 and 38.2 and produced intervals of [0.8228, 1.0693] and
[0.8531, 1.0948]. A third waited for a load average below 8 before each pair
but still met spikes of 35.2, 33.35/33.66, 23.88/38.7, and 53.15 inside rounds
3-6; its interval, [0.7152, 1.0101], clears the bound but is computed from runs
that measured other tenants' linker bursts, so it is not the evidence this
report accepts.

The first load-gated pass rejected every attempt because of a defect in the
driver: the arm's diagnostic line went to standard output, so the captured
"maximum" was a timestamp whose year, 2026, `awk` compared against the ceiling.
Round 4's attempt 2 in that pass sampled 8.55 and 6.04 and was still rejected;
its candidate series was then destroyed by the next attempt's cleanup, so it
was re-collected rather than admitted on a partial record. The second pass
fixed the driver, preserved each attempt's artefacts under
`unprofiled/attempts/`, and admitted rounds 3-6 on the attempts listed above.

## Microbenchmark

The `observe-dispatch` group in `benchmarks/test_stream_microbenchmarks.py`
dispatches 1,000 events per benchmark call, so each figure below is the cost of
1,000 dispatches and dividing by 1,000 gives the per-event cost. The control
worktree predates the group, so the candidate's benchmark file was copied into
it; the file imports `_emit_exec_event` and times it, and adds no production
code, so the control run measures the control dispatcher.

Four runs were made: the committed primary pair, then three repeats that
alternate which tree runs first. Medians, in microseconds per 1,000 dispatches:

| benchmark          | control (four runs)                    | candidate (four runs)                  | candidate/control |
| ------------------ | -------------------------------------- | -------------------------------------- | ----------------- |
| sync hook (`None`) | 392.96, 384.80, 387.01, 382.20         | 117.54, 113.82, 112.24, 114.02         | 0.295             |
| `async def` hook   | 4,441.46, 4,275.10, 4,138.74, 4,177.51 | 4,502.55, 4,164.44, 4,098.18, 4,089.29 | 0.978             |

The median of the four medians is 385.91 µs for the control's sync hook and
113.92 µs for the candidate's, a 3.39x reduction, or about 272 ns saved per
event. That is the per-line cost this item removes, measured directly rather
than inferred from a profile.

The `async def` hook is unchanged within run-to-run variation: 4,226.31 µs
against 4,131.31 µs, 2.3% in the candidate's favour. The path is dominated by
`asyncio.Task` creation at roughly 4.1 µs per event, which is why Decision D8
declined to profile it at full scale. The means are noisier than the medians —
the primary candidate run's mean of 6,349.48 µs is the largest of the eight
async figures while its median, 4,502.55 µs, is in line with the rest — so the
comparison uses medians. `microbench-repeats.json` holds every run.

## Acceptance status

| criterion                                             | result                                                      |
| ----------------------------------------------------- | ----------------------------------------------------------- |
| every candidate census: both target weights 0, exit 0 | met in all three captures                                   |
| every control census: equal non-zero weights, exit 1  | met in all three captures                                   |
| every anchor weight at least 14,000                   | met; the smallest is 18,217 (candidate, round 2)            |
| `cb` ratio's 95% interval upper bound at most 1.05    | met; the upper bound is 1.0001                              |
| construction share re-measured on the same stacks     | 30.7670% control median, 33.0662% candidate median          |
| microbenchmark of the dispatched group                | sync hook 3.39x faster; `async def` hook unchanged at 0.978 |

The construction share is reported, not judged: it is 5.2.1's gate, and a rise
is what that metric does when work outside the counted constructor is removed
(Constraint C10). The detector's own sampled weight, which is 5.2.2's subject,
falls to zero in every candidate capture.

## Machine-readable outputs

Every number above is derived from a committed file:

- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/frame-census.json`
  — the census result documents.
- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/construction-share.json`
  — the classifier result documents.
- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/worker-result.json`
  — each run's own timings, line count, and fixture checksum.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/*.json` — the unprofiled
  wall-time samples.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/timing-summary.json` — the
  paired ratios, their geometric mean, and the bootstrap interval.
