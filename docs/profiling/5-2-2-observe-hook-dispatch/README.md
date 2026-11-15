# 5.2.2 observe-hook dispatch profile evidence

Complete inputs and outputs for roadmap item 5.2.2's hot-path census. The
analysis and the verdict are in
[`docs/tee-hotpath-observe-hook-dispatch-5-2-2.md`](../../tee-hotpath-observe-hook-dispatch-5-2-2.md);
this directory holds the data that argument rests on, so a reader can
recalculate the census rather than trust the numbers in the report.

## Layout

```text
r{1,2,3}-control/         three pre-change captures of the base, `1d9e7d37f`
r{1,2,3}-candidate/       three post-change captures of the head, `0df4bbab6`
unprofiled/               six admitted ABBA `cb` pairs and their evidence
unprofiled/attempts/      every attempt of this collection, admitted or not
unprofiled/superseded/    what remains of the first collection's records
```

The branch was rebased onto `ebb8c7c25` after these captures were taken. The
delta between the two bases touches only Rust sources and documentation, so the
captures still describe the delivered branch; the report's
[superseded collections](../../tee-hotpath-observe-hook-dispatch-5-2-2.md#superseded-collections)
section lists the seven paths.

Each capture directory carries the same nine committed files:

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
| `pyspy-diagnostic.txt`    | py-spy's own report, redacted to what carries    |

A collection run also produces `pyspy.log`, `census.log`, and `classifier.log`
beside these, but `.gitignore` excludes `*.log` (as it does everywhere in this
repository), so none is committed. Nothing in the analysis depends on a file a
reader cannot obtain: `census.log` and `classifier.log` repeat the JSON that is
committed, and `pyspy-diagnostic.txt` carries py-spy's load-bearing lines from
`pyspy.log` — `Samples: <n> Errors: 0`, which reports the sum of the weights in
the committed `stacks.folded`, and whatever error line py-spy printed after it.
The host path inside those lines is replaced with `<capture>`; nothing else is
edited, so the diagnostic can be checked against the counts it explains.

`variant.txt` is the provenance record. It names the variant, the interpreter
that ran it, the `cuprum` package directory the worker imported, and the
revision that directory resolved to, so a capture cannot be attributed to the
wrong tree. Absolute host paths are replaced with `<repo:control>` and
`<repo:candidate>`. It also records the one-minute load average immediately
before and after the capture.

The `r2-candidate` capture reports py-spy exit status 1 because py-spy's
teardown lost its child during reaping. Its `pyspy-diagnostic.txt` holds both
lines that explain the status: py-spy wrote the capture and reported
`Samples: 60266 Errors: 0`, and then printed
`Error: No child process (os error 10)` while reaping. The capture itself
completed, and the sum of the weights in the committed `stacks.folded` is
60266, so the exit status records a known reaping race rather than a failed
capture. Every other capture exits 0 and its diagnostic holds the single
`Samples: <n> Errors: 0` line. The race follows teardown timing rather than a
variant — the earlier collection saw it on the round-1 pair instead.

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
the target weights agree and are non-zero, and 2 that the input or the command
line was malformed, the floor was not positive or the anchor fell below it, or
the two target weights disagreed. A candidate capture is expected to exit 0 and
a control capture to exit 1.

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

Every capture's `stacks.folded` also contains exactly one line whose stack is
empty — a space followed by the sample weight, for example `' 34'`. py-spy
emits it when a sample carries no resolvable frames. The census drops it, so
the folded weight sum exceeds the census's `total_weight` by exactly that
line's weight, which is between three and eight samples across these six
captures. Any other line the parser cannot read makes the census report status
2 instead of a weight, so the difference is the check that the census read the
whole file and discarded nothing else.

## The unprofiled timing pairs

`unprofiled/` holds the wall-clock and CPU-time comparison. Each variant ran
one discarded warm-up, then six `cb` runs (`echo-devnull-cb-s1-python`, repeat
count 1) in ABBA order — control first for rounds 1, 3 and 5, candidate first
for rounds 2, 4 and 6 — so that a drift in host conditions affects both
variants rather than one. The accepted set is the third collection; the two
earlier ones, on revisions the branch no longer contains, are described in
[`docs/tee-hotpath-observe-hook-dispatch-5-2-2.md`](../../tee-hotpath-observe-hook-dispatch-5-2-2.md#superseded-collections).

Each admitted pair contributes six files per arm under the canonical
`cb-rN-<arm>` names: `.json` (the worker's result document, which records
`wall_time_seconds`), `.cpu.json` (the child's CPU time and the host's busy
share, measured by `cpuwrap.py`), `.load.log` (the five-second one-minute load
series), `.exit`, `.log`, and nothing else. `timing-summary.json` and
`timing-summary.log` hold the paired ratios, their geometric means, and the
10,000-resample bootstrap 95% intervals for both quantities.

`/usr/bin/time` is not installed on this host, so `cpuwrap.py` runs the worker
under `subprocess.call` and reports `resource.getrusage(RUSAGE_CHILDREN)`,
which is the child's CPU time exactly, together with the host's aggregate busy
share from `/proc/stat` across the same window. CPU time is reported because a
wall time on a host shared with other tenants mixes the workload's cost with
the scheduler's decisions; the CPU time does not.

### Admission

A pair is admitted under Decision D11 when both arms exit 0 and produce a
result document, the pair's pooled median load — the mean of the two arm
medians — is at most 45, the arms' load medians differ by at most 20% of the
pooled median, the arms' measured host-busy shares differ by at most 10 points,
and no arm's sampled maximum exceeds the pooled median by more than 15. The
rule reads `/proc/loadavg`, `/proc/stat`, and exit status, and never a wall
time, a CPU time, or a ratio, so it cannot select on the result. Its final form
was settled during the first collection and this collection applied it
unchanged to every attempt through `reeval.py`'s logic, recorded in
`d11-admissions.json`; each round's earliest qualifying attempt is the one
adopted.

`d11-attempts.tsv` lists every attempt of this collection with each arm's
maximum and median sampled load, its host-busy share, and the verdict of the
rule in force when it ran. `d11-admissions.json` holds the final rule's verdict
for every attempt and the adopted attempt per round, and `load-samples.json`
holds the full five-second series and CPU record for every adopted arm, so the
rule can be re-applied from committed files alone. `unprofiled/attempts/` keeps
each attempt's `.json`, `.exit`, and `.cpu.json` under `cb-rN-<arm>.aM` names,
including rejected attempts, and an admitted attempt is copied to the canonical
`cb-rN-<arm>.*` names that `timing-summary.json` reads. Each attempt also
produces a `.log` and a `.load.log`, which `.gitignore` excludes; the load
series that matters for the rule is in `d11-attempts.tsv`'s statistics and, for
adopted arms, in full in `load-samples.json`.

`unprofiled/warmup-<arm>.*` holds the discarded warm-up pair, which ran before
round 1 under the same protocol and is not part of the accepted set.

## The superseded collections

`unprofiled/superseded/` holds what is left of the first collection, which
measured control `ccc970116` and candidate `2dd643995` under Decision D9. It
contains `timing-attempts.tsv` (that collection's attempt table, which the
report cites when it explains why D9's ceiling of 20 could not be met),
`load-samples.json` and `timing-summary.json` (its summary documents), and
`attempts/` (the four attempt pairs whose result documents the re-collection
did not overwrite). The re-collection reused every other file name in
`unprofiled/`, so the rest of that collection's result documents survive only
in the branch history. None of its numbers is quoted as a result.

A second collection, on control `e170f908f` and candidate `064d69405`, was
replaced when the branch was rebased onto a base twelve commits ahead. Its
files were deleted rather than archived, because every one of them measured a
tree the branch no longer contains; it survives only in the branch history, and
nothing here depends on it.

## The microbenchmark

`microbench-control.json` and `microbench-candidate.json` are the primary
`observe-dispatch` group run from `make benchmark-micro` in each tree, and
`microbench-repeats.json` holds all sixty-four runs — that primary pair plus
fifteen alternating-order repeats — each with its tree, benchmark, mean,
median, and round count. The group dispatches 1,000 events per benchmark call,
so every figure is per 1,000 dispatches; divide by 1,000 for the per-event
cost. The control worktree predates the group, so the candidate's benchmark
file was copied into it for the measurement. The file only imports
`_emit_exec_event` and times it, so the control run measures the control
dispatcher.

These runs used the project virtual environment's CPython 3.14.7 rather than
the 3.14.4 interpreter the profiler and the `cb` pairs used, because
`pytest-benchmark` is installed there. Both variants ran on the same
interpreter, so the comparison is within-run; the absolute figures are not
comparable with the profiled captures.
