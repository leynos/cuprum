# 5.2.2 observe-hook dispatch profile evidence

Complete inputs and outputs for roadmap item 5.2.2's hot-path census. The
analysis and the verdict are in
[`docs/tee-hotpath-observe-hook-dispatch-5-2-2.md`](../../tee-hotpath-observe-hook-dispatch-5-2-2.md);
this directory holds the data that argument rests on, so a reader can
recalculate the census rather than trust the numbers in the report.

## Layout

```text
r{1,2,3}-control/       three pre-change captures, `ccc970116`
r{1,2,3}-candidate/     three post-change captures, `2dd643995`
unprofiled/             six load-admitted ABBA `cb` pairs and their evidence
unprofiled/attempts/    every attempt's result documents, admitted or not
```

Each capture directory carries the same eight committed files:

| file                      | contents                                         |
| ------------------------- | ------------------------------------------------ |
| `stacks.folded`           | the py-spy raw capture, folded-stack format      |
| `frame-census.json`       | the census result document for this capture      |
| `construction-share.json` | the 5.2.1 classifier's result for this capture   |
| `worker-result.json`      | the worker's own timings and validation record   |
| `variant.txt`             | variant, interpreter, package path, and revision |
| `pyspy-exit.txt`          | py-spy's exit status                             |
| `census-exit.txt`         | the census's exit status                         |
| `classifier-exit.txt`     | the classifier's exit status                     |

A collection run also produces `pyspy.log`, `census.log`, and `classifier.log`
beside these, but `.gitignore` excludes `*.log` (as it does everywhere in this
repository), so none is committed. Nothing in the analysis depends on a file a
reader cannot obtain: `census.log` and `classifier.log` repeat the JSON that is
committed, and `pyspy.log`'s load-bearing line — `Samples: <n> Errors: 0` —
reports the sum of the weights in the committed `stacks.folded`.

`variant.txt` is the provenance record. It names the variant, the interpreter
that ran it, the `cuprum` package directory the worker imported, and the
revision that directory resolved to, so a capture cannot be attributed to the
wrong tree. Absolute host paths are replaced with `<repo:control>` and
`<repo:candidate>`.

## Reproducing the census

Run from the repository root:

```sh
python -m benchmarks.summarize_hot_path_frames \
  docs/profiling/5-2-2-observe-hook-dispatch/r1-candidate/stacks.folded \
  --anchor emit_line@cuprum/_line_callbacks.py \
  --target isawaitable@inspect.py \
  --min-anchor-weight 14000 \
  --output /tmp/r1-candidate.json
```

Exit 0 means both target weights are zero and the anchor met the floor, 1 that
the target weights agree and are non-zero, and 2 that the input was malformed,
the anchor fell below the floor, or the two target weights disagreed. A
candidate capture is expected to exit 0 and a control capture to exit 1.

## Reproducing the classification

The 5.2.1 classifier is re-run on every capture so that the two items are
measured on the same stacks:

```sh
python -m benchmarks.summarize_line_event_profile \
  docs/profiling/5-2-2-observe-hook-dispatch/r1-candidate/stacks.folded \
  --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output /tmp/r1-candidate-share.json
```

Its exit status is recorded per capture but is not a 5.2.2 acceptance
criterion; 5.2.1's own gate is judged in
[`5-2-1-line-event-emission`](../5-2-1-line-event-emission/README.md).

## The anchor floor

The census can only report an absence it had the opportunity to observe. The
floor of 14,000 anchored samples is derived from the control's measured target
share: at the control's rate, 14,000 anchored samples make a surviving target
overwhelmingly likely to appear, so a candidate reading of zero is evidence
rather than silence. Every capture in this directory clears the floor, and the
per-capture `anchor_weight` is committed so the claim can be checked.

## The unprofiled timing pairs

`unprofiled/` holds the wall-clock comparison. Each variant ran one discarded
warm-up, then six `cb` runs (`echo-devnull-cb-s1-python`, repeat count 1) in
ABBA order — control first for rounds 1 to 3, candidate first for rounds 4 to 6
— so that a drift in host load affects both variants rather than one. Each run's
`wall_time_seconds` is in its committed result document, and
`timing-summary.json` records the paired ratios, their geometric mean, and the
10,000-resample bootstrap 95% interval.

This host is shared with other work, whose linker bursts push the one-minute
load average past 30. A wall-time pair measured during such a burst measures
contention, not the workload, so a pair is admitted only when neither arm's
sampled load average exceeds 20 — a rule that reads `/proc/loadavg` and never a
run's ratio. `timing-attempts.tsv` lists every attempt of both load-gated
passes with each arm's sampled maximum and whether the attempt was admitted;
`load-samples.json` holds the full five-second series behind those maxima, so
the rule can be re-applied. `unprofiled/attempts/` keeps each attempt's result
documents, including rejected ones, and the accepted attempt is copied to the
canonical `cb-rN-<arm>.*` names that `timing-summary.json` reads.

## The microbenchmark

`microbench-control.json` and `microbench-candidate.json` are the
`observe-dispatch` group from `make benchmark-micro` in each tree, and
`microbench-repeats.json` adds three alternating-order repeats plus the primary
run in one document. The group dispatches 1,000 events per benchmark call, so
every figure is per 1,000 dispatches; divide by 1,000 for the per-event cost.
The control worktree predates the group, so the candidate's benchmark file was
copied into it for the measurement. The file only imports `_emit_exec_event`
and times it, so the control run measures the control dispatcher.
