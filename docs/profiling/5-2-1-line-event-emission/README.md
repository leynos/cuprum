# 5.2.1 line-event-emission profile evidence

Complete inputs and outputs for roadmap item 5.2.1's construction-share gate
(V5). The analysis and the verdict are in
[`docs/tee-hotpath-line-event-emission-5-2-1.md`](../../tee-hotpath-line-event-emission-5-2-1.md);
this directory holds the data that argument rests on, so a reader can
recalculate N/D rather than trust the numbers in the report.

## Layout

```text
classifier-rules.json   the committed classification rules (the gate's input)
verdict.txt             the collection's summary table and its 28%-era verdict
r{1,2,3}-control/       three pre-hoist captures, `01ec41bd`
r{1,2,3}-candidate/     three post-hoist captures, `f4d1010a`
unprofiled/             five paired rounds per unprofiled scenario (30 runs)
reclassified-at-30/     the six captures re-judged at the revised 30% limit
```

Each capture directory carries the same seven committed files:

| file                      | contents                                          |
| ------------------------- | ------------------------------------------------- |
| `stacks.folded`           | the py-spy raw capture, folded-stack format       |
| `construction-share.json` | the classifier's result document for this capture |
| `worker-result.json`      | the worker's own timings and validation record    |
| `revision.txt`            | the resolved source SHA this capture ran against  |
| `variant.txt`             | `control` or `candidate`                          |
| `pyspy-exit.txt`          | py-spy's exit status                              |
| `classifier-exit.txt`     | the classifier's exit status                      |

A collection run also produces `pyspy.log` and `classifier.log` beside these,
but `.gitignore` excludes `*.log` (as it does everywhere in this repository),
so neither is committed. Both are recoverable from what is: `classifier.log` is
byte-identical to the committed `construction-share.json`, and `pyspy.log`'s
load-bearing line — `Samples: <n> Errors: 0` — reports the sum of the weights
in the committed `stacks.folded` (26224 for `r2-control`), with its teardown
error, where one occurred, quoted in the evidence report alongside the
committed exit status in `pyspy-exit.txt`. Nothing in the analysis depends on a
file a reader cannot obtain.

## Reproducing the classification

Run from the repository root:

```sh
python -m benchmarks.summarize_line_event_profile \
  docs/profiling/5-2-1-line-event-emission/r1-candidate/stacks.folded \
  --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output /tmp/r1-candidate.json
```

Exit 0 means the share is within the limit, 1 that it exceeds it, and 2 that
the input was malformed, insufficient, or had unresolved frames. A control
capture is expected to exit 1.

## The threshold, and why the directory holds two verdicts

The captures were collected against a 28% limit and missed it by 1.90 points
(`verdict.txt`). The limit was then revised to **30%** with user approval, on
those measurements, and the same six captures were re-classified
(`reclassified-at-30/`). Nothing was re-collected and no rule was changed:
every field of every result document is byte-identical between the two runs
except `limit_percent` and `status`. That is the check that the revision moved
the target rather than the measurement, and it is why both verdicts are kept
here rather than one overwriting the other.

## The observe-hook dispatch boundary

A rule's caller list names emission sites, but that alone does not exclude
every constructor reached *through* the emission path. An observe hook runs
below the dispatcher and therefore below every emission caller, so a hook that
builds its own dataclass per event would render as a generated `__init__` whose
nearest matching caller is still `emit_line` -- and it would be counted as
`ExecEvent` construction. `_crosses_hook_dispatch` in
`benchmarks/_line_event_profile_classifier.py` rejects such a candidate
structurally, matching `_emit_event` and `_emit_exec_event` on function *and*
module location so that a same-named frame elsewhere cannot trip the guard.

The guard changed no committed number, and that was measured rather than
assumed. Re-running the guarded classifier over all six captures reproduces
`reclassified-at-30/` byte for byte, and every measurement field is identical
to the committed per-capture `construction-share.json` -- the only differences
there are `limit_percent` and its derived `status`, from the approved 28% to
30% revision. The reason is visible in the captures themselves: no construction
frame appears below the dispatcher. The benchmark's own hook only increments a
counter, so every counted frame was already a direct call from an emission
caller.

The tightest pair is unchanged at r2: candidate 29.9414% against control
34.2928%, a 0.0586-point margin under the 30% limit.

## Redaction

Absolute host paths are replaced with `<repo>` and `<capture>` placeholders.
Frame names, line identifiers, sample weights, timings, and SHAs are preserved
verbatim; nothing that bears on the measurement was altered. Large binary
profiles and the wrap-76 fixture itself stay in the gitignored `dist/`.
