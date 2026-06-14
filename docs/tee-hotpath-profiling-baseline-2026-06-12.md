# Tee hot-path profiling baseline (2026-06-12)

This document records the first full run of the tee hot-path profiling harness
(`benchmarks/`) and the hotspot verdicts it produced. It is the Phase 1
baseline evidence called for by Architectural Decision Record (ADR) 002
([ADR-002](adr-002-additional-rust-components.md)), and it gates the Phase 2
decision on issue [#127](https://github.com/leynos/cuprum/issues/127)
(capture-only consume dispatch through `rust_consume_stream`).

## Environment

| Item          | Value                                                      |
| ------------- | ---------------------------------------------------------- |
| Git commit    | `a50a631` (main)                                           |
| CPU           | AMD Ryzen 9 3900 (12 cores)                                |
| Kernel        | Linux 6.12.0-124.27.1.el10_1.x86_64 (Rocky 10)             |
| Python        | CPython 3.14.4 with `PYTHONPERFSUPPORT=1`                  |
| Rust build    | `maturin develop --release`, `-C force-frame-pointers=yes` |
| perf          | 6.12.0-211.18.1.el10_2 at `perf_event_paranoid=2`          |
| Sampling      | `perf record -F 999 -g --call-graph dwarf,16384`           |
| Corroboration | py-spy 100-200 Hz, parent process only                     |

_Table 1: Profiling environment._

Fixtures were generated with `benchmarks/deterministic_b64_fixture.py`
(algorithm `sha256-counter-v1`, seed 12345, 1.5 GiB raw):

- unwrapped: 2 GiB output, SHA-256 `15e4356a…389f914`;
- wrap-76: 2.03 GiB output, SHA-256 `51394f18…319774cc`.

Artefacts live under `dist/profiles/<scenario>/` (gitignored): `perf.data`,
`perf.report.txt`, `stacks.folded`, `summary.json`, `worker-result.json`, and
`scenario.json`, plus py-spy folded captures under `dist/profiles/pyspy/`.

## Scenario results

Each scenario replays the fixture three times (one warm-up execution runs
before measurement), so the measured payload is 6 GiB (6.08 GiB for the wrap-76
fixture).

| Scenario                      | Wall time | Throughput  | Note           |
| ----------------------------- | --------- | ----------- | -------------- |
| `echo-devnull-nocb-s1`        | 6.80 s    | 0.88 GiB/s  | baseline       |
| `echo-textblackhole-nocb-s1`  | 5.63 s    | 1.07 GiB/s  | text branch    |
| `echo-pty-nocb-s1`            | 33.02 s   | 0.18 GiB/s  | PTY sink       |
| `tee-devnull-nocb-s1`         | 14.45 s   | 0.42 GiB/s  | capture + echo |
| `echo-devnull-cb-s1`          | 312.11 s  | 0.019 GiB/s | 84.77 M lines  |
| `echo-devnull-nocb-s4-python` | 42.47 s   | 0.14 GiB/s  | Python pump    |
| `echo-devnull-nocb-s4-rust`   | 7.78 s    | 0.77 GiB/s  | Rust pump      |

_Table 2: Scenario wall times and throughput._

The `echo-pty-nocb-s1` scenario echoes into a pseudo-terminal (PTY) sink. The
line-callback scenario (`-cb-`) uses the wrap-76 fixture, whose 2,175,740,011
output bytes are 28,256,363 full lines of 76 characters plus a newline
(28,256,363 × 77 bytes) followed by a final partial line of 60 bytes with no
trailing newline. Each repeat therefore emits 28,256,364 lines, and three
measured repeats yield 84,769,092 callback invocations (the `stdout_line_count`
total in `worker-result.json`).

## Hypothesis verdicts

The harness README frames six questions. The verdicts below cite the `perf`
leaf rankings (`summary.json`), the `perf.report.txt` call trees, and the
py-spy line-level drill-downs.

### 1. Does the 4 KiB read loop dominate tee time?

**Partially confirmed.** A read-size sweep shows a ~20% improvement that
plateaus at 16 KiB. The sweep used a throwaway inline driver (no harness
changes): set `cuprum._streams._READ_SIZE` before invoking the worker with the
same arguments as the scenario, three repeats per point (one repeat for the
callback row):

```bash
.venv/bin/python -c "
import sys
sys.argv = ['w', '--fixture', 'dist/fixtures/seed12345-nowrap.b64',
            '--stages', '1', '--mode', 'echo', '--sink-kind', 'devnull',
            '--backend', 'auto', '--repeat', '3']
import cuprum._streams as s
s._READ_SIZE = 65536  # the swept value
from benchmarks import tee_profile_worker as w
w.main()
"
```

| Mode                        | 4 KiB   | 16 KiB | 64 KiB  | 256 KiB |
| --------------------------- | ------- | ------ | ------- | ------- |
| echo                        | 5.91 s  | 4.64 s | 4.64 s  | 4.65 s  |
| tee                         | 13.50 s | —      | 10.98 s | —       |
| echo + callbacks (1 repeat) | ~104 s  | —      | 88.2 s  | —       |

_Table 3: Wall time by `_READ_SIZE` across modes._

The remaining cost is kernel pipe transfer and `memmove`
(`__memmove_avx_unaligned_erms` is 37% of leaf samples in the baseline, with
~42% unresolved kernel leaf time at `perf_event_paranoid=2`). Raising
`_READ_SIZE` to 16-64 KiB is a cheap ~20% win for every parent-side consume
path, consistent with ADR-002 Proposal 3 (buffer tuning), though it does not
change the asymptotic shape.

### 2. Does per-chunk flush dominate tee time?

**Refuted as a dominator.** `py::_write_chunk` carries only 0.34% self time in
the baseline report, and the whole `_write_chunk` subtree is ~11% of parent
samples in the py-spy tee capture. The `write(2)` path is 14% inclusive in the
baseline. Flush-per-chunk adds syscalls but is not where the time goes.

### 3. Does the text sink path add substantial decode/write overhead?

**Refuted.** The text-branch scenario is _faster_ than the file-descriptor (fd)
backed sink (5.63 s vs 6.80 s): per-chunk UTF-8 decode of ASCII base64 is
cheaper than the extra `write(2)`+flush syscalls to `/dev/null`. Decode cost is
negligible (`decoder.decode` is 0.24% even in the callback-heavy capture).

### 4. Does `capture=True` materially increase cost?

**Confirmed.** Tee (capture + echo) is 2.1× the echo-only wall time. The py-spy
drill-down attributes the parent's time inside `_consume_stream_without_lines`
to exactly the predicted double-touch:

- `buffer.extend(chunk)` (`_streams.py:54`): 24.9% leaf;
- final `buffer.decode(...)` (`_streams.py:65`): 26.2% leaf.

The perf leaf profile for tee is 69% unresolved kernel time, consistent with
page-fault traffic from materializing a 2 GiB `bytearray` plus a 2 GiB `str`
per repeat. This is the hotspot that `rust_consume_stream` (issue #127)
targets: a native read-and-decode would eliminate the Python read loop, the
`bytearray` growth, and the second full-buffer decode pass.

### 5. Do line callbacks materially increase cost?

**Confirmed, and the cost is not where the suspect list pointed.** The callback
scenario is ~46× slower than the baseline. Within `_consume_stream_with_lines`,
89% of all samples sit below `on_line(line)` (`_streams.py:211`) and only 6% in
`_split_complete_lines`; incremental decode is 0.24%. The per-line dispatch
chain decomposes as:

| Cost | Frames                                                                         |
| ---- | ------------------------------------------------------------------------------ |
| ~39% | `ExecEvent`/`_EventDetails` dataclass construction in `_StageObservation.emit` |
| ~25% | `emit` body itself (`time.time()`, field assembly)                             |
| ~8%  | `inspect.isawaitable` per hook call (`_emit_exec_event`)                       |
| ~3%  | `SafeCmd.argv_with_program` recomputed per line                                |
| ~1%  | the benchmark's own counting callback                                          |

_Table 4: Per-line dispatch cost breakdown._

Every output line builds a fresh `ExecEvent` carrying programme, argv, cwd,
env, pid, and a wall-clock timestamp, then runs awaitable detection per hook.
At 28.3 M lines per repeat this framework overhead dwarfs the user callback by
two orders of magnitude. The largest single optimization opportunity in the tee
path is to make per-line emission cheap (for example: precompute the invariant
event fields once per stream, avoid `inspect.isawaitable` for known-sync hooks,
and avoid recomputing `argv_with_program` per event) or to make per-line events
opt-in.

### 6. How much does Rust inter-stage pumping change total cost?

**Confirmed as effective.** With three passthrough stages, the Python pump
costs ~36 s over the single-stage baseline (42.47 s vs 6.80 s) with
`_write_to_stream_writer` and `StreamReader.read` visible as the hot Python
frames. The Rust backend collapses that to ~1 s (7.78 s total);
`_rust_backend_native::splice::try_splice_pump` and `splice` appear at only
0.11% inclusive, running on executor threads. The splice path works as designed.

## Incidental findings

- **File-descriptor (FD) close race on the Rust pump path.** The
  `echo-devnull-nocb-s4-rust` run logged repeated
  `OSError: [Errno 9] Bad file descriptor` from
  `_UnixWritePipeTransport._call_connection_lost` during shutdown. This matches
  the file-descriptor ownership risk recorded in ADR-002 and is adjacent to
  issues #124/#125. The run still completed with exit code 0, so the failure is
  currently silent. Worth an issue.
- **`perf script` drops Python just-in-time (JIT) trampoline frames on this
  distro.** Distro perf 6.12 resolves `py::` trampoline symbols in
  `perf report` but not in `perf script`, so the generated `stacks.folded` and
  `summary.json` contain no Python-level frames (a minimal single-process
  capture does not reproduce this; the harness's multi-process captures do).
  Until this is understood, treat `summary.json` as a native-frame ranking and
  use `perf.report.txt` plus a py-spy raw capture for Python-level attribution.
- **Trampoline self-time is thin everywhere.** Python functions carry
  tiny self percentages in perf because most interpreter work lands in native
  CPython symbols (`_PyEval`/tail-call dispatch, allocator). This is expected;
  it is why the py-spy view is kept alongside perf.

## Implications for ADR-002 phasing

1. **Phase 2 (capture-only consume dispatch, issue #127) is supported
   by the evidence.** Capture double-touch is ~51% of parent CPU in tee mode,
   comfortably above the 20% acceptance threshold, provided dispatch is
   restricted to fd-backed, UTF-8/replace, capture-only streams. PR #164's
   annotate-first stance is consistent with this document gating the wiring
   work.
2. **A `_READ_SIZE` bump to 16-64 KiB is a cheap, Rust-free ~20% win**
   on every parent-side consume scenario measured, and stacks with the other
   changes (ADR-002 Proposal 3).
3. **Per-line observe-hook dispatch is the dominant tee-path cost when
   line callbacks are active** and is a pure-Python fix unanticipated by the
   original suspect list. It should be triaged ahead of (or alongside) Phase 3
   raw-sink work.
4. **PTY echo cost is kernel-side.** The 5× slowdown against `/dev/null`
   is dominated by unresolved kernel time in the tty layer; a Rust raw sink
   helper (Phase 3) would remove Python loop overhead but should not be
   expected to reclaim the kernel cost.

## Reproduction

```bash
export RUSTFLAGS="-C force-frame-pointers=yes"
export PYTHONPERFSUPPORT=1
uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml

uv run python benchmarks/deterministic_b64_fixture.py --seed 12345 \
  --raw-bytes 1610612736 --wrap 0 \
  --output dist/fixtures/seed12345-nowrap.b64 \
  --manifest dist/fixtures/seed12345-nowrap.json
uv run python benchmarks/deterministic_b64_fixture.py --seed 12345 \
  --raw-bytes 1610612736 --wrap 76 \
  --output dist/fixtures/seed12345-wrap76.b64 \
  --manifest dist/fixtures/seed12345-wrap76.json

uv run python benchmarks/profile_tee_hotpath.py --profiler perf run
```
