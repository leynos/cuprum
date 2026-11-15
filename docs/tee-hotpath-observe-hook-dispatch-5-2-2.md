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
reports between 1,932 and 2,207.

The evidence below is measured on the delivery candidate, not on an
intermediate revision: the control is the PR base `1d9e7d37f` and the candidate
is the PR head `0df4bbab6`. Two earlier collections are superseded — one on
`ccc970116` and `2dd643995`, and one on `e170f908f` and `064d69405`;
"Superseded collections" explains why.

| Quantity                                          | Control (`1d9e7d37f`) | Candidate (`0df4bbab6`)        |
| ------------------------------------------------- | --------------------- | ------------------------------ |
| `isawaitable` weight under `emit_line`, 3 pairs   | 1,932 – 2,207         | **0**                          |
| anchored weight, 3 pairs                          | 28,763 – 31,621       | 27,660 – 29,455                |
| target share of the anchor, 3 pairs               | 6.59% – 6.98%         | 0                              |
| construction share, median of three pairs         | 28.1409%              | 29.3436%                       |
| wall time, geometric-mean ratio, six `cb` pairs   | 1.0 (reference)       | 0.935504                       |
| 95% interval for that ratio, six pairs            | —                     | [0.897741, 0.974806]           |
| CPU time, geometric mean and interval, same pairs | 1.0 (reference)       | 0.936883, [0.899205, 0.975167] |
| sync-hook dispatch, median of 16 runs             | 433.23 µs / 1,000     | 167.25 µs / 1,000              |
| `async def`-hook dispatch, median of 16 runs      | 4,372.03 µs / 1,000   | 4,277.00 µs / 1,000            |

The census separates the variants cleanly. In all three control captures the
target weights agree exactly — the location-specific and name-only counts are
identical — and no target sample lies outside the anchor, so the detector is
reachable only from the per-line path. In all three candidate captures both
target weights are zero while the anchor carries at least 27,660 samples, well
above the 14,000 floor, so the zero is an observed absence rather than an
unobserved one.

The construction share **rises**, and that is the expected direction: the
metric is `N/D`, the removed work is denominator-only, and a smaller
denominator raises the share. The measured rise, 1.2027 points, is within a
quarter of a point of the 5.2.1 forecast; see "Reconciling the share with the
5.2.1 forecast".

The change is also faster in fact, not only in profile. Across six admitted
unprofiled pairs the candidate's wall time is 6.4% below the control's at the
geometric mean, and the 95% interval's upper bound, 0.9748, is inside the 1.05
tolerance; the child's CPU time gives the same answer, 0.9369 with an upper
bound of 0.9752. The in-process microbenchmark isolates the mechanism: a hook
that returns `None` now costs about 167 ns per event instead of 433 ns, while an
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

The control is the PR base `1d9e7d37fd289efb6fe270f06115363ec7893a88`, whose
`_emit_exec_event` still calls `inspect.isawaitable(result)` unconditionally.
The candidate is the PR head `0df4bbab6c5b531e59d103c8a7baee0d634ff146`, which
carries the result-guarded dispatcher. Between the two trees the only product
source difference is `cuprum/_observability.py`; the rest of the diff is this
item's own benchmark group, test modules, evidence documents, plan, changelog,
and roadmap entries. The classifier that computes the construction share
differs only in a comment that cites a moved line number, so both arms were
classified by the same rules.

Each capture ran from inside its own worktree so that `python -m` imported that
tree's `cuprum`; `variant.txt` in every capture directory records the
interpreter, the resolved package directory, and the revision, and the two
variants resolve to different directories and different revisions.

The host is shared with other agents' builds. The profiled captures recorded
one-minute load averages between 7.69 and 37.11, and the unprofiled attempts
that were rejected sampled maxima above 51, so profiled wall times are *not*
used as a speed measurement: the arms did not run under comparable conditions.
The profiled pairs alternate order — control first in round 1, candidate first
in round 2, control first in round 3 — and the unprofiled pairs use an ABBA
schedule.

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
| `r1-control`   | 31,621 | 2,207  | 2,207 | 0       | 62,614 | 1           | `fail_target_present` |
| `r1-candidate` | 27,660 | 0      | 0     | 0       | 57,494 | 0           | `pass`                |
| `r2-control`   | 30,315 | 1,998  | 1,998 | 0       | 60,587 | 1           | `fail_target_present` |
| `r2-candidate` | 29,455 | 0      | 0     | 0       | 60,261 | 0           | `pass`                |
| `r3-control`   | 28,763 | 1,932  | 1,932 | 0       | 55,376 | 1           | `fail_target_present` |
| `r3-candidate` | 28,179 | 0      | 0     | 0       | 57,809 | 0           | `pass`                |

The control's target share of its anchored weight is 6.98%, 6.59%, and 6.72%
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
| `r1-control`   | 62,620     | 62,614         | `6`              |
| `r1-candidate` | 57,498     | 57,494         | `4`              |
| `r2-control`   | 60,590     | 60,587         | `3`              |
| `r2-candidate` | 60,266     | 60,261         | `5`              |
| `r3-control`   | 55,384     | 55,376         | `8`              |
| `r3-candidate` | 57,816     | 57,809         | `7`              |

The six differences are exactly the six empty-stack weights, so the census read
every line it could attribute and discarded nothing else. py-spy's own record
agrees: each capture's log reports `Samples: <n> Errors: 0` with `<n>` equal to
the folded sum.

### py-spy teardown on the round-2 candidate

One capture records py-spy exit status 1: `r2-candidate`. Its log ends with
`Error: No child process (os error 10)` after the final sample, which is a
reaping race in py-spy's teardown: the worker had already exited and been
reaped by the time py-spy waited for it. The capture is complete — the sample
count is 60,266, `Errors: 0`, and the committed weights reconcile with it — so
the exit status records the race rather than a failed capture. The status is
committed as recorded rather than normalised, and the census does not read it.
Which capture the race reaches varies between collections, so it is a property
of teardown timing, not of either variant: the earlier collection of this item
saw it on the round-1 pair instead.

## Construction share

The 5.2.1 classifier was re-run on every capture, unchanged, with the committed
rules and its 30% limit:

| capture        | construction share | classifier exit | status |
| -------------- | ------------------ | --------------- | ------ |
| `r1-control`   | 28.1409%           | 0               | `pass` |
| `r1-candidate` | 28.8185%           | 0               | `pass` |
| `r2-control`   | 27.8379%           | 0               | `pass` |
| `r2-candidate` | 29.7520%           | 0               | `pass` |
| `r3-control`   | 28.1938%           | 0               | `pass` |
| `r3-candidate` | 29.3436%           | 0               | `pass` |

This gate is 5.2.1's, not 5.2.2's, and its exit status is not a 5.2.2
acceptance criterion. It is reported because the plan requires the classifier
to be re-run on the same stacks, and because the direction of the change is the
one 5.2.1 predicted. The candidate arm is above the control arm in every pair —
by 0.68, 1.91, and 1.15 points — and no arm now exceeds the 30% limit. The
previous collection of this item did have candidate arms above it, at 30.19% to
30.54%, so the limit is no longer reached because the base's line-emission
refactor lowered the level of both arms; the rise within a pair is unaffected.

### Reconciling the share with the 5.2.1 forecast

5.2.1 forecast that removing the detector would move the share from 29.9414% to
30.9686%, a rise of 0.9686 points, and labelled that a conditional forecast
because no capture existed with the call absent. This collection measures a
control median of 28.1409% and a candidate median of 29.3436%, a rise of 1.2027
points; the three per-pair rises are 0.68, 1.91, and 1.15 points.

The forecast is low by 0.23 points on the rise. On the level it is high by 1.80
points for the control and 0.60 points for the candidate, because both arms sit
below 5.2.1's 29.9414% reference. That reference came from a different
collection, on a different pair of revisions, so the level is not comparable
across collections; the rise is, because each pair is measured on the same
stacks, with the same classifier, and differs only in `_observability.py`.

Both arms fell by about the same amount between the previous collection of this
item and this one — 1.31 points for the control and 1.13 for the candidate, and
the two collections' per-arm ranges do not overlap. The arms share the same
base, and the base changed in this item's rebase: the per-line emission path
moved into `_LineEventEmitter` and `_consume_stream_with_lines` moved into its
own module, which changes the denominator the shared rule counts. The level
shift is therefore attributable to that refactor rather than to either arm, and
the forecast's own reference level belongs to the collection that preceded it.

An earlier collection of this item measured a rise of 2.30 points and explained
it as the forecast counting only the detector's leaf frames while the
denominator loses every stack that reached it. Neither this collection nor the
one before it reproduces that size — they measure 1.20 and 1.02 points — so the
explanation is withdrawn: with three pairs on the delivered revisions the rise
is 0.68 to 1.91 points across the three pairs, and the forecast's own figure
sits inside that range.

## Unprofiled paired rounds

Six ABBA pairs of the `cb` scenario (echo to a devnull sink, one stage, Python
backend, line callbacks, repeat count 1) were admitted under the load rule of
Decision D11: rounds 1, 3 and 5 ran control first and rounds 2, 4 and 6
candidate first, so a slow drift in host conditions cannot favour one arm.

| round | order | control (s) | candidate (s) | wall ratio | control CPU (s) | candidate CPU (s) | CPU ratio | load median control/candidate | host busy control/candidate |
| ----- | ----- | ----------- | ------------- | ---------- | --------------- | ----------------- | --------- | ----------------------------- | --------------------------- |
| 1     | cb    | 283.47      | 276.98        | 0.9771     | 281.62          | 274.35            | 0.9742    | 16.54 / 16.80                 | 0.540 / 0.549               |
| 2     | bc    | 279.68      | 282.16        | 1.0089     | 276.09          | 277.60            | 1.0055    | 14.98 / 14.81                 | 0.504 / 0.586               |
| 3     | cb    | 306.28      | 264.83        | 0.8647     | 301.18          | 260.34            | 0.8644    | 20.70 / 17.92                 | 0.640 / 0.546               |
| 4     | bc    | 276.21      | 251.33        | 0.9099     | 273.74          | 250.92            | 0.9166    | 12.87 / 14.39                 | 0.495 / 0.492               |
| 5     | cb    | 276.19      | 262.21        | 0.9494     | 273.01          | 260.94            | 0.9558    | 17.76 / 15.24                 | 0.555 / 0.538               |
| 6     | bc    | 267.72      | 243.71        | 0.9103     | 266.79          | 243.22            | 0.9117    | 12.34 / 11.28                 | 0.410 / 0.422               |

The wall-time geometric mean ratio is 0.935504, with a bootstrap 95% interval
of [0.897741, 0.974806] over 10,000 resamples (seed 20261105). The candidate is
6.4% faster at the geometric mean, and the interval's upper bound is 0.9748, so
this collection does not support a slowdown and clears the 1.05 tolerance.

The child's CPU time gives the same answer independently: a geometric mean
ratio of 0.936883 with an interval of [0.899205, 0.975167]. CPU time is 98.3%
to 99.8% of wall time in every arm, so the workload is CPU-bound and the two
quantities measure nearly the same thing; agreement between them is evidence
that the wall-time ratios were not produced by the scheduler giving one arm
more of the machine.

Round 2 is the only pair whose ratio exceeds 1, and it is retained. The
admission rule reads load and the host's busy share, never a ratio, so
discarding a pair after seeing its result would be selecting on the outcome.

The per-pair ratios span 0.8647 to 1.0089 — a spread far wider than the
geometric means suggest — because the pairs ran at different points in a host
load series that moved by a factor of two. That is why the interval, not any
single pair, is the reported uncertainty: five of the six pairs favour the
candidate, and the one that does not is the pair whose candidate arm ran under
the larger host busy share, 0.586 against 0.504.

### Admission under Decision D11

Decision D9's ceiling of 20 was unreachable during the first collection of this
item: the host's one-minute load average stayed between 20 and 45 for more than
six hours while other agents' builds ran, and four round-1 attempts under D9
were rejected at sampled maxima of 22.04/29.53, 28.43/36.55, 44.98/45.45, and
39.56/42.28 (`unprofiled/superseded/timing-attempts.tsv`, pass 3). An eighth
attempt that waited 45 minutes for a quiet window before each pair admitted
nothing.

Decision D11 replaces the absolute ceiling with the properties the ceiling was
a proxy for, measured directly: a burst inside one arm, and an arm-to-arm
imbalance. A pair is admitted when both arms exit 0 with a result document, the
pair's pooled median load — the mean of the two arm medians, as the rule
computes it — is at most 45, the arms' load medians differ by at most 20% of
the pooled median, the arms' measured host-busy shares differ by at most 10
points, and no arm's maximum exceeds the pooled median by more than 15. The
rule reads `/proc/loadavg`, `/proc/stat`, and exit status only. Its final form
was settled during the first collection, and this collection applied it
unchanged to all thirteen recorded attempts; each round's earliest qualifying
attempt is the one adopted. Seven attempts fail the rule and are not used:

| round | attempt | load medians control/candidate | maxima control/candidate | host busy control/candidate | reason                                                                      |
| ----- | ------- | ------------------------------ | ------------------------ | --------------------------- | --------------------------------------------------------------------------- |
| 1     | 1       | 34.22 / 25.60                  | 48.90 / 29.04            | 0.789 / 0.689               | medians differ by 8.62; control burst                                       |
| 2     | 1       | 15.75 / 12.37                  | 22.88 / 18.31            | 0.541 / 0.481               | medians differ by 3.38                                                      |
| 3     | 1       | 30.10 / 21.41                  | 51.39 / 26.13            | 0.774 / 0.621               | medians differ by 8.69; control burst; busy shares differ by 15.3 points    |
| 3     | 2       | 18.41 / 11.57                  | 29.94 / 16.08            | 0.586 / 0.397               | medians differ by 6.84; busy shares differ by 19.0 points                   |
| 3     | 3       | 12.98 / 28.88                  | 15.94 / 47.99            | 0.501 / 0.761               | medians differ by 15.90; candidate burst; busy shares differ by 26.0 points |
| 6     | 1       | 26.10 / 10.98                  | 44.05 / 19.45            | 0.702 / 0.451               | medians differ by 15.12; control burst; busy shares differ by 25.1 points   |
| 6     | 2       | 9.03 / 14.01                   | 13.63 / 20.14            | 0.401 / 0.462               | medians differ by 4.98                                                      |

The rejected attempts cluster in rounds 1, 3 and 6, which are the rounds that
had to be retried. Every one of them fails the arm-to-arm median tolerance, and
five also breach the rule on an arm burst or the busy-share gap; the reason
column names each clause the rule emitted. `d11-attempts.tsv` records every
attempt with the verdict of the rule, `d11-admissions.json` that verdict in
machine-readable form together with the adopted attempt per round, and
`load-samples.json` the full five-second load series and CPU record for every
adopted arm. The rejected attempts' series are not committed — `.gitignore`
excludes `*.log` — but their maxima, medians, and busy shares are in the table
above and in the TSV, which is what the rule reads.

Round 3's adopted attempt 4 is the closest call in the set: its arms differ by
9.4 points of host busy share, just inside the 10-point tolerance, with the
control arm the busier of the two and also the slower. It is the pair with the
largest candidate-favouring ratio, 0.8647, so the imbalance runs against the
direction of the result: the control arm was measured under the heavier load
and still came out slower.

One further pair was run before round 1 and discarded by protocol as the
warm-up, to prime the fixture cache before any measurement; it is excluded
before its result exists and is not part of the accepted set. Its documents are
committed under `unprofiled/warmup-<arm>.*` so the exclusion can be checked
rather than assumed: control 278.55 s and candidate 370.05 s, a ratio of 1.328.
The candidate arm of that pair ran at a host busy share of 0.801 against the
control arm's 0.566, so the rule would have rejected the pair as a measurement
even ignoring its position in the schedule. It is excluded twice over, and its
ratio is not evidence about the change.

## Microbenchmark

The `observe-dispatch` group in `benchmarks/test_stream_microbenchmarks.py`
dispatches 1,000 events per benchmark call, so each figure below is the cost of
1,000 dispatches and dividing by 1,000 gives the per-event cost. The control
worktree predates the group, so the candidate's benchmark file was copied into
it; the file imports `_emit_exec_event` and times it, and adds no production
code, so the control run measures the control dispatcher.

Sixteen runs were made in each tree: the primary pair, plus fifteen repeats
that alternate which tree runs first. The median of each tree's sixteen runs,
with the observed range in brackets, in microseconds per 1,000 dispatches:

| benchmark          | control (16 runs)              | candidate (16 runs)            | candidate/control |
| ------------------ | ------------------------------ | ------------------------------ | ----------------- |
| sync hook (`None`) | 433.23 (413.75 – 765.80)       | 167.25 (156.73 – 231.16)       | 0.386             |
| `async def` hook   | 4,372.03 (4,041.63 – 7,886.48) | 4,277.00 (3,998.02 – 5,768.41) | 0.978             |

The control's sync hook costs 433 ns per event and the candidate's 167 ns, a
2.59x reduction, or about 266 ns saved per event. That is the per-line cost
this item removes, measured directly rather than inferred from a profile. The
medians rather than the means carry the comparison: three of the control's sync
runs landed above 600 µs while the other thirteen span 413.75 to 497.02 µs, and
that arm's mean is pulled upward by the three.

The `async def` hook does not move. Its medians differ by 2.2% in the
candidate's favour and the two sixteen-run ranges overlap across almost their
whole length (4,041.63 to 7,886.48 against 3,998.02 to 5,768.41), so this is
unchanged within the noise of a shared host. The path is dominated by
`asyncio.Task` creation at roughly 4.3 µs per event, which is why Decision D8
declined to profile it at full scale. `microbench-repeats.json` holds every run.

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
| every anchor weight at least 14,000                   | met; the smallest is 27,660 (candidate, round 1)          |
| `cb` ratio's 95% interval upper bound at most 1.05    | met; the upper bound is 0.9748 (CPU time 0.9752)          |
| construction share re-measured on the same stacks     | 28.1409% control median, 29.3436% candidate median        |
| microbenchmark of the dispatched group                | sync hook 2.59x faster; `async def` hook 0.978, unchanged |

The construction share is reported, not judged: it is 5.2.1's gate, and a rise
is what that metric does when work outside the counted constructor is removed
(Constraint C10). The detector's own sampled weight, which is 5.2.2's subject,
falls to zero in every candidate capture.

## Superseded collections

Two earlier collections of this item's evidence are preserved in the branch
history, and neither is quoted as a result.

The first measured a control of `ccc970116` and a candidate of `2dd643995`. It
was replaced because it measured intermediate revisions rather than the
revisions delivered by the pull request: the candidate's dispatcher was
refactored into `_emit_observe_hook` after `2dd643995`, so a measurement of
that revision does not describe the delivered hot path.

The second measured a control of `e170f908f` and a candidate of `064d69405`. It
was replaced by the same argument at one remove: the pull request was rebased
onto a base twelve commits ahead, which moved the per-line emission path into
`_LineEventEmitter` and `_consume_stream_with_lines` into its own module, so
neither arm of that collection measured code the branch still contains. The
refactor changed the denominator the construction share is computed from, and
both arms of that collection read about a point and a quarter higher than their
counterparts here (see "Reconciling the share with the 5.2.1 forecast").

Every profiled capture, every unprofiled pair, and all sixty-four
microbenchmark runs in this directory were collected against `1d9e7d37f` and
`0df4bbab6`, which is why the directory's files carry those revisions.

The first collection's records survive in `unprofiled/superseded/`: its attempt
table (`superseded/timing-attempts.tsv`, cited above for the D9 rejections),
its summary documents, and the four attempt pairs whose result documents the
re-collection did not overwrite. The second collection's records survive only
in the branch history at the commits that added them; its result documents were
deleted when this collection replaced them with the same file names, and no
figure here depends on any of them.

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
- `profiling/5-2-2-observe-hook-dispatch/unprofiled/superseded/` — the first
  collection's surviving records, described under "Superseded collections".
- `profiling/5-2-2-observe-hook-dispatch/microbench-control.json` and
  `microbench-candidate.json` — the primary microbenchmark pair for each tree.
- `profiling/5-2-2-observe-hook-dispatch/microbench-repeats.json` — all
  sixty-four microbenchmark runs, each with its tree, benchmark, mean, median,
  and round count.
