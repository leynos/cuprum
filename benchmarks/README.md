# Tee hot-path profiling harness

This directory contains a deterministic profiling harness for the parent-side
tee and capture path used by `echo=True` and `capture=True`. It is separate
from `pipeline_worker.py`, which measures end-to-end pipeline throughput with a
subprocess sink and therefore bypasses the final parent consume loop.

## Prerequisites

Linux is the reference platform for profiler artefacts. Build Cuprum with debug
information and frame pointers before collecting samples:

```bash
export RUSTFLAGS="-C force-frame-pointers=yes"
export PYTHONPERFSUPPORT=1
maturin develop --release
```

Install `perf` and `inferno-collapse-perf` for the primary profiler workflow.
`py-spy` is optional and is only intended as a Python-first corroborating view.

## Fixture generation

Generate fixtures once and reuse them across all profiling runs. Fixture
generation is intentionally outside the profiled workload.

Unwrapped fixture:

```bash
uv run python benchmarks/deterministic_b64_fixture.py \
  --seed 12345 \
  --raw-bytes 1610612736 \
  --wrap 0 \
  --output dist/fixtures/seed12345-nowrap.b64 \
  --manifest dist/fixtures/seed12345-nowrap.json
```

Line-oriented fixture for callback scenarios:

```bash
uv run python benchmarks/deterministic_b64_fixture.py \
  --seed 12345 \
  --raw-bytes 1610612736 \
  --wrap 76 \
  --output dist/fixtures/seed12345-wrap76.b64 \
  --manifest dist/fixtures/seed12345-wrap76.json
```

Recommended raw fixture sizes are:

- smoke: 64 MiB (mebibytes);
- medium: 512 MiB (mebibytes);
- heavy: 1.5 GiB (gibibytes), producing roughly 2 GiB of base64 text.

The manifest records the seed, raw size, wrapping mode, output byte count,
output SHA-256, and generator algorithm identifier.

## Planning

Generate a serial plan without executing scenarios:

```bash
uv run python benchmarks/profile_tee_hotpath.py plan > dist/profiles/plan.json
```

The default matrix is fixed:

- `echo-devnull-nocb-s1`
- `echo-textblackhole-nocb-s1`
- `echo-pty-nocb-s1`
- `tee-devnull-nocb-s1`
- `echo-devnull-cb-s1`
- `echo-devnull-nocb-s4-python`
- `echo-devnull-nocb-s4-rust`

## Running scenarios

Run one unprofiled scenario to validate the workload and write `scenario.json`,
`worker-result.json`, and `notes.txt`:

```bash
uv run python benchmarks/profile_tee_hotpath.py run-scenario \
  --scenario echo-devnull-nocb-s1
```

Run one scenario with `perf` and generate all primary artefacts:

```bash
uv run python benchmarks/profile_tee_hotpath.py \
  --profiler perf \
  run-scenario \
  --scenario echo-devnull-nocb-s1
```

The `perf` sampling defaults match
`perf record -F 999 -g --call-graph dwarf,16384`. Tune them without editing the
harness by passing `--perf-frequency` or `--perf-call-graph` before the
subcommand.

Run the full matrix serially:

```bash
uv run python benchmarks/profile_tee_hotpath.py --profiler perf run
```

Serial execution is deliberate. It reduces noise and avoids cross-profile
contamination.

## Artefacts

Each scenario writes under `dist/profiles/<scenario-name>/`.

Primary profiled runs produce:

- `scenario.json`: full resolved scenario configuration;
- `worker-result.json`: machine-readable workload status and timing;
- `perf.data`: raw `perf record` capture;
- `perf.report.txt`: text call tree from `perf report --stdio`;
- `stacks.folded`: folded stacks from `perf script` and
  `inferno-collapse-perf`;
- `summary.json`: ranked inclusive frames, leaf frames, and stacks.

Optional `py-spy` runs write `pyspy.raw`.

## Interpretation

Use the scenario comparisons to separate likely sources of cost:

- compare `echo-devnull-nocb-s1`, `echo-textblackhole-nocb-s1`, and
  `echo-pty-nocb-s1` to isolate bytes, text, and terminal-like sink behaviour;
- compare `tee-devnull-nocb-s1` with `echo-devnull-nocb-s1` to estimate capture
  accumulation and final decode cost;
- compare `echo-devnull-cb-s1` with `echo-devnull-nocb-s1` to estimate
  incremental decode and line splitting overhead;
- compare `echo-devnull-nocb-s4-python` with
  `echo-devnull-nocb-s4-rust` to distinguish inter-stage pumping cost from the
  constant final tee path.

Treat `_READ_SIZE = 4096`, per-chunk flush, `bytearray.extend` plus final
decode, and callback line splitting as hypotheses. The harness exists to test
them, not to assume them.
