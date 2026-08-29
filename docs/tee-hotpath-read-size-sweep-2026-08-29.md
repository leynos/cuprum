# Tee hot-path read-size sweep (2026-08-29)

This document records the fresh measurement used to select Cuprum's
pure-Python stream read size for roadmap item 5.1.1. It supplements the
historical profiling baseline in
[`tee-hotpath-profiling-baseline-2026-06-12.md`](tee-hotpath-profiling-baseline-2026-06-12.md)
and keeps the raw wall-time samples reviewable in Markdown.

## Result

The selected value is **65536 bytes (64 KiB)**. The gated
`tee-devnull-nocb-s1` scenario improved by 22.9997% against its same-session
4096-byte control. The 95% paired-bootstrap interval was 22.5508% to 23.2037%,
so the interval clears the 20% improvement requirement. The 65536-byte value
is also the smallest candidate at the measured one-read-per-pipe-full
call-count floor.

All regression scenarios improved at the selected value. The largest measured
line-callback change was a 1.9721% improvement; therefore no scenario exceeded
the 5% regression tolerance.

| Scenario | 4096-byte median (s) | 65536-byte median (s) | Median improvement | 95% paired-bootstrap interval |
| -------- | -------------------- | ---------------------- | ------------------ | ----------------------------- |
| `tee-devnull-nocb-s1` | 11.609 | 8.951 | 22.9997% | 22.5508% to 23.2037% |
| `capture-devnull-nocb-s1` | 9.947 | 8.767 | 11.8415% | 11.7840% to 12.1559% |
| `echo-devnull-nocb-s1` | 4.415 | 2.112 | 51.9213% | 51.8182% to 52.4792% |
| `echo-textblackhole-nocb-s1` | 4.181 | 2.375 | 43.3911% | 42.3904% to 43.9327% |
| `echo-pty-nocb-s1` | 27.638 | 24.383 | 11.5028% | 11.2501% to 12.0639% |
| `echo-devnull-cb-s1` | 448.618 | 439.892 | 1.9721% | 1.8433% to 2.1604% |

_Table 1: Median wall times and paired improvements. A positive improvement
means the 65536-byte candidate completed sooner._

## Protocol and environment

Each scenario ran 15 rounds. The read sizes were visited in a random order in
each round. Each sample contains three measured subprocess repeats after the
harness warm-up. The five-point primary scenarios therefore contain 75
samples each; the two-point regression scenarios contain 30 samples each.

The paired improvement for a round is
`(control_seconds - candidate_seconds) / control_seconds`. The interval is the
percentile interval from 100,000 bootstrap resamples of the 15 paired round
improvements, using `random.Random(20260829)` for reproducibility. Medians are
reported because the acceptance criterion is median wall time, while the
bootstrap retains the within-round pairing created by the randomized order.

The sweep used the release-built extension, the repository's CPython 3.13.13
environment, and these deterministic fixtures:

- unwrapped base64: 2,147,483,648 output bytes, raw seed input 1,610,612,736
  bytes, SHA-256
  `15e4356ae06fa10a81a3b4ba9e7b0e4437961a21752f982582371aa88389f914`;
- wrap-76 base64: 2,175,740,011 output bytes, the same raw seed input, SHA-256
  `51394f18e57972a681a2eb97c7c477d02d1d15b175247f518a60c331319774cc`.

The machine-readable run outputs remain in the gitignored directory
`dist/profiles/5-1-1-read-size-sweep-2026-08-29/`. The tables below are the
committed record of every wall-time sample, in occurrence order for each read
size. The scenario command used the existing `profile_tee_hotpath` driver with
`--profiler none`, `--rounds 15`, and `--randomize-order`.

## Primary sweep samples

### `tee-devnull-nocb-s1`

| Round | 4096 bytes (s) | 8192 bytes (s) | 16384 bytes (s) | 32768 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | -------------- | --------------- | --------------- | ---------------- |
| 1 | 11.701 | 10.476 | 9.775 | 9.379 | 9.260 |
| 2 | 11.621 | 10.378 | 9.592 | 9.198 | 9.037 |
| 3 | 11.666 | 10.276 | 9.557 | 9.146 | 8.979 |
| 4 | 11.597 | 10.392 | 9.561 | 9.158 | 8.948 |
| 5 | 11.636 | 10.252 | 9.494 | 9.223 | 8.957 |
| 6 | 11.555 | 10.256 | 9.438 | 9.128 | 8.951 |
| 7 | 11.569 | 10.238 | 9.470 | 9.127 | 8.884 |
| 8 | 11.620 | 10.172 | 9.489 | 9.095 | 8.914 |
| 9 | 11.611 | 10.209 | 9.470 | 9.247 | 8.906 |
| 10 | 11.609 | 10.235 | 9.487 | 9.129 | 8.972 |
| 11 | 11.565 | 10.186 | 9.499 | 9.178 | 8.880 |
| 12 | 11.573 | 10.297 | 9.515 | 9.097 | 8.964 |
| 13 | 11.568 | 10.260 | 9.532 | 9.139 | 8.922 |
| 14 | 11.573 | 10.220 | 9.500 | 9.107 | 8.912 |
| 15 | 11.637 | 10.217 | 9.459 | 9.128 | 8.958 |

_Table 2: Raw tee wall-time samples._

### `capture-devnull-nocb-s1`

| Round | 4096 bytes (s) | 8192 bytes (s) | 16384 bytes (s) | 32768 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | -------------- | --------------- | --------------- | ---------------- |
| 1 | 10.018 | 9.387 | 9.431 | 12.895 | 8.775 |
| 2 | 9.936 | 9.328 | 9.064 | 8.902 | 8.738 |
| 3 | 9.968 | 9.382 | 9.076 | 8.865 | 8.715 |
| 4 | 9.973 | 9.360 | 9.071 | 8.883 | 8.796 |
| 5 | 9.938 | 9.400 | 9.119 | 8.872 | 8.762 |
| 6 | 9.940 | 9.417 | 9.072 | 8.898 | 8.732 |
| 7 | 9.954 | 9.377 | 9.067 | 8.824 | 8.797 |
| 8 | 9.953 | 9.358 | 9.089 | 8.865 | 8.781 |
| 9 | 9.942 | 9.378 | 9.077 | 8.853 | 8.716 |
| 10 | 9.932 | 9.379 | 9.113 | 8.849 | 8.770 |
| 11 | 9.945 | 9.337 | 9.083 | 8.864 | 8.771 |
| 12 | 9.947 | 9.396 | 9.071 | 8.861 | 8.758 |
| 13 | 9.944 | 9.337 | 9.064 | 8.876 | 8.767 |
| 14 | 9.948 | 9.350 | 9.058 | 8.839 | 8.776 |
| 15 | 9.967 | 9.375 | 9.147 | 9.017 | 8.757 |

_Table 3: Raw capture-only wall-time samples._

## Regression sweep samples

The remaining scenarios compare only the old and selected values. They use the
same paired-round and bootstrap protocol described above.

### `echo-devnull-nocb-s1`

| Round | 4096 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | ---------------- |
| 1 | 4.375 | 2.105 |
| 2 | 4.426 | 2.135 |
| 3 | 4.403 | 2.124 |
| 4 | 4.456 | 2.141 |
| 5 | 4.427 | 2.130 |
| 6 | 4.384 | 2.112 |
| 7 | 4.402 | 2.089 |
| 8 | 4.385 | 2.113 |
| 9 | 4.451 | 2.140 |
| 10 | 4.395 | 2.142 |
| 11 | 4.440 | 2.089 |
| 12 | 4.418 | 2.108 |
| 13 | 4.420 | 2.100 |
| 14 | 4.415 | 2.082 |
| 15 | 4.404 | 2.102 |

_Table 4: Raw echo-to-`/dev/null` wall-time samples._

### `echo-textblackhole-nocb-s1`

| Round | 4096 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | ---------------- |
| 1 | 4.135 | 2.385 |
| 2 | 4.110 | 2.408 |
| 3 | 4.198 | 2.353 |
| 4 | 4.166 | 2.362 |
| 5 | 4.181 | 2.338 |
| 6 | 4.188 | 2.412 |
| 7 | 4.141 | 2.383 |
| 8 | 4.144 | 2.397 |
| 9 | 4.147 | 2.348 |
| 10 | 4.232 | 2.375 |
| 11 | 4.213 | 2.358 |
| 12 | 4.230 | 2.359 |
| 13 | 4.214 | 2.370 |
| 14 | 4.161 | 2.395 |
| 15 | 4.211 | 2.378 |

_Table 5: Raw text-sink wall-time samples._

### `echo-pty-nocb-s1`

| Round | 4096 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | ---------------- |
| 1 | 27.464 | 24.748 |
| 2 | 27.554 | 24.230 |
| 3 | 27.602 | 24.528 |
| 4 | 27.671 | 24.501 |
| 5 | 27.579 | 24.476 |
| 6 | 27.378 | 24.860 |
| 7 | 27.677 | 24.372 |
| 8 | 27.311 | 24.172 |
| 9 | 27.666 | 24.123 |
| 10 | 27.689 | 24.456 |
| 11 | 27.865 | 24.207 |
| 12 | 27.689 | 24.504 |
| 13 | 27.408 | 24.301 |
| 14 | 27.883 | 24.198 |
| 15 | 27.638 | 24.383 |

_Table 6: Raw PTY wall-time samples._

### `echo-devnull-cb-s1`

| Round | 4096 bytes (s) | 65536 bytes (s) |
| ----- | -------------- | ---------------- |
| 1 | 447.479 | 439.982 |
| 2 | 448.248 | 440.623 |
| 3 | 448.206 | 439.944 |
| 4 | 449.467 | 440.071 |
| 5 | 447.985 | 439.972 |
| 6 | 448.223 | 439.783 |
| 7 | 448.618 | 439.766 |
| 8 | 448.179 | 439.340 |
| 9 | 449.786 | 439.739 |
| 10 | 449.605 | 439.892 |
| 11 | 448.907 | 439.693 |
| 12 | 449.202 | 440.505 |
| 13 | 451.720 | 439.500 |
| 14 | 449.578 | 439.325 |
| 15 | 447.880 | 440.354 |

_Table 7: Raw line-callback wall-time samples._

## Interpretation

The tee curve falls from 11.609 seconds at 4 KiB to 9.499 seconds at 16 KiB,
9.139 seconds at 32 KiB, and 8.951 seconds at 64 KiB. The additional
measurement of 64 KiB is therefore a plateau selection, not an assumption that
the old baseline's 64 KiB point is still representative. The capture-only
control improves by 11.8415%, providing the relevant tuned Python baseline for
later capture-dispatch work.

The line-callback scenario took approximately 4 hours 57 minutes for its
2.1 GiB wrapped fixture. This is substantially longer than the planning
estimate because the fixture contains roughly 28 million lines per repeat.
It is recorded as an operational observation; it does not change the selected
value or the acceptance result.
