# Tee hot-path line-event emission (5.2.1)

This document records the measurement for roadmap item 5.2.1, which hoists the
invariant execution-event fields out of the per-line observation path. It
supplements
[`tee-hotpath-read-size-sweep-2026-08-29.md`](tee-hotpath-read-size-sweep-2026-08-29.md)
and keeps the raw wall-time samples and every N, D, and percentage reviewable
in Markdown.

## Result

The hoist is implemented, correct, and materially faster. It **misses** roadmap
item 5.2.1's revised construction-share threshold of 28%.

| Quantity                                        | Control (`01ec41bd`) | Candidate (`f4d1010a`) | Change         |
| ----------------------------------------------- | -------------------- | ---------------------- | -------------- |
| construction share, median of three pairs       | 34.2928%             | **29.9087%**           | −4.3841 points |
| wall time, median, profiled callback scenario   | 260.97 s             | 180.16 s               | **−30.96%**    |
| wall time, median, unprofiled callback scenario | 242.534 s            | 178.789 s              | **−26.28%**    |

Every candidate capture exceeds the 28% bar, the smallest by 1.8991 points. The
candidate's dispersion is 0.0423 points across three matched pairs against a
two-percentage-point tolerance, so the miss is a stable property of the
implementation rather than an unstable observation. The control misses the same
bar by 6.04 to 7.35 points, which is expected: the control predates the hoist.

**Why the threshold is missed, in one sentence.** The share is `N/D` and the
hoist shrinks both terms; it removed 39.8% of N and 31.0% of D, where the
projection the bar was sited on assumed N would fall 39.5% but D only 13.7%.
The numerator behaved as predicted and the denominator shrank more than twice
as far, and it is the smaller denominator that leaves the surviving numerator
at a higher share. See "Why the share fell short of the projection".

**The measurement does not say the hoist is worthless.** It says a
percentage-of-total-work gate does not capture "the hot path got faster" for a
change whose whole effect is to shrink the total. Every correctness and
regression criterion passes; see "Acceptance status".

## Protocol and environment

Three matched control/candidate profile pairs, each with one worker repeat,
collected sequentially with no gate or competing profile running alongside.
Control and candidate order alternates per round. One unprofiled warm-up per
variant precedes collection. The same shared interpreter (CPython 3.14.4) runs
both variants, and `cuprum` is imported from the current working directory
rather than from `site-packages`, so a worktree checkout selects the variant.

- profiler: `py-spy 0.4.2`, `--format raw --rate 100`;
- backend: `--backend python` (isolates Python tuning from the extension);
- read size: `--read-size 65536`, per the read-size sweep;
- worker repeat count: 1;
- control revision: `01ec41bd56b5968e7b9b5ec205ba82ffdbe55724`;
- candidate revision: `f4d1010aaf352a4dd6f549f6f602dd29bc329c4d`, the revision
  recorded in the collection log. The branch tip at the time of writing is
  `94ebcda1b6dbf075cae7f829bd2c964f8111871f`; `git diff f4d1010a..94ebcda1`
  touches only the execplan, so the production code and the classifier under
  measurement are byte-identical between the two. The captures are attributed to
  `f4d1010a` here because that is what ran;
- classifier: `benchmarks/_line_event_profile_classifier.py`, SHA-256
  `0e1c6e135841e82f…`;
- rules: `docs/profiling/5-2-1-line-event-emission/classifier-rules.json`,
  SHA-256 `574da3fe7d865a4e…`.

Fixtures, byte-identical to the read-size sweep's:

- wrap-76 base64: 2,175,740,011 bytes, SHA-256
  `51394f18e57972a681a2eb97c7c477d02d1d15b175247f518a60c331319774cc`;
- unwrapped base64: 2,147,483,648 bytes, SHA-256
  `15e4356ae06fa10a81a3b4ba9e7b0e4437961a21752f982582371aa88389f914`.

The wrap-76 fixture drives the callback scenario; the unwrapped fixture drives
both no-callback controls, matching `benchmarks/tee_profile_scenarios.py`.

### Profiled capture command

```bash
py-spy record --format raw --rate 100 --output <dir>/stacks.folded \
  -- <python> -m benchmarks.tee_profile_worker \
  --fixture dist/fixtures/seed12345-wrap76.b64 --stages 1 \
  --mode echo --sink-kind devnull --line-callbacks --backend python \
  --repeat-count 1 --read-size 65536 \
  --output <dir>/worker-result.json
```

Classification of the resulting capture:

```bash
python -m benchmarks.summarize_line_event_profile \
  <dir>/stacks.folded --rules docs/profiling/5-2-1-line-event-emission/classifier-rules.json \
  --output <dir>/construction-share.json
```

### Unprofiled command

The same worker invocation without py-spy, and for the no-callback controls
without `--line-callbacks` and with `--mode tee` / `--mode echo` as the
scenario requires, against `dist/fixtures/seed12345-nowrap.b64`.

## Profiled pairs

`D` is the sum of sample weights of stacks containing
`_consume_stream_with_lines`; `N` is those also containing a matched
construction frame, counted once per stack. Every capture reports
`unresolved_frames: {}`, so no classifier rule drifted and no run is
inconclusive.

| capture      | py-spy samples | parent | D     | N     | share    | wall (s) | load at start     |
| ------------ | -------------- | ------ | ----- | ----- | -------- | -------- | ----------------- |
| r1-control   | 26185          | 26184  | 25726 | 9095  | 35.3533% | 260.97   | 11.40 21.09 21.91 |
| r1-candidate | 17912          | 17911  | 17539 | 5244  | 29.8991% | 180.16   | 5.59 12.31 18.02  |
| r2-control   | 26224          | 26223  | 25743 | 8828  | 34.2928% | 259.22   | 5.70 7.88 14.14   |
| r2-candidate | 18145          | 18141  | 17758 | 5317  | 29.9414% | 181.18   | 7.58 9.90 16.04   |
| r3-control   | 31311          | 31310  | 29722 | 10117 | 34.0388% | 306.38   | 4.11 5.72 11.70   |
| r3-candidate | 17740          | 17737  | 17413 | 5208  | 29.9087% | 175.41   | 8.25 16.96 15.89  |

_Table 1: Per-capture counts and shares, in collection order._

|           | median share | range             |
| --------- | ------------ | ----------------- |
| control   | 34.2928%     | 1.3145 points     |
| candidate | **29.9087%** | **0.0423 points** |

_Table 2: Dispersion. The candidate's range is roughly 1/47 of the two-point
tolerance._

All six captures report `stdout_line_count` of exactly **28256364** with
`exit_code` 0 and `read_size` 65536, so every run performed identical work.

Two captures carry a non-zero py-spy exit status (r2-control, r3-candidate).
Both are benign and were checked rather than assumed: each log ends
`Wrote raw flamegraph data … Samples: <n> Errors: 0` followed by
`Error: No child process (os error 10)`, which is py-spy losing its target
during teardown after the worker exited. The captures are complete and classify
cleanly.

## Unprofiled paired rounds

Five rounds per scenario, alternating order, never pooled across scenarios.

| scenario    | control median (s) | candidate median (s) | delta   |
| ----------- | ------------------ | -------------------- | ------- |
| `cb`        | 242.534            | 178.789              | −26.28% |
| `echo-nocb` | 2.156              | 2.140                | −0.70%  |
| `tee-nocb`  | 4.688              | 3.306                | −29.49% |

_Table 3: Median wall times and percentage change. A negative delta means the
candidate completed sooner. No scenario is slower, so the 5% regression
tolerance passes everywhere._

Raw samples, in collection order:

| scenario    | variant   | wall times (s)                          |
| ----------- | --------- | --------------------------------------- |
| `cb`        | control   | 247.528 240.677 242.534 240.173 254.140 |
| `cb`        | candidate | 202.873 178.789 175.072 167.463 210.943 |
| `echo-nocb` | control   | 3.489 2.156 2.117 2.148 2.176           |
| `echo-nocb` | candidate | 2.185 2.146 2.129 2.140 2.129           |
| `tee-nocb`  | control   | 5.614 4.658 4.953 4.688 4.462           |
| `tee-nocb`  | candidate | 3.193 3.326 4.171 3.306 3.032           |

_Table 4: Every wall-time sample, retained rather than summarized away._

### `tee-nocb`'s −29.49% is a host artefact

This is the one timing that requires explanation, and it was tested rather than
excused. `tee-nocb` runs with `with_line_callbacks=False`. In both revisions
`_compose_line_callbacks` returns `None` before any hoisted code is reached
(control `_line_callbacks.py:102`, candidate `_line_callbacks.py:208`), and the
entire production diff between the two revisions is 119 lines in that one file,
so the change cannot reach this path.

Re-measured alone on a quieter host — 8 alternating rounds at load 2.7–3.2,
against the collection's 5.95–7.64:

|           | median (s) | min (s) | max (s) |
| --------- | ---------- | ------- | ------- |
| control   | 4.574      | 4.517   | 4.665   |
| candidate | 4.582      | 4.497   | 4.668   |
| **delta** | **+0.18%** |         |         |

_Table 5: Isolated re-measurement of `tee-nocb`._

The two distributions overlap completely here, where Table 3's were disjoint
(control minimum 4.462 above candidate maximum 4.171). The apparent separation
was host load. This is recorded because it is the one large _favourable_ result
in a scenario the change cannot touch, which is the direction that most invites
being reported unchecked.

`echo-nocb`'s −0.70% is the honest null, and its agreement with the isolated
re-measurement is the evidence that the no-callback path is genuinely
unaffected: two independent no-callback scenarios both land within a percentage
point of parity.

## Why the share fell short of the projection

The share **did** fall: 34.2928% to 29.9087%, a 4.3841-point improvement, on
every pair. What it did not do is reach the bar. The threshold was revised from
10% to 28% before implementation, on a projected post-hoist share of 24.35% to
26.91%, and the measurement lands above that range. Phrasing this as "the share
did not fall" would be wrong; the projection over-predicted how far it would.

The projection subtracted the same _absolute_ frame weights from numerator and
denominator, which assumes each removed sample was as likely to be counted by
the numerator as by the whole subtree. Measured on the r2 pair, with one rule
set at one classifier revision:

| term                | control | candidate | removed  | fell by |
| ------------------- | ------- | --------- | -------- | ------- |
| D (consume subtree) | 25743   | 17758     | **7985** | 31.0%   |
| N (numerator)       | 8828    | 5317      | **3511** | 39.8%   |
| N-excluded (D − N)  | 16915   | 12441     | **4474** | 26.4%   |

_Table 6: What the hoist removed from each term. The three rows reconcile:
`3511 + 4474 = 7985`._

The projection's assumption held only approximately: N fell by 39.8% against
D's 31.0%, so the share moved the right way, but a 28% bar requires the
candidate's denominator to be at least `N / 0.28`. Every candidate pair falls
short of that by a similar margin:

| pair         | N    | D     | share    | D required for 28% | shortfall |
| ------------ | ---- | ----- | -------- | ------------------ | --------- |
| r1-candidate | 5244 | 17539 | 29.8991% | 18728.6            | −1189.6   |
| r2-candidate | 5317 | 17758 | 29.9414% | 18989.3            | −1231.3   |
| r3-candidate | 5208 | 17413 | 29.9087% | 18600.0            | −1187.0   |

_Table 7: The candidate misses the bar by a consistent ~1200 denominator
samples, or 6.3 to 6.9% of its own D._

Reconstructing the projection on the _final_ rule set, rather than against the
pre-hoist rule set it was written with, isolates the error. Had the removals
been equal in absolute weight, the share would be well under the bar:

| pair | if removal were equal | actual   |
| ---- | --------------------- | -------- |
| r1   | 23.9726%              | 29.8991% |
| r2   | 23.9160%              | 29.9414% |
| r3   | 20.9890%              | 29.9087% |

_Table 8: The equal-removal assumption, applied to the final rule set. It
predicts a pass on every pair, so the assumption — not the bar's arithmetic —
is what failed._

**The removals are not equal, and the direction is systematic.** Sample weights
are removed from the numerator only when the removed work sat _below_ the
constructor call in a stack that reached it. The three largest removals are
exactly the frames item 5.2.1 was scoped to eliminate, and none of them ever
had the generated constructor below it:

| leaf frame                                   | control | candidate | removed  |
| -------------------------------------------- | ------- | --------- | -------- |
| `emit (cuprum/_pipeline_types.py)`           | 4471    | 0         | **4471** |
| `_event_details (cuprum/_line_callbacks.py)` | 1212    | 0         | **1212** |
| `argv_with_program (cuprum/sh/safe_cmd.py)`  | 337     | 0         | **337**  |

_Table 9: The three largest removals that the numerator never counted. All are
now exactly zero. They account for 6020 of the 6503 gross removal; the net
N-excluded change is 4474 because other frames grew by 2029 as the hoist moved
work into `emit_line` and its callees. The three are therefore the largest
sources of the change, not its whole._

A representative control stack for the largest source ends
`emit_line (cuprum/_line_callbacks.py:109)` →
`emit (cuprum/_pipeline_types.py:135)`. The sample lands _inside_ `emit` while
it assembles the per-line payload, before the constructor is reached, so no
generated frame is on the stack and the numerator cannot count it. The
candidate's `_LineEventEmitter.emit_line` calls
`self.emit_event(ExecEvent(...))` directly, so that hop does not exist.
Removing a frame the numerator never counted lowers D while holding N, which
raises `N/D`.

**The measured fraction, for the record.** N fell by 39.8% and D by 31.0% on
r2; the projection assumed N would fall 39.5% and D only 13.7%. The numerator
estimate was good. The denominator estimate was low by more than a factor of
two, and that is the whole of the gap.

## What the residual numerator is

Decomposing the r2 numerator by innermost executing frame places **100.00%** of
it inside the generated `ExecEvent.__init__`, under both variants. No numerator
sample is attributed to a caller that merely has the constructor on its stack.

The consequence is general and worth stating plainly: because the numerator
counts only samples _inside_ the constructor, any optimization that removes
work reached before the constructor call lowers D while holding N, and
therefore **pushes the share up** against whatever it would otherwise have
been. Here that was a drag on a share that still fell, but the same mechanism
makes the metric non-monotonic in speed: on this classifier a strictly faster
emission path can score worse than a slower one. That is not a hypothetical,
because the roadmap's next item is exactly such a change.

This already has a named successor. Roadmap item 5.2.2 removes the per-line
`inspect.isawaitable` call, which is 589 samples, all D-only, in the candidate
capture. Removing it moves the share from 29.9414% to 30.9686% — a worse number
from work the roadmap explicitly wants done. Item 5.2.1's share gate therefore
cannot be the acceptance instrument for 5.2.2.

`ExecEvent` is declared `@dc.dataclass(frozen=True, slots=True)` with 27
fields, and the `frozen=True` guard dominates the constructor's cost:
`dataclasses` emits one `object.__setattr__` call per field, 27 full call
sequences in one function body. Measured on this host (3.14.4, 300k
repetitions):

| construction                                                      | ns/ctor | ratio to shipped |
| ----------------------------------------------------------------- | ------- | ---------------- |
| shipped — `@dc.dataclass(frozen=True, slots=True)`                | 1890.3  | 1.0000           |
| same fields without `frozen=True`                                 | 341.6   | 0.1807           |
| named-parameter `__init__` calling `object.__setattr__` per field | 1765.8  | 0.9342           |
| named-parameter descriptor `__init__`, slot setters hoisted       | 1278.3  | 0.6763           |

_Table 10: Construction cost of the shipped type and three alternatives._

The descriptor variant preserves the full dataclass protocol surface — 27 slots,
`FrozenInstanceError` on write, and `dc.fields`, `dc.replace`, `dc.asdict`,
`dc.astuple`, keyword construction, `pickle`, `copy`, `deepcopy`, hash and set
membership, and `repr` all verified equal to the shipped type. Applied to the
r2 counts it models a share of 22.42%; that figure is arithmetic on a
microbenchmark, not a capture, and is recorded as such.

`frozen=True` is pre-existing public API on `ExecEvent` (introduced with the
structured-event work, well before this branch) and three test suites assert
`FrozenInstanceError` on event writes, one of them a property test. Reopening
it is a public-API decision outside item 5.2.1's approved scope.

## Acceptance status

| Requirement                                  | Status      | Evidence                                                  |
| -------------------------------------------- | ----------- | --------------------------------------------------------- |
| R1 — invariant fields hoisted per stream     | met         | V1; no per-line `_EventDetails`, no per-line argv rebuild |
| R2 — payloads and hooks unchanged            | met         | V2–V4; behavioural scenarios                              |
| R3 — construction share at most 28%          | **NOT met** | 29.9087% median, minimum 29.8991%                         |
| R4 — Python-first tuning, unchanged dispatch | met         | scoped diff; unchanged dispatcher                         |

V5's regression tolerances all pass: `D ≥ 10000` in every capture (minimum
17413), candidate share range at most two points (0.0423), five unprofiled
rounds per scenario for the callback workload and both no-callback controls,
and no scenario slowed by more than 5%.

Item 5.2.1 remains blocked against R3. The measurement and its decomposition
are committed so the threshold decision rests on this artefact rather than on a
further projection.

## Machine-readable outputs

The full folded captures, worker results, and classifier outputs remain in the
gitignored `dist/` tree and in `/tmp/smoke521/v5/`. The fixtures are 2 GiB each
and are not committed; their checksums above identify them. The classifier
rules are committed at
[`profiling/5-2-1-line-event-emission/classifier-rules.json`](profiling/5-2-1-line-event-emission/classifier-rules.json).
