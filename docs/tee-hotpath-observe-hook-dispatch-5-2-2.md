# Tee hot-path observe-hook dispatch (5.2.2)

This document records the measurement for roadmap item 5.2.2, which removes the
per-hook `inspect.isawaitable` call from the per-line event dispatcher. It
supplements
[`tee-hotpath-line-event-emission-5-2-1.md`](tee-hotpath-line-event-emission-5-2-1.md)
and keeps every sample count, share, and timing pair reviewable in Markdown.
The raw captures and result documents are in
[`profiling/5-2-2-observe-hook-dispatch/`](profiling/5-2-2-observe-hook-dispatch/README.md).

## Result

The change is implemented, correct, and removes the detector from the hot path
entirely. Every candidate capture reports zero sampled weight on
`inspect.isawaitable` under the per-line emitter, and every matched control
reports between 2,572 and 2,762.

The evidence below is measured on the delivery candidate, not on an
intermediate revision: the control is the PR base `e170f908f` and the candidate
is the PR head `064d69405`. An earlier collection on `ccc970116` and
`2dd643995` is superseded; "Superseded collection" explains why.

| Quantity                                          | Control (`e170f908`) | Candidate (`064d69405`)        |
| ------------------------------------------------- | -------------------- | ------------------------------ |
| `isawaitable` weight under `emit_line`, 3 pairs   | 2,572 – 2,762        | **0**                          |
| anchored weight, 3 pairs                          | 33,602 – 36,547      | 30,886 – 43,268                |
| target share of the anchor, 3 pairs               | 7.40% – 7.65%        | 0                              |
| construction share, median of three pairs         | 29.4469%             | 30.4699%                       |
| wall time, geometric-mean ratio, six `cb` pairs   | 1.0 (reference)      | 0.956848                       |
| 95% interval for that ratio, six pairs            | —                    | [0.934407, 0.985617]           |
| CPU time, geometric mean and interval, same pairs | 1.0 (reference)      | 0.958527, [0.938406, 0.985121] |
| sync-hook dispatch, median of four runs           | 434.89 µs / 1,000    | 171.83 µs / 1,000              |
| `async def`-hook dispatch, median of four runs    | 4,380.97 µs / 1,000  | 4,527.14 µs / 1,000            |

The census separates the variants cleanly. In all three control captures the
target weights agree exactly — the location-specific and name-only counts are
identical — and no target sample lies outside the anchor, so the detector is
reachable only from the per-line path. In all three candidate captures both
target weights are zero while the anchor carries at least 30,886 samples, well
above the 14,000 floor, so the zero is an observed absence rather than an
unobserved one.

The construction share **rises**, and that is the expected direction: the
metric is `N/D`, the removed work is denominator-only, and a smaller
denominator raises the share. The measured rise, 1.0230 points, is within 0.06
points of the 5.2.1 forecast; see "Reconciling the share with the 5.2.1
forecast".

The change is also faster in fact, not only in profile. Across six admitted
unprofiled pairs the candidate's wall time is 4.3% below the control's at the
geometric mean, and the 95% interval's upper bound, 0.9856, is inside the 1.05
tolerance; the child's CPU time gives the same answer, 0.9585 with an upper
bound of 0.9851. The in-process microbenchmark isolates the mechanism: a hook
that returns `None` now costs about 172 ns per event instead of 435 ns, while an
`async def` hook, whose cost is `asyncio.Task` creation, does not move.

## Protocol and environment

Every profiled capture and every unprofiled arm used one interpreter, CPython
3.14.4 (`3.14.4 (main, Apr 14 2026, 14:26:14) [Clang 22.1.3]`), and py-spy
0.4.2 at 100 Hz. The fixture is the 5.2.1 line-oriented fixture, regenerated
from the committed generator and verified byte-identical by checksum:

| property     | value                                                              |
| ------------ | ------------------------------------------------------------------ |
| path         | `dist/fixtures/seed12345-wrap76.b64` (not committed)               |
| generator    | `benchmarks/deterministic_b64_fixture.py --seed 12345 --wrap 76`   |
| raw bytes    | 1,610,612,736                                                      |
| output bytes | 2,175,740,011                                                      |
| lines        | 28,256,364                                                         |
| SHA-256      | `51394f18e57972a681a2eb97c7c477d02d1d15b175247f518a60c331319774cc` |

The control is the PR base `e170f908f2a7b4187cc321296a9029e621169b07`, whose
`_emit_exec_event` still calls `inspect.isawaitable(result)` unconditionally.
The candidate is the PR head `064d6940502096ebb038f146673dae8f8b750af4`, which
carries the result-guarded dispatcher. The only runtime difference between the
two trees is `cuprum/_observability.py`; `benchmarks/` is identical in both.
Each capture ran from inside its own worktree so that `python -m` imported that
tree's `cuprum`; `variant.txt` in every capture directory records the
interpreter, the resolved package directory, and the revision, and the two
variants resolve to different directories and different revisions.

The host is shared with other agents' builds. During this collection the
one-minute load average ranged from 9.99 to 44.32, so profiled wall times are
*not* used as a speed measurement: the arms did not run under comparable
conditions. The profiled pairs alternate order — control first in round 1,
candidate first in round 2, control first in round 3 — and the unprofiled pairs
use an ABBA schedule.

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

| capture        | anchor | target | any   | outside | total   | census exit | status                |
| -------------- | ------ | ------ | ----- | ------- | ------- | ----------- | --------------------- |
| `r1-control`   | 36,547 | 2,762  | 2,762 | 0       | 80,741  | 1           | `fail_target_present` |
| `r1-candidate` | 35,919 | 0      | 0     | 0       | 84,678  | 0           | `pass`                |
| `r2-control`   | 36,179 | 2,678  | 2,678 | 0       | 78,975  | 1           | `fail_target_present` |
| `r2-candidate` | 43,268 | 0      | 0     | 0       | 104,862 | 0           | `pass`                |
| `r3-control`   | 33,602 | 2,572  | 2,572 | 0       | 72,006  | 1           | `fail_target_present` |
| `r3-candidate` | 30,886 | 0      | 0     | 0       | 69,784  | 0           | `pass`                |

The control's target share of its anchored weight is 7.56%, 7.40%, and 7.65%
across the three rounds, so the floor of 14,000 anchored samples would have
exposed a surviving detector with near-certainty. The total weight differs
between the arms because the captures ran for different durations under
different host load; the census's quantities of interest are ratios of weights
within one capture, so the difference does not affect them.

### Reconciling the folded weight with the census total

Each capture's `stacks.folded` contains exactly one line whose stack is empty —
a single space followed by the sample weight — because py-spy emits a line with
no resolvable frames at least once per run. The census drops that line, so the
folded weight sum exceeds `total_weight` by exactly its weight:

| capture        | folded sum | `total_weight` | empty-stack line |
| -------------- | ---------- | -------------- | ---------------- |
| `r1-control`   | 80,743     | 80,741         | `2`              |
| `r1-candidate` | 84,712     | 84,678         | `34`             |
| `r2-control`   | 78,978     | 78,975         | `3`              |
| `r2-candidate` | 104,875    | 104,862        | `13`             |
| `r3-control`   | 72,009     | 72,006         | `3`              |
| `r3-candidate` | 69,787     | 69,784         | `3`              |

The six differences are exactly the six empty-stack weights, so the census read
every line it could attribute and discarded nothing else. py-spy's own record
agrees: each capture's log reports `Samples: <n> Errors: 0` with `<n>` equal to
the folded sum.

### py-spy teardown on the round-1 pair

Both round-1 captures record py-spy exit status 1, while the round-2 and
round-3 captures exit 0. The round-1 log ends with
`Error: No child process (os error 10)` after the final sample, which is a
reaping race in py-spy's teardown: the worker had already exited and been
reaped by the time py-spy waited for it. The capture is complete — the sample
count is non-zero, `Errors: 0`, and the committed weights reconcile with it —
so the exit status records the race rather than a failed capture. The status is
committed as recorded rather than normalised, and the census does not read it.

## Construction share

The 5.2.1 classifier was re-run on every capture, unchanged, with the committed
rules and its 30% limit:

| capture        | construction share | classifier exit | status             |
| -------------- | ------------------ | --------------- | ------------------ |
| `r1-control`   | 29.2698%           | 0               | `pass`             |
| `r1-candidate` | 30.4699%           | 1               | `fail_above_limit` |
| `r2-control`   | 29.4469%           | 0               | `pass`             |
| `r2-candidate` | 30.1865%           | 1               | `fail_above_limit` |
| `r3-control`   | 29.5660%           | 0               | `pass`             |
| `r3-candidate` | 30.5440%           | 1               | `fail_above_limit` |

This gate is 5.2.1's, not 5.2.2's, and its exit status is not a 5.2.2
acceptance criterion. It is reported because the plan requires the classifier
to be re-run on the same stacks, and because the direction of the change is the
one 5.2.1 predicted.

### Reconciling the share with the 5.2.1 forecast

5.2.1 forecast that removing the detector would move the share from 29.9414% to
30.9686%, a rise of 0.9686 points, and labelled that a conditional forecast
because no capture existed with the call absent. This collection measures a
control median of 29.4469% and a candidate median of 30.4699%, a rise of 1.0230
points.

The forecast is accurate to 0.05 points on the rise and low by 0.49 points on
the level: both arms sit below 5.2.1's 29.9414% reference. That reference came
from a different collection, on a different pair of revisions, under a much
lower host load, so the level is not comparable across collections; the rise
is, because each pair is measured on the same stacks.

An earlier collection of this item measured a rise of 2.30 points and explained
it as the forecast counting only the detector's leaf frames while the
denominator loses every stack that reached it. This collection does not
reproduce that size, so the explanation is withdrawn: with three pairs on the
delivered revisions the rise is 0.74 to 1.20 points across the three pairs, and
the forecast's own figure sits inside that range.

## Unprofiled paired rounds

Six ABBA pairs of the `cb` scenario (echo to a devnull sink, one stage, Python
backend, line callbacks, repeat count 1) were admitted under the load rule of
Decision D11: rounds 1, 3 and 5 ran control first and rounds 2, 4 and 6
candidate first, so a slow drift in host conditions cannot favour one arm.

| round | order | control (s) | candidate (s) | wall ratio | control CPU (s) | candidate CPU (s) | CPU ratio | load median control/candidate | host busy control/candidate |
| ----- | ----- | ----------- | ------------- | ---------- | --------------- | ----------------- | --------- | ----------------------------- | --------------------------- |
| 1     | cb    | 261.54      | 246.07        | 0.9409     | 258.75          | 244.97            | 0.9467    | 31.31 / 31.54                 | 0.543 / 0.549               |
| 2     | bc    | 257.47      | 247.47        | 0.9611     | 256.52          | 246.31            | 0.9602    | 26.49 / 29.34                 | 0.458 / 0.546               |
| 3     | cb    | 255.14      | 238.95        | 0.9365     | 253.92          | 238.41            | 0.9389    | 16.31 / 14.05                 | 0.519 / 0.484               |
| 4     | bc    | 277.16      | 254.48        | 0.9182     | 273.43          | 252.97            | 0.9251    | 16.06 / 16.04                 | 0.578 / 0.513               |
| 5     | cb    | 252.74      | 258.85        | 1.0242     | 251.61          | 256.37            | 1.0189    | 15.00 / 15.59                 | 0.511 / 0.543               |
| 6     | bc    | 249.31      | 240.23        | 0.9636     | 249.12          | 240.15            | 0.9640    | 13.34 / 11.84                 | 0.478 / 0.388               |

The wall-time geometric mean ratio is 0.956848, with a bootstrap 95% interval
of [0.934407, 0.985617] over 10,000 resamples (seed 20261105). The candidate is
4.3% faster at the geometric mean, and the interval's upper bound is 0.9856, so
this collection does not support a slowdown and clears the 1.05 tolerance.

The child's CPU time gives the same answer independently: a geometric mean
ratio of 0.958527 with an interval of [0.938406, 0.985121]. CPU time is 98.7%
to 100.0% of wall time in every arm, so the workload is CPU-bound and the two
quantities measure nearly the same thing; agreement between them is evidence
that the wall-time ratios were not produced by the scheduler giving one arm
more of the machine.

Round 5 is the only pair whose ratio exceeds 1, and it is retained. The
admission rule reads load and the host's busy share, never a ratio, so
discarding a pair after seeing its result would be selecting on the outcome.

### Admission under Decision D11

Decision D9's ceiling of 20 was unreachable during this collection: the host's
one-minute load average stayed between 20 and 45 for more than six hours while
other agents' builds ran, and four round-1 attempts under D9 were rejected at
sampled maxima of 22.04/29.53, 28.43/36.55, 44.98/45.45, and 39.56/42.28
(`unprofiled/superseded/timing-attempts.tsv`, pass 3). An eighth attempt that
waited 45 minutes for a quiet window before each pair admitted nothing.

Decision D11 replaces the absolute ceiling with the properties the ceiling was
a proxy for, measured directly: a burst inside one arm, and an arm-to-arm
imbalance. A pair is admitted when both arms exit 0 with a result document, the
pair's pooled median load is at most 45, the arms' load medians differ by at
most 20% of the pooled median, the arms' measured host-busy shares differ by at
most 10 points, and no arm's maximum exceeds the pooled median by more than 15.
The rule reads `/proc/loadavg`, `/proc/stat`, and exit status only. It was
refined twice while the collection ran, because its own load diagnostics showed
it rejecting pairs whose two arms had measured within a point of the same host
busy share; the final form was applied uniformly to all ten recorded attempts,
and each round's earliest qualifying attempt is the one adopted. Two attempts
fail the final rule and are not used:

| round | attempt | pooled median load | maxima control/candidate | host busy control/candidate | reason                                                     |
| ----- | ------- | ------------------ | ------------------------ | --------------------------- | ---------------------------------------------------------- |
| 2     | 4       | 30.00              | 39.12 / 30.78            | 0.611 / 0.483               | busy shares differ by 12.8 points                          |
| 3     | 1       | 18.73              | 40.06 / 21.53            | 0.703 / 0.543               | medians differ by 7.23; busy by 15.9 points; control burst |

`d11-attempts.tsv` records every attempt with the verdict of the rule in force
when it ran, `d11-admissions.json` the final rule's verdict for each and the
adopted attempt per round, and `load-samples.json` the full five-second load
series and CPU record for every adopted arm. The rejected attempts' series are
not committed — `.gitignore` excludes `*.log` — but their maxima, medians, and
busy shares are in the table above and in the TSV, which is what the rule reads.

One further pair was run before round 1 and discarded by protocol as the
warm-up, to prime the fixture cache before any measurement; it is excluded
before its result exists and is not part of the accepted set. Its documents are
committed under `unprofiled/warmup-<arm>.*` so the exclusion can be checked
rather than assumed: control 245.63 s and candidate 258.10 s, a ratio of 1.051.
That the one excluded pair is also the least favourable to the candidate is a
consequence of excluding it in advance, not a reason for the exclusion.

## Microbenchmark

The `observe-dispatch` group in `benchmarks/test_stream_microbenchmarks.py`
dispatches 1,000 events per benchmark call, so each figure below is the cost of
1,000 dispatches and dividing by 1,000 gives the per-event cost. The control
worktree predates the group, so the candidate's benchmark file was copied into
it; the file imports `_emit_exec_event` and times it, and adds no production
code, so the control run measures the control dispatcher.

Four runs were made in each tree: the primary pair, then three repeats that
alternate which tree runs first. Medians, in microseconds per 1,000 dispatches:

| benchmark          | control (four runs)                    | candidate (four runs)                  | candidate/control |
| ------------------ | -------------------------------------- | -------------------------------------- | ----------------- |
| sync hook (`None`) | 441.44, 436.77, 433.02, 429.40         | 170.72, 169.14, 172.93, 173.74         | 0.395             |
| `async def` hook   | 4,521.39, 4,367.98, 4,365.39, 4,393.97 | 4,415.46, 4,504.33, 4,664.90, 4,549.94 | 1.033             |

The median of the four medians is 434.89 µs for the control's sync hook and
171.83 µs for the candidate's, a 2.53x reduction, or about 263 ns saved per
event. That is the per-line cost this item removes, measured directly rather
than inferred from a profile.

The `async def` hook does not move: the median of medians is 4,380.97 µs
against 4,527.14 µs, 3.3% in the candidate's favour, and the two four-run
ranges overlap (4,365.39 to 4,521.39 against 4,415.46 to 4,664.90). The path is
dominated by `asyncio.Task` creation at roughly 4.4 µs per event, which is why
Decision D8 declined to profile it at full scale. The means are noisier than
the medians — the primary candidate run's mean is the largest of the eight
async figures while its median is in line with the rest — so the comparison
uses medians. `microbench-repeats.json` holds every run.

These runs used the project virtual environment's CPython 3.14.7, not the
3.14.4 interpreter the profiler and the unprofiled arms used, because
`pytest-benchmark` is installed there. Both variants ran on the same
interpreter, so the comparison holds within the run, but the absolute figures
are not comparable with the profiled captures or with the `cb` pair wall times.

## Acceptance status

| criterion                                             | result                                                    |
| ----------------------------------------------------- | --------------------------------------------------------- |
| every candidate census: both target weights 0, exit 0 | met in all three captures                                 |
| every control census: equal non-zero weights, exit 1  | met in all three captures                                 |
| every anchor weight at least 14,000                   | met; the smallest is 30,886 (candidate, round 3)          |
| `cb` ratio's 95% interval upper bound at most 1.05    | met; the upper bound is 0.9856 (CPU time 0.9851)          |
| construction share re-measured on the same stacks     | 29.4469% control median, 30.4699% candidate median        |
| microbenchmark of the dispatched group                | sync hook 2.53x faster; `async def` hook 1.033, unchanged |

The construction share is reported, not judged: it is 5.2.1's gate, and a rise
is what that metric does when work outside the counted constructor is removed
(Constraint C10). The detector's own sampled weight, which is 5.2.2's subject,
falls to zero in every candidate capture.

## Superseded collection

The first collection of this item's evidence measured a control of `ccc970116`
and a candidate of `2dd643995` and is preserved in the branch history. It was
replaced because it measured intermediate revisions rather than the revisions
delivered by the pull request: the candidate's dispatcher was refactored into
`_emit_observe_hook` after `2dd643995`, so a measurement of that revision does
not describe the delivered hot path. Every profiled capture, every unprofiled
pair, and all eight microbenchmark runs in this directory were re-collected
against `e170f908` and `064d69405`, which is why the directory's files carry
the later revisions.

None of the superseded collection's figures is quoted as a result here. Its
records survive in two places. `unprofiled/superseded/` keeps that collection's
attempt table (`superseded/timing-attempts.tsv`, cited above for the D9
rejections), its summary documents, and the four attempt pairs whose result
documents the re-collection did not overwrite. Every other result document of
that collection was replaced when the re-collection wrote the same file names,
so it survives only in the branch history at the commit that added it; this
report does not depend on any of them.

## Machine-readable outputs

Every number above is derived from a committed file:

- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/frame-census.json`
  — the census result documents.
- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/construction-share.json`
  — the classifier result documents.
- `profiling/5-2-2-observe-hook-dispatch/r{1,2,3}-{control,candidate}/worker-result.json`
  — each run's own timings, line count, and fixture checksum.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/cb-rN-<arm>.json` — the
  adopted arms' worker result documents, each carrying its own
  `wall_time_seconds`.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/cb-rN-<arm>.cpu.json` — the
  same arms' CPU-time records and measured host-busy shares.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/load-samples.json` — every
  adopted arm's five-second load series, which is the input the admission rule
  reads.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/d11-attempts.tsv` — every
  attempt's load statistics and the verdict of the rule in force.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/d11-admissions.json` — the
  final rule's verdict for every attempt and the adopted attempt per round.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/timing-summary.json` — the
  paired ratios, their geometric means, and the bootstrap intervals.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/attempts/` — every attempt's
  result, exit, and CPU-time documents, admitted or not.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/warmup-<arm>.json` — the
  discarded warm-up pair, excluded before its result existed.
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/superseded/` — the earlier
  collection's surviving records, described under "Superseded collection".
- `profiling/5-2-2-observe-hook-dispatch/microbench-repeats.json` — every
  microbenchmark run.
