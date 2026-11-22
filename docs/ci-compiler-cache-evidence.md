# CI compiler-cache evidence

This contract is for Cuprum maintainers. It describes the per-job compiler
cache record that ordinary CI runs produce for the backend experiment in
[CI cache ownership](ci-cache-ownership.md#compiler-cache-backend-experiment),
how to read it, and what it deliberately does not record.

## Problem and source of truth

A green Rust job proves nothing about compiler reuse. sccache binds its backend
once, when its server starts, and then reports a plausible hit rate whatever it
bound. A restored archive can contain nothing the job can use. A route that
never invokes rustc reports zero requests honestly, and the same zero from a
route that should compile is a silent hole in the measurement.

No single existing signal separates those cases. The run summary's cache table
reports the rendered key and whether the archive restore hit, which says
nothing about compiler requests. The `sccache --show-stats` report is a whole
job figure, so it cannot say which step compiled. Neither says whether the
server reached the store the workflow claims to have selected.

The workflow's own steps are the source of truth for what was attempted. The
writer here adds only what the runner can observe: the compiler's counters
across a bracketed step, the server's reported backend, and the identity of the
build shape.

## Record contract

`scripts/compiler_cache_evidence.py` writes schema version 1. The workflow
brackets a compiling step with a `snapshot` before and after it, writes a
`mark` before a cache restore or save, and appends the bracket's `record` after
the save so the transfer it measures is inside the record. Each bracket
contributes one line to `records.jsonl`, uploaded as the artefact
`compiler-cache-evidence-${job}-${run_attempt}` with 90-day retention, matching
the benchmark-gate and release telemetry convention.

```json
{
  "schema_version": 1,
  "record_kind": "compiler_cache_observation",
  "bracket": "typecheck",
  "run_id": "123456789",
  "run_attempt": "1",
  "job": "typecheck-test",
  "event_class": "pull_request",
  "recorded_at": "2026-11-22T12:34:56Z",
  "setup_status": "started",
  "backend": "local",
  "wrapper_present": true,
  "cache_service_v2": false,
  "cache_host_class": "absent",
  "sccache_version": "0.12.0",
  "rustc": {"release": "1.87.0", "commit": "unknown", "host": "x86_64-unknown-linux-gnu"},
  "family": "py3.12-debug",
  "matrix_leg": "3.12",
  "compiler_shape": "debug",
  "python_version": "3.12",
  "toolchain": "nightly-2026-08-23",
  "dev_fast": true,
  "checkout_sha": "0000000000000000000000000000000000000000",
  "head_sha": "unknown",
  "base_sha": "unknown",
  "matched_key": "sccache-v1-Linux-X64-self-hosted-ubuntu2404-py3.12-debug-123456700",
  "restore_hit": false,
  "restore_seconds": 12.4,
  "save_seconds": null,
  "counters": {"compile_requests": 17, "cache_hits": 0, "cache_misses": 14},
  "durations": {"cache_write_duration": 0.31},
  "not_cached": {"crate_type": 3},
  "zero_requests": "nonzero",
  "state": "ok"
}
```

`counters` holds exactly the allowlisted statistics names the writer copies:
`compile_requests`, `requests_executed`, `requests_unsupported_compiler`,
`requests_not_compile`, `requests_not_cacheable`, `cache_hits`, `cache_misses`,
`cache_timeouts`, `cache_read_errors`, `cache_write_errors`, `cache_writes`,
`compile_fails`, `non_cacheable_compilations`, `forced_recaches` and
`dist_errors`. `durations` holds `cache_write_duration`,
`cache_read_hit_duration` and `compiler_write_duration`, in seconds. A counter
the server did not report is `null`, which reads as unknown and never as zero.

`state` is one of three values:

| State          | Meaning                                                                                               |
| -------------- | ----------------------------------------------------------------------------------------------------- |
| `ok`           | The server started, a wrapper was bound, the store matches the declaration, and any zero is explained |
| `degraded`     | The job compiled without the cache, or a zero request count is unexplained                            |
| `noncompliant` | A `gha` backend reached a GitHub-hosted cache address, or the v2 service flag was still set           |

_Table 1: The three states, and when a record takes each one._

A `degraded` or `noncompliant` record is excluded from success evidence and
kept in the cost totals. It still describes a paid job that ran.

`zero_requests` is one of four values. `nonzero` means the bracket compiled
something. `no-rustc-route` means the bracket declared that it should not
invoke rustc. `fresh-target` means a Cargo `target` directory already existed
when the bracket opened, which lets Cargo legitimately compile nothing.
`unexplained` is a zero the declaration does not account for, and it forces
`state` to `degraded` so a silent hole cannot look like a healthy run.

## Privacy boundary

The writer copies selected fields and nothing else. It never dumps the
environment. In particular:

- `ACTIONS_CACHE_URL` is read only to classify its host as `github-results`,
  `github-legacy`, `non-github` or `absent`. The URL, its port and its path are
  never persisted.
- `ACTIONS_RUNTIME_TOKEN` and every other credential are neither read nor
  recorded.
- `RUSTC_WRAPPER` is reduced to a boolean presence flag, so a home directory
  path cannot leak through it.
- A rejected value is never echoed. Invalid input produces a fixed warning and
  no record.
- Free-form text has no field. Every string is either a closed-vocabulary
  label, a validated decimal, a 40-character lowercase hex SHA, or a bounded
  identifier.

## Failure policy

Evidence never gates a build. Every writer step is `continue-on-error`, and the
composite action warns with a fixed annotation title when a write or upload
fails. A job that compiled correctly is not failed because its telemetry could
not be stored.

The reader is the opposite. It validates strictly and exits non-zero on a
missing key, an extra key, an unsupported schema version, a boolean where an
integer belongs, a non-finite number, a field of the wrong type or shape, a
closed-vocabulary violation, or a duplicate
`(run_id, run_attempt, job, bracket, matrix_leg)`. A malformed artefact must
not be able to flatten into a plausible summary.

## Reading the evidence

```sh
uv run python scripts/compiler_cache_evidence.py --help
```

The reader accepts JSONL files or directories of them, one or more `gh api`
jobs documents, a declared mapping from job key to display-name substring, and
an optional dated console observation. It writes a JSON summary and an optional
Markdown report.

Whole-job seconds come from the jobs API's `started_at` and `completed_at`,
which are GitHub's timestamps and not the runner's clock. Where the API
document is absent, or a record's job cannot be joined to it, the seconds are
`null` and the family has fewer usable pairs; nothing is estimated.

The console observation supplies what no runner can see:

```json
{
  "schema_version": 1,
  "observed_at": "2026-11-22",
  "branch_protection": "enabled",
  "storage_gb": 30,
  "eviction_days": 7,
  "billing_rounding": "unknown",
  "initial_population_seconds": null,
  "incremental_transfer_seconds": null,
  "incremental_cost_seconds": null,
  "note": "Ubicloud console, cache tab"
}
```

`billing_rounding` is `verified` only when the applicable charge terms have
been read and recorded. Otherwise every runner-second figure is labelled a
proxy, and the break-even calculation stays `unknown` rather than assuming
hit-rate economics.

A family that moves to the proxy also needs its own namespace. `setup-sccache`
accepts an optional `namespace` and exports it as `SCCACHE_GHA_VERSION` for the
`gha` backend only; the store is shared per repository, so two build shapes
that both use it would otherwise collide. The namespace is a fixed identifier,
never a pull-request value, and the action refuses one with the `local` backend.

## Limits

The record measures compiler behaviour, not whole-job behaviour on its own. A
hit rate can rise while a job gets slower, because archive transfer, server
startup and upload are all inside the job. That is why the gate in
[CI cache ownership](ci-cache-ownership.md#adoption-gate) compares whole-job
seconds and charges the restore and save time the migration removes.

The writer cannot see the cache service's own accounting. Storage, eviction and
transfer cost come from the console observation or stay `unknown`.

A bracket's counters are deltas between two snapshots of a shared server. Two
brackets in one job therefore must not overlap, and the writer refuses nothing
if they do; the workflow contract `tests/test_ci_compiler_cache_evidence.py`
holds the brackets to adjacent non-overlapping ranges instead.
