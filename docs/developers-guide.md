# Developers' guide

## Profiling harness overview

The profiling benchmark harness provides deterministic parent-side tee and
capture hot-path profiling for Cuprum, distinct from end-to-end throughput
benchmarks that measure whole pipelines. It lives under `benchmarks/`, uses
Linux `perf` as the primary profiler, supports optional `py-spy` corroboration,
and can run unprofiled smoke scenarios when only command construction and worker
behaviour need to be checked.

## Fixture generation (`benchmarks/deterministic_b64_fixture.py`)

`FixtureConfig` describes deterministic fixture generation with three fields:
`seed`, `raw_bytes`, and `wrap`. `raw_bytes` must be greater than or equal to
zero, and `wrap` must be one of `0` or `76`; `wrap=0` writes unwrapped base64
output, while `wrap=76` writes line-oriented output for callback scenarios.

The generator uses a SHA-256 (Secure Hash Algorithm 256) counter-mode seeded
stream. It encodes `str(seed).encode("utf-8")` plus successive big-endian
counters, reads deterministic bytes in stable chunks, base64-encodes those
chunks, and streams the encoded output to disk. The JSON (JavaScript Object
Notation) manifest records `seed`, `raw_bytes`, `wrap`, `output_bytes`,
`sha256`, and `algorithm`.

Use the command-line interface (CLI) as a module entry point:

```bash
python -m benchmarks.deterministic_b64_fixture \
  --seed N \
  --raw-bytes N \
  --wrap 0|76 \
  --output F \
  --manifest M
```

## Sink model (`benchmarks/sinks.py`)

| Sink kind | Implementation | Cost model |
| --- | --- | --- |
| `devnull` | Operating system null device | Discards bytes without allocation. |
| `text_blackhole` | `TextBlackhole` text stream | Counts characters and exposes no `.buffer`, forcing the text branch. |
| `pty_blackhole` | `PtyBlackhole` pseudo-terminal master/slave pair | Drains the master side from a daemon thread to simulate terminal-like throughput. |

## Worker (`benchmarks/tee_profile_worker.py`)

`TeeProfileWorkerConfig` defines one worker run. It validates that
`fixture_path` points to an existing file, `stages >= 1`,
`repeat_count >= 1`, and that `mode`, `sink_kind`, and `backend` are members of
the supported literal sets. It also carries `encoding` and `errors`, which
default to `utf-8` and `replace`.

`run_tee_profile_worker` builds a command or pipeline through `_build_command`,
selects the stream backend through `_selected_backend`, runs the workload
`repeat_count` times, accumulates `captured_output_length` and
`stdout_line_count`, and returns a `TeeProfileWorkerResult`. The worker result
is the machine-readable payload written by the worker CLI and by the scenario
driver.

The worker mode maps directly to Cuprum's final consume flags:

| Mode | `capture` | `echo` |
| --- | --- | --- |
| `echo` | `False` | `True` |
| `capture` | `True` | `False` |
| `tee` | `True` | `True` |

Backend selection is process-local and environment-driven. `auto` unsets
`CUPRUM_STREAM_BACKEND`; `python` and `rust` set it explicitly. `_selected_backend`
clears the backend availability and selection caches before entering the
context and again when restoring the previous environment value.

## Scenario driver (`benchmarks/profile_tee_hotpath.py`)

`benchmarks/profile_tee_hotpath.py` remains the public driver and module entry
point. It re-exports the stable API while the implementation is split across
supporting modules: scenario composition in `benchmarks/tee_profile_scenarios.py`,
profiler orchestration in `benchmarks/tee_profile_profilers.py`, and command-line
interface and JSON output helpers in `benchmarks/tee_profile_driver.py`.
`TeeProfileScenario` records a resolved scenario: name, fixture path, stage
count, mode, sink kind, line-callback flag, backend, repeat count, encoding,
and error handling. `TeeProfileDriverConfig` records fixture paths, output
directory, profiler choice, warm-up count, measured repeat count, `perf`
frequency, call-graph configuration, and an optional scenario name. It validates
that run counts and `perf` frequency are in range, and that the `perf`
call-graph setting is not blank.

The default matrix is stable and ordered:

1. `echo-devnull-nocb-s1`
2. `echo-textblackhole-nocb-s1`
3. `echo-pty-nocb-s1`
4. `tee-devnull-nocb-s1`
5. `echo-devnull-cb-s1`
6. `echo-devnull-nocb-s4-python`
7. `echo-devnull-nocb-s4-rust`

The Rust scenario is conditional on `can_use_rust_backend()`, so pure-Python
installs omit `echo-devnull-nocb-s4-rust` from the plan before execution.

The driver exposes three CLI subcommands:

- `plan` emits a JSON plan with the resolved `worker_command` for each
  scenario.
- `run-scenario` runs one named scenario with warm-up executions followed by one
  measured run.
- `run` executes the full matrix serially.

Profiler modes are selected through `TeeProfileDriverConfig.profiler`. `none`
runs the worker directly and writes a note that profiler artefacts were not
generated. `perf` uses Linux `perf record`, then post-processes with
`perf report`, `perf script`, and `inferno-collapse-perf`. `py-spy` runs the
optional Python-first profiler and writes its raw output.

## Folded-stack summariser (`benchmarks/summarize_folded.py`)

The folded-stack summariser consumes one text file where each non-empty line has
the form `frame1;frame2 count`. Malformed lines, empty stacks, and non-positive
sample counts are ignored.

It writes a JSON summary with `total_samples`, `top_inclusive_frames`,
`top_leaf_frames`, and `top_stacks`. Frame entries include inclusive samples,
leaf samples, normalised percentages, and example stacks, while stack entries
record sample counts and percentages.

Inclusive frame accounting deduplicates frames within a single folded stack. If
the same frame appears multiple times in one stack, for example through
recursion, it is counted once for that stack in the inclusive tally.

## Makefile tooling changes

`LOCAL_TOOL_ENV` prepends `~/.local/bin` and `~/.bun/bin` to `PATH` for `uv` and
tool-discovery recipes only. This supports non-interactive Continuous
Integration/Continuous Delivery (CI/CD) hook environments without globally
shadowing system tools for unrelated Makefile workflows.
