# Developers' guide

This guide is for maintainers. It captures the operational scope for build,
test, lint, release, debugging, and extension workflows and acts as the source
of truth for day-to-day contributor expectations. For the system design, see the
[design document](cuprum-design.md); for where code lives, see the
[repository layout](repository-layout.md). Accepted architectural decisions are:

- [ADR-002: Additional Rust components](adr-002-additional-rust-components.md)
- [ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md)
- [ADR-004: Interrogate docstring-coverage gate](adr-004-interrogate-docstring-gate.md)

## Rust dependency management

When editing `Cargo.toml`, dependencies must use explicit semver-compatible
caret requirements only (for example `"1.2.3"`). Do not use wildcards such as
`*` or open-ended ranges such as `>=` or `~`.

When updating Rust dependencies, keep the requested version aligned to the
patch baseline already present in `Cargo.lock`. This keeps lockfile updates
focused, small, and easy to review.

## Tar and rsync builder helpers

`TarCreateOptions.compression` in `cuprum/builders/tar.py` selects one member
of the `Compression` enum: `NONE`, `GZIP`, `BZIP2`, or `XZ`. This makes the
compression choice mutually exclusive while keeping `TarCreateOptions`
immutable.

The private `_tar_create_argv` and `_tar_extract_argv` helpers in
`cuprum/builders/tar.py`, together with `_rsync_argv` in
`cuprum/builders/rsync.py`, construct immutable argument vectors independently
of `sh.make` wrapping. They exist, so the command construction contract can be
tested directly for issue #71, while the public builders remain responsible for
attaching their curated program.

`_FLAG_ORDER` in `cuprum/builders/rsync.py` defines the fixed rsync flag
emission order: `archive`, `delete`, `dry_run`, `verbose`, then `compress`.
This is a documented contract covered by the property tests in
`cuprum/unittests/test_builder_property_based.py`.

Both `TarCreateOptions` and `RsyncOptions` provide `allow_relative`. It
defaults to `False`, so `safe_path` rejects relative paths unless a caller
explicitly opts in.

## Command argument construction

`cuprum.sh.build_argv(*args, **kwargs)` is the public, pure argv-construction
helper behind `sh.make` builders. It delegates to the same internal coercion
path as builders, so tests and project-specific wrappers can verify argument
normalization without catalogue lookup or subprocess execution.

Keep `build_argv` and `sh.make` behaviour aligned:

- positional arguments are stringified with `str()` in the order supplied;
- keyword arguments are serialized after positionals as `--flag=value` entries;
- underscores in keyword names are normalized to hyphens;
- insertion order for keyword flags is preserved;
- `None` raises `TypeError` in positional and keyword positions.

Property coverage for this contract lives in
`cuprum/unittests/test_sh_property_based.py`. Update those properties whenever
argv construction semantics change.

## Program catalogue duplicate diagnostics

`ProgramCatalogue` indexes project settings in two passes: first by project
name, then by program ownership. Keep duplicate diagnostics structured so tests
and configuration tooling can assert on fields instead of parsing messages.

- `DuplicateProjectError` is raised for repeated project names and exposes the
  duplicated name as `project_name`.
- `DuplicateProgramError` is raised when a program is claimed by more than one
  project and exposes the contested `program` plus the existing owner's project
  name as `owner`.

Both exceptions intentionally subclass `ValueError` to preserve compatibility
with callers that already treat catalogue construction failures as invalid
configuration.

## Stream line-splitting properties

Line callbacks in the Python stream backend use two pure helpers from
`cuprum/_streams.py`:

- `_split_complete_lines(text)` splits text into completed lines, strips each
  recognized line ending, and returns `(lines, remainder)`. The `remainder` is
  the final partial line and never ends in `"\n"` or `"\r"`.
- `_strip_line_ending(line)` removes at most one trailing `"\r\n"`, `"\n"`, or
  `"\r"` sequence. It does not normalize or edit interior text.

These helpers are re-exported from `cuprum/_testing.py` so tests can state the
contract directly without driving subprocess I/O. Keep them private to the
package: they exist to make `_emit_completed_lines` small and testable, not as
public user API.

`cuprum/unittests/test_line_splitting.py` contains the direct property suite.
Hypothesis generates text with mixed recognized line endings and checks that
normalized text is preserved, the final remainder is partial, and stripping is
idempotent. CrossHair runs PEP 316 (Python Enhancement Proposal 316) contracts
over bounded symbolic inputs for the same invariants. CrossHair is a
development dependency only; the tests skip the symbolic checks whenever
CrossHair cannot run on the active interpreter. Rather than hard-coding a
Python-version gate, the suite probes CrossHair at import time and degrades to
skipping only for expected availability failures: a missing dev dependency
(`ImportError`) or an interpreter whose opcode set CrossHair cannot yet trace
(`crosshair.tracers.TraceException`, as with the `CALL_KW` gap on early Python
3.15 betas, issue #109). Any other probe exception is allowed to propagate so
that unexpected import failures stay visible. The probe self-resolves once
CrossHair supports the interpreter.

When changing `_emit_completed_lines`, `_split_complete_lines`, or
`_strip_line_ending`, run:

```bash
uv run pytest -q cuprum/unittests/test_line_splitting.py
```

Run `make test` before committing so the stream behaviour and the pure helper
contracts stay aligned.

## Canonical stream-drain loop

`cuprum._streams._drain(stream, config, *, on_chunk=None)` is the single
read/echo/buffer loop behind both consume variants. It reads in `_READ_SIZE`
chunks, extends the capture buffer when capturing, echoes each chunk to the
configured sink when echoing, and hands the chunk to the optional `on_chunk`
callback for variant-specific processing:

- `_consume_stream_without_lines` calls `_drain` with no callback.
- `_consume_stream_with_lines` supplies an `on_chunk` callback that feeds the
  incremental decoder and emits completed lines, then flushes the decoder tail
  after the drain returns.

Re-use policy: the public entry point remains `_consume_stream`, which
dispatches between the two variants on whether an `on_line` callback was
supplied. Any fix to the read/echo/capture mechanics belongs in `_drain` so the
capture path and the line-emitting path cannot silently diverge; new consume
variants must layer behaviour through `on_chunk` rather than copying the loop.

`cuprum/unittests/test_stream_property_based.py` and
`tests/behaviour/test_stream_property_preservation_behaviour.py` hold the
public-boundary property coverage: Hypothesis generates byte payloads split at
arbitrary boundaries and asserts that real subprocess pipelines preserve
payloads, keep final stdout and stderr captures independent, and echo all
stdout and stderr text when both streams share one sink.
`cuprum/unittests/test_stream_drain.py` keeps focused direct coverage for the
canonical helper contract and the two `_consume_stream` variants.

### Concurrency model

Each `_drain()` invocation is self-contained. It owns its capture buffer
(`bytearray`) and, in the line-emitting variant, its own
`codecs.IncrementalDecoder`. The `on_chunk` callback closes over that decoder
and acts only as the chunk delivery hook. Concurrent stdout and stderr drains
therefore do not share mutable capture or decoder state.

The echo sink (`config.sink`) may be shared between concurrent drains when
stdout and stderr are both echoed. Writes to that sink interleave only at
`await` points: once a drain starts writing a single decoded chunk, there is no
intermediate `await` before that chunk write finishes.

Cancellation is fail-fast. If an `asyncio.Task` wrapping `_drain()` is
cancelled, `asyncio.CancelledError` propagates from `stream.read()` and any
bytes captured by that invocation so far are discarded.

Callers must not share one `asyncio.StreamReader` between two `_drain()`
invocations. Each invocation must receive its own reader.

### Canonical adapter event projection and locked-store base

`cuprum/adapters/_support.py` keeps the three telemetry adapters from
repeating the same event projection and in-memory collector locking. It owns
the canonical logging/tracing `(key, value)` projection helper for optional
execution fields, the `project` tag helper, the shared unhandled-phase debug
log, and `_LockedStore` with its lock-guarded `reset()`. Each adapter retains
backend-specific key names and value shaping, such as tuple versus list
`argv`, at its call site.

This module was extracted to prevent three-way projection drift and keep each
adapter within its cohesion budget. It is importable only by adapter modules:
do not add backend rendering, logging configuration, event construction, or
general-purpose utilities. Production imports stay adapter-only; contract tests
such as `cuprum/unittests/test_adapter_projection.py` may import
`_event_common_fields` to pin the projection contract. Add an adapter-visible
`ExecEvent` field once to `_event_common_fields`; new in-memory collectors
derive from `_LockedStore` and implement `_clear()` while its lock is held.

`cuprum/unittests/test_adapter_projection.py` pins this contract with Hypothesis
properties and redacted per-phase syrupy snapshots.

### Build and test worker controls

`make test` runs pytest serially by default. Set `PYTEST_WORKERS` to a positive
worker count to enable xdist explicitly. Set `BUILD_JOBS=-jN` to pass the same
count to Rust test commands and, through `CARGO_JOB_ENV`, to both
`RAYON_NUM_THREADS` and `CARGO_BUILD_JOBS`.

## Canonical `_TokenRegistration` handle base

All `ContextVar`-backed scope-registration handles — `AllowRegistration`,
`HookRegistration`, and `EnvRegistration` in `cuprum/context.py` — derive from
one canonical `_TokenRegistration` base. The base owns the `_token`/`_detached`
pair, the idempotent `detach()`, the context-manager protocol, and the
`_install(new_ctx)` step that sets the derived context and captures the
restoration token. Subclasses implement only the context-derivation step in
`__init__`. The consolidated "Token-based Restoration" docstring lives on the
base.

Re-use policy: any new scope-registration handle must derive from
`_TokenRegistration` and confine itself to deriving the new context; the
restoration protocol is subtle (`ContextVar` token discipline), so a divergent
copy is a latent correctness hazard. Note that `LoggingHookRegistration`
(`cuprum/logging_hooks.py`) is a *pair* handle: it composes two
`HookRegistration` instances and detaches them in reverse order; it
deliberately carries no token of its own.

`cuprum/unittests/test_token_registration_stateful.py` verifies the token
discipline with a Hypothesis `RuleBasedStateMachine` driving randomized
register/detach sequences across all token-backed handle types (nesting, context-manager
exit, LIFO detach, double-detach), plus an example test pinning the
documented non-LIFO hazard.

## Canonical stage-observation inputs

The observation tag schema is a wire contract for observability, so the
env-overlay resolution and base tag construction shared by the single-command
and pipeline paths live in exactly one place, `cuprum/_observability.py`:

- `_resolve_env_overlay(extra)` layers the per-call overlay (typically
  `ExecutionContext.env`) over the scoped overlay from the active
  `CuprumContext` and returns the immutable merge result. It stays overlay-only
  — `os.environ` is merged separately at spawn time by `resolve_env`.
- `_base_stage_tags(cmd, capture=…, echo=…)` builds the shared tag schema
  (`project`, `capture`, `echo`). The pipeline observation builder grafts on
  only its stage-specific keys (`pipeline_stage_index`, `pipeline_stages`);
  per-call tags are merged over the base via `_merge_tags`.

Re-use policy: the three call sites — `_prepare_execution_observation`
(`cuprum/sh.py`), `_build_pipeline_observations`
(`cuprum/_pipeline_internals.py`), and `_build_spawn_observations`
(`cuprum/_process_lifecycle.py`, which now delegates to the pipeline builder
and adds only its no-observe-hooks assertion) — must route through these
helpers. A new shared tag is added once, in `_base_stage_tags`, or it will
silently diverge between the single-command and pipeline telemetry.

`cuprum/unittests/test_stage_observation_builder.py` pins the contract with
Hypothesis properties (overlay resolution matches `merge_env_overlays`
semantics and stays immutable; both paths agree on the shared tag keys) and a
syrupy snapshot of representative single-command and pipeline tag dictionaries.

## Context allowlist internals

`CuprumContext` stores an `allowlist` plus the internal
`_allowlist_is_restricted` marker. The marker distinguishes the permissive
default context from a context that has deliberately narrowed to an empty
allowlist. It defaults to `False` on the default context and becomes `True` for
explicit narrowing, when `ScopeConfig.allowlist` is provided, or when
`with_allowlist()` receives a non-empty replacement. Direct allowlist
replacement also preserves restriction when the previous context already had an
explicit policy. Replacing that allowlist with `frozenset()` therefore cannot
widen the context back to the permissive default by accident.

`check_allowed()` therefore has two empty-allowlist modes. Empty and
unrestricted means *no policy has been established yet*, so every program is
permitted for the adoption-friendly default. Empty and restricted means a
policy has been established and then narrowed to no programs, so every program
is denied. Keeping that bit separate from the set contents prevents permission
broadening regressions where `frozenset()` could otherwise mean both "allow
everything" and "deny everything".

`narrow()` handles allowlists in three cases:

- An empty unrestricted parent uses the provided allowlist directly, creating
  the first explicit base scope.
- An empty restricted parent stays empty, preserving the deny-all result of
  earlier narrowing.
- A non-empty parent intersects its allowlist with the provided allowlist, so
  nested scopes can remove programs but cannot add new ones.

`with_allowlist()` is the direct replacement path. It preserves restriction
when the current context already has an explicit policy, even if the
replacement allowlist is empty, and a non-empty replacement establishes an
explicit policy by setting restriction. So direct replacement cannot turn a
deny-all context into the permissive default.

The allowlist, hook, and timeout rules are split into pure helpers so the
invariants can be tested directly:

- `_narrow_allowlist(parent, config, parent_is_restricted=...)` returns the
  narrowed allowlist for the three parent/config cases without mutating either
  input.
- `_is_narrowed_allowlist_restricted(config, parent_is_restricted=...)`
  returns whether the child context should enforce allowlist policy after
  narrowing.
- `_merge_before_hooks(parent, config)` appends scoped before hooks after
  parent hooks so execution stays FIFO.
- `_merge_after_hooks(parent, config)` prepends scoped after hooks before
  parent hooks so teardown stays LIFO.
- `_merge_observe_hooks(parent, config)` appends scoped observation hooks after
  parent hooks so execution stays FIFO.
- `_validate_timeout(timeout, class_name)` coerces non-negative timeout values
  to `float`, preserves `None`, and rejects negative values.
- `_resolve_narrowed_timeout(parent, config)` inherits the parent timeout when
  the scoped config is silent and otherwise uses the scoped value.

Context property tests live in `cuprum/unittests/test_context.py`. Run them
directly with:

```bash
uv run pytest -q cuprum/unittests/test_context.py
```

The same test module marks pure-helper properties for optional CrossHair
execution. The `crosshair` Hypothesis profile is registered in
`cuprum/unittests/conftest.py`; using it requires the `hypothesis-crosshair`
package from the dev dependency group.

## `rust_consume_stream` integration status

`rust_consume_stream` is implemented, tested, and exported, but production
consumes currently go through the pure-Python `_consume_stream` function until
Phase 2 is complete. Integration is deferred to
[ADR-002: Additional Rust components](adr-002-additional-rust-components.md)
(Phase 2). The rationale is to defer consume-side dispatch until the ADR-002
Phase 2 stack is complete, including dispatcher wiring, the Python fallback
path, and parity/property coverage.

## Fail-fast reducer properties

`_build_final_results` in `cuprum/concurrent.py` is the pure reducer that
compacts fail-fast concurrent command results.  It drops `None` (cancelled)
entries and remaps failure indices to the compacted result list.  The reducer
carries explicit postcondition-style contracts in its docstring:

- `final_results` contains only non-`None` entries (cancelled slots removed).
- `len(final_results)` equals the number of non-`None` entries in `inputs`.
- Every index in `failures` is within `[0, len(final_results))`.
- `failures` is sorted in ascending order.
- Every index in `failures` points at an entry with `ok == False`.
- `failures` contains *all* such indices — no non-ok entry is omitted.
- The relative order of non-`None` inputs is preserved in `final_results`.

These invariants are verified at two levels:

- **Hypothesis** (`cuprum/unittests/test_build_final_results_property.py`)
  generates up to 50 compact `CommandResult | None` lists and asserts
  `_build_final_results_invariants_hold` over each.  Run:

  ```bash
  uv run pytest -q cuprum/unittests/test_build_final_results_property.py
  ```

- **CrossHair** performs bounded symbolic verification over the assertion
  target.  Run:

  ```bash
  uv run crosshair check \
    cuprum.unittests.test_build_final_results_property._assert_build_final_results_invariants \
    --analysis_kind asserts
  ```

  CrossHair is a development dependency only.  The property module skips
  symbolic checks on Python 3.15, where CrossHair cannot yet trace the
  `CALL_KW` opcode (tracked in issue `#109`).

When changing `_build_final_results`, run both verification paths before
committing.

## Environment overlay resolution

The user-facing `env(...)` context manager and the related `ScopeConfig` field
carry an *overlay-only* mapping that is layered on top of the live `os.environ`
at subprocess spawn time. The implementation sits in `cuprum/context.py` and is
built on three cooperating helpers:

- `merge_env_overlays(parent, child)` (public) returns an immutable
  `MappingProxyType` whose entries are `parent` updated by `child`. Either
  layer may be `None`, in which case the result is whichever layer is set (or
  `None`); empty mappings are treated as "no contribution".
- `resolve_env(*layers)` (public) returns `os.environ.copy()` updated by
  every non-empty layer, in left-to-right order. When every layer is `None` or
  empty, the helper returns `None` so the caller can pass it straight through to
  `subprocess.Popen` to mean *inherit the parent environment unchanged* — this
  is also the path that avoids the redundant `os.environ` copy.
- `_coerce_env_overlay(overlay)` (internal) wraps any caller-supplied
  mapping in `MappingProxyType(dict(overlay))` so the stored overlay cannot be
  mutated through the original reference.

The split between `merge_env_overlays` and `resolve_env` is deliberate.
`merge_env_overlays` is the overlay-only merge used by observation tagging
(`_StageObservation.env_overlay` and the `ExecEvent.env` field) — it must not
include a snapshot of `os.environ`, otherwise structured event logs would carry
the entire parent process environment on every emission. `resolve_env` is the
spawn-time merge that *does* include `os.environ`; it is called from
`_process_lifecycle._merge_env` for both the single-command and pipeline paths.

The live-view contract from issue #100 is enforced at one place only:
`resolve_env` reads `os.environ` at call time, not when the overlay is
registered. Any code that touches the spawn path must therefore route through
`resolve_env` (directly or via `_merge_env`) — never via a captured snapshot of
`os.environ` at registration time.

The `CuprumContext.env_overlay` field is a `MappingProxyType` (or `None`) and
is itself part of the immutable context dataclass.
`scoped(ScopeConfig(env_overlay=...))` and `env(...)` both build a new
`CuprumContext` via `with_env_overlay`, capture the resulting `ContextVar`
token, and reset it on scope exit; nested scopes therefore behave as a stack
and are restricted by the same LIFO detach rule as `AllowRegistration` and
`HookRegistration`.

Property tests for the merge and resolve invariants live in
`cuprum/unittests/test_env_context_properties.py`. They use
[Hypothesis](https://hypothesis.readthedocs.io/) to exercise arbitrary layer
counts, payload contents, and overlap patterns, and to confirm that the helpers
never mutate caller-supplied mappings.

## Pipeline throughput benchmark configuration

`PipelineBenchmarkConfig` controls the hyperfine-based end-to-end throughput
suite in `benchmarks/pipeline_throughput.py`. Scenario commands run
`benchmarks/pipeline_worker.py` with `python_bin`, which defaults to the active
interpreter and is resolved to an absolute executable path before measured
non-dry-run benchmarks. The measured command intentionally avoids `uv run` so
the Rust ratchet measures worker pipeline throughput rather than environment
startup overhead.

Each worker process executes `worker_iterations` pipeline runs (default: 20).
Hyperfine therefore measures a batched worker invocation rather than one cold
pipeline execution, reducing Python interpreter startup noise in the ratchet.
The ratchet itself compares each scenario's within-run
`rust_mean / python_mean` ratio between the baseline and candidate runs, so
runner-speed differences and residual startup overhead cancel out of the
comparison. Dry-run plans record `benchmark_profile_version` and
`worker_iterations`; ratchet comparison skips older baseline artefacts whose
profile metadata does not match the current benchmark shape.

The remaining fields follow the benchmark plan: `output_path` receives
hyperfine JSON or dry-run plan JSON, `worker_path` points at the worker module,
`scenarios` supplies the rendered command matrix, `warmup` and `runs` configure
hyperfine iteration counts, `hyperfine_bin` selects the hyperfine executable,
`dry_run` writes the plan without invoking hyperfine, and `rust_available`
records whether Rust scenarios are included.

`uv_bin` is a deprecated legacy field that remains accepted in the dataclass
for backward compatibility, but current benchmark command construction ignores
it entirely. Keep it unset in new usage and set `python_bin` when a specific
interpreter is required. In dry-run mode, command rendering does not resolve
`python_bin` via PATH.

## Profiling harness overview

The profiling benchmark harness provides deterministic parent-side tee and
capture hot-path profiling for Cuprum, distinct from end-to-end throughput
benchmarks that measure whole pipelines. It lives under `benchmarks/`, uses
Linux `perf` as the primary profiler, supports optional `py-spy` corroboration,
and can run unprofiled smoke scenarios when only command construction and
worker behaviour need to be checked.

### Profiling prerequisites and build settings

Linux is the reference platform for profiler artefacts. Symbol-quality and
sampling settings must match those used to collect the baseline, otherwise the
call graphs lose Rust and Python frames:

- Build the native extension with frame pointers so `perf` can unwind mixed
  Python and Rust stacks: set `RUSTFLAGS="-C force-frame-pointers=yes"`, then
  run
  `uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml`
  from the repository root (as in the reproduction block below).
- Export `PYTHONPERFSUPPORT=1` so CPython emits `perf` map entries for
  interpreted frames.
- Sample with `perf record -F 999 -g --call-graph dwarf,16384`. DWARF
  unwinding is more robust than frame-pointer-only unwinding for the mixed
  stacks here; the driver applies these defaults and exposes `--perf-frequency`
  and `--perf-call-graph` overrides.
- Grant `perf` permission to collect user-space samples. The baseline was
  taken at `perf_event_paranoid=2`, which is sufficient for the user-space call
  graphs this harness needs; kernel symbols remain unresolved at that level
  (raw addresses in the call trees). Check the current level with
  `cat /proc/sys/kernel/perf_event_paranoid`. If sampling is denied, either
  lower it on the host (`sudo sysctl -w kernel.perf_event_paranoid=2`, or a
  lower value such as `1` or `-1` if kernel frames are also required), or grant
  `CAP_PERFMON` to the `perf` binary (for example with `setcap`).
- Install `perf`, `inferno-collapse-perf`, and optionally `py-spy` on `PATH`.

These settings, the deterministic fixtures, and the full reproduction sequence
are recorded in
[the tee hot-path profiling baseline](tee-hotpath-profiling-baseline-2026-06-12.md).
The harness reproduction entrypoint is:

```bash
export RUSTFLAGS="-C force-frame-pointers=yes"
export PYTHONPERFSUPPORT=1
uv run maturin develop --release --manifest-path rust/cuprum-rust/Cargo.toml
uv run python benchmarks/profile_tee_hotpath.py --profiler perf run
```

## Fixture generation (`benchmarks/deterministic_b64_fixture.py`)

`FixtureConfig` describes deterministic fixture generation with three fields:
`seed`, `raw_bytes`, and `wrap`. `raw_bytes` must be greater than or equal to
zero, and `wrap` must be one of `0` or `76`; `wrap=0` writes unwrapped base64
output, while `wrap=76` writes line-oriented output for callback scenarios.

The generator uses an SHA-256 (Secure Hash Algorithm 256) counter-mode seeded
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

<!-- markdownlint-disable MD013 -->

| Sink kind        | Implementation                                   | Cost model                                                                        |
| ---------------- | ------------------------------------------------ | --------------------------------------------------------------------------------- |
| `devnull`        | Operating system null device                     | Discards bytes without allocation.                                                |
| `text_blackhole` | `TextBlackhole` text stream                      | Counts characters and exposes no `.buffer`, forcing the text branch.              |
| `pty_blackhole`  | `PtyBlackhole` pseudo-terminal master/slave pair | Drains the master side from a daemon thread to simulate terminal-like throughput. |

<!-- markdownlint-enable MD013 -->

## Worker (`benchmarks/tee_profile_worker.py`)

`TeeProfileWorkerConfig` defines one worker run. It validates that
`fixture_path` points to an existing file, `stages >= 1`, `repeat_count >= 1`,
and that `mode`, `sink_kind`, and `backend` are members of the supported
literal sets. It also carries `encoding` and `errors`, which default to `utf-8`
and `replace`.

`run_tee_profile_worker` builds a command or pipeline through `_build_command`,
selects the stream backend through `_EnvBackendSelector`, runs the workload
`repeat_count` times, accumulates `captured_output_length` and
`stdout_line_count`, and returns a `TeeProfileWorkerResult`. The worker result
is the machine-readable payload written by the worker CLI and by the scenario
driver.

The worker mode maps directly to Cuprum's final consume flags:

| Mode      | `capture` | `echo`  |
| --------- | --------- | ------- |
| `echo`    | `False`   | `True`  |
| `capture` | `True`    | `False` |
| `tee`     | `True`    | `True`  |

Backend selection is process-local and environment-driven. `auto` unsets
`CUPRUM_STREAM_BACKEND`; `python` and `rust` set it explicitly.
`_EnvBackendSelector` holds a process-wide lock while the worker runs so
concurrent benchmark workers cannot race on `os.environ` or the backend
availability and selection caches. The selector clears those caches before
entering the context and again when restoring the previous environment value.
It is intentionally not re-entrant: a thread-local guard detects nested entry
on the same thread, logs the rejected backend and thread identifier, and raises
`RuntimeError` before mutating backend state.

### Selector observability metrics

`TeeProfileWorkerResult` includes selector metrics gathered while activating
the backend. The metrics are thread-local, reset for each worker run, and
reported with the rest of the worker payload. They represent per-invocation
totals for `TeeProfileWorkerResult`, not process-lifetime aggregates.

| Field                       | Type    | Description                                                                          |
| --------------------------- | ------- | ------------------------------------------------------------------------------------ |
| `lock_wait_seconds`         | `float` | Cumulative time spent blocking on `_BACKEND_LOCK` during selector activation.        |
| `reentrant_rejection_count` | `int`   | Count of selector re-entrancy violations detected and rejected on the worker thread. |

*Table: Selector observability metrics reported in each
`TeeProfileWorkerResult`, with field name, type, and what each value records.*

## Scenario driver (`benchmarks/profile_tee_hotpath.py`)

`benchmarks/profile_tee_hotpath.py` remains the public driver and module entry
point. It re-exports the stable API while the implementation is split across
supporting modules: scenario composition in
`benchmarks/tee_profile_scenarios.py`, profiler orchestration in
`benchmarks/tee_profile_profilers.py`, and command-line interface and JSON
output helpers in `benchmarks/tee_profile_driver.py`. `TeeProfileScenario`
records a resolved scenario: name, fixture path, stage count, mode, sink kind,
line-callback flag, backend, repeat count, encoding, and error handling.
`TeeProfileDriverConfig` records fixture paths, output directory, profiler
choice, warm-up count, measured repeat count, `perf` frequency, call-graph
configuration, and an optional scenario name. It validates that run counts and
`perf` frequency are in range, and that the `perf` call-graph setting is not
blank.

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

### Profiler adapter protocol

Profiler orchestration is decoupled from scenario execution through the
`ProfilerAdapter` protocol (defined in `benchmarks/tee_profile_profilers.py`).
Any object with a `run(scenario, *, scenario_dir, config)` method satisfies the
protocol. Three concrete adapters are provided:

<!-- markdownlint-disable MD013 -->

| Adapter class    | `profiler` value | Behaviour                                                                                                                                         |
| ---------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_NoneProfiler`  | `"none"`         | Runs the worker directly and writes `notes.txt` explaining that profiler artefacts were not generated.                                            |
| `_PerfProfiler`  | `"perf"`         | Records `perf.data`, generates `perf.report.txt` and `stacks.folded` via `inferno-collapse-perf`, and summarizes folded stacks to `summary.json`. |
| `_PySpyProfiler` | `"py-spy"`       | Records a raw `py-spy` trace to `pyspy.raw`.                                                                                                      |

<!-- markdownlint-enable MD013 -->

`_profiler_for(name)` is the factory that maps a `ProfilerName` literal to its
adapter. Adding a new profiler requires implementing the protocol and
registering it in `_profiler_for`.

### `TeeProfileScenario` semantics

`TeeProfileScenario` is a frozen dataclass representing one fully resolved
profiling scenario. Its fields are:

<!-- markdownlint-disable MD013 -->

| Field                 | Type           | Description                                                                     |
| --------------------- | -------------- | ------------------------------------------------------------------------------- |
| `name`                | `str`          | Unique scenario identifier, used as the sub-directory name under `output_dir`.  |
| `fixture_path`        | `pathlib.Path` | Path to the base64 fixture file replayed by the worker.                         |
| `stages`              | `int`          | Number of pipeline stages (1 = single stage, >1 = chained pass-through stages). |
| `mode`                | `TeeMode`      | Consumption mode: `"echo"`, `"capture"`, or `"tee"`.                            |
| `sink_kind`           | `SinkKind`     | Output sink variant used during execution.                                      |
| `with_line_callbacks` | `bool`         | Whether stdout-line observers are registered during the run.                    |
| `backend`             | `BackendName`  | Stream backend: `"auto"`, `"python"`, or `"rust"`.                              |
| `repeat_count`        | `int`          | Number of measured repetitions.                                                 |

<!-- markdownlint-enable MD013 -->

`as_dict()` returns a JSON-serializable dictionary. `worker_config()` converts
the scenario into a `TeeProfileWorkerConfig`, optionally overriding
`repeat_count`.

### Worker configuration validation

`TeeProfileWorkerConfig.__post_init__` delegates validation to three private
methods:

- `_coerce_fixture_path` coerces `fixture_path` to `pathlib.Path` and raises
  `ValueError` if the path does not refer to an existing file.
- `_validate_numeric_bounds` raises `ValueError` if `stages < 1` or
  `repeat_count < 1`.
- `_validate_enum_fields` raises `ValueError` if `mode`, `sink_kind`, or
  `backend` are not members of the respective `_VALID_*` sets.

### Worker test suite layout

The worker test suite is split across focused modules so that each file covers
one boundary of behaviour:

- `cuprum/unittests/test_tee_profile_worker_core.py` covers parent-side consume
  hot-path execution, result accounting, and snapshotted worker output.
- `cuprum/unittests/test_tee_profile_worker_cli.py` covers CLI invocation, the
  JSON payload shape, and `TeeProfileWorkerConfig` validation errors.
- The `_EnvBackendSelector` concurrency coverage is itself split across three
  modules sharing common scaffolding, keeping each file's responsibility count
  within the cohesion budget:
  - `cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py` — the
    `_BACKEND_LOCK` `RLock` reentrancy guarantee, plus same-thread re-entrant
    selector rejection, recovery, and the structured warning log (snapshot).
  - `cuprum/unittests/test_tee_profile_worker_concurrent_workers.py` —
    concurrent `run_tee_profile_worker` race-freedom across backend pairs.
  - `cuprum/unittests/test_tee_profile_worker_env_preservation.py` —
    `CUPRUM_STREAM_BACKEND` preservation under concurrent, interleaved access.
  - `cuprum/unittests/_tee_profile_worker_test_helpers.py` — the instrumented
    lock, coordinating backend selectors, and race harness used by the
    env-preservation tests; timeout constants, backend-availability helpers,
    Hypothesis backend strategies, and the thread join/assert helper live in
    `cuprum/unittests/conftest.py`.
- `cuprum/unittests/test_tee_profile_worker_selector_metrics.py` covers the
  selector observability metrics (`lock_wait_seconds`,
  `reentrant_rejection_count`): their accumulation, thread-locality, reset per
  run, and presence in the worker result payload.

Keeping the concerns in separate files makes the coverage boundary explicit: a
change to command construction touches the core module, a change to the CLI
contract touches the CLI module, a change to backend locking or the selector
state machine touches one of the three concurrency modules (with shared
scaffolding in the helpers module and `conftest.py`), and a change to selector
metrics touches the metrics module.

### `_EnvBackendSelector` concurrency invariants

The three concurrency modules verify the `_EnvBackendSelector` state machine
that serializes process-local backend selection. The selector is backed by a
process-wide reentrant lock (`_BACKEND_LOCK`) and a thread-local reentrancy
guard; the tests assert the following invariants:

1. `_BACKEND_LOCK` is held for the full duration of the selection context.
2. `os.environ["CUPRUM_STREAM_BACKEND"]` is restored to its previous value on
   context exit.
3. Backend availability and dispatch caches are cleared on entry and on exit.
4. Same-thread reentrancy is rejected before any nested environment mutation.

These invariants mirror the state transitions a threading-level model checker
would explore. Candidate full model-checking routes include `pynusmv` and
translating the selector state machine to Promela for SPIN (Simple Promela
Interpreter). Full tool integration is out of scope; the explicit checkpoint
tests keep the observable states aligned with the model such tools would verify.

#### Hypothesis property-based generation

[Hypothesis](https://hypothesis.readthedocs.io/) generates the input domains
that fixed examples cannot cover exhaustively:

- `test_nested_selector_rejects_generated_backend_pairs` draws an outer and an
  inner backend from the available set and asserts that same-thread nested
  entry always raises `RuntimeError` before mutating backend state, regardless
  of which backend pair is generated.
- `test_generated_concurrent_workers_complete` draws a thread count (2–8) and a
  same-length sequence of backend selections, then runs one worker per backend
  concurrently and asserts every worker completes with `status == "ok"` and
  `exit_code == 0`.

The strategies sample only backends available in the current environment
(`_available_backend_names`), so pure-Python installs omit the Rust backend
from generated cases rather than skipping individual examples. Both generated
tests set `deadline=None` because real worker execution time is not a useful
signal for these properties, and suppress the `function_scoped_fixture` health
check because each example reuses the per-test `tmp_path` fixture.

#### Checkpointed interleaving tests

Property generation establishes that races do not occur across the input
domain; the checkpointed tests prove *why* by pinning a specific interleaving
that would expose a missing lock. They inject a coordinating backend selector
and a `_SignallingRLock` wrapper that signals when a blocking acquire first
observes contention, then drive two worker threads through a deterministic
schedule using `threading.Event` checkpoints:

- `test_concurrent_workers_preserve_backend_environment` holds the lock in the
  first ("python") worker while a second worker contends, and asserts the first
  worker's view of `CUPRUM_STREAM_BACKEND` stays pinned to `"python"`.
- `test_selector_interleaving_blocks_environment_observation_until_unlock`
  asserts the second worker cannot enter its context — and therefore cannot
  observe the environment — until the first worker releases the lock, yielding
  the serialized observation sequence `["python", None]`.

When changing `_EnvBackendSelector`, `_BACKEND_LOCK`, or the reentrancy guard,
run the three concurrency modules together:

```bash
uv run pytest cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py \
  cuprum/unittests/test_tee_profile_worker_concurrent_workers.py \
  cuprum/unittests/test_tee_profile_worker_env_preservation.py
```

## Folded-stack summarizer (`benchmarks/summarize_folded.py`)

The folded-stack summarizer consumes one text file where each non-empty line
has the form `frame1;frame2 count`. Malformed lines, empty stacks, and
non-positive sample counts are ignored.

It writes a JSON summary with `total_samples`, `top_inclusive_frames`,
`top_leaf_frames`, and `top_stacks`. Frame entries include inclusive samples,
leaf samples, normalized percentages, and example stacks, while stack entries
record sample counts and percentages.

Inclusive frame accounting counts each distinct frame name **once per stack**,
regardless of how many times it appears in that stack (for example, through
recursion or inlined duplicate symbols). This matches the convention used by
most sampling profilers: a recursive frame inflates the wall-time cost of the
leaf, not the inclusive tally of every caller on the path.

## Makefile tooling changes

`LOCAL_TOOL_ENV` prepends `~/.local/bin` and `~/.bun/bin` to `PATH` for `uv`
and tool-discovery recipes only. This supports non-interactive Continuous
Integration/Continuous Delivery (CI/CD) hook environments without globally
shadowing system tools for unrelated Makefile workflows.

## Rust error taxonomy (`PumpError`)

The `cuprum-rust` crate reports stream pump and consume failures through one
semantic error enum, `PumpError` (`rust/cuprum-rust/src/errors.rs`), derived
with `thiserror`:

- `LengthOverflow` — an integer length conversion overflowed its target
  type ("impossible" on supported platforms, kept observable rather than
  silently truncating).
- `BufferRangeExceeded` — a computed range exceeded the backing buffer.
- `Io(io::Error)` — an operating-system I/O failure (transparent wrapper).

Conversion to a Python exception happens in exactly one place
(`From<PumpError> for PyErr`): `Io` maps through `pyo3`'s standard `io::Error`
translation, and the overflow variants surface as `OSError`. The non-fatal
write classification (broken pipe / connection reset) lives on the enum as
`PumpError::is_nonfatal_write`, replacing the free function the splice and
read/write paths previously shared. New failure conditions get a variant here
rather than a stringly-typed `io::Error::other(...)`.

## Rust splice-loop and drain contract

The Linux zero-copy path in `rust/cuprum-rust/src/splice.rs` follows one
canonical loop. `try_splice_pump` performs the first `splice_once` solely to
detect support: `EINVAL` on that first call signals unsupported descriptor
types and the read/write fallback. Every outcome thereafter — including the
first call's, which is fed into the loop — is handled by the same arms: `Ok(0)`
ends the transfer, `Ok(n)` accumulates, a non-fatal write error (broken pipe /
connection reset) drains the reader and reports the bytes transferred so far,
and anything else propagates.

`drain_reader` routes through the canonical raw-fd read helper
(`io_utils::read_raw_fd`), so it shares the Unix read policy with the
read/write fallback: interrupted reads (`EINTR`) retry instead of silently
ending the drain, end of file terminates it, and other errors propagate.
Behavioural tests in the module cover full pipe-to-pipe transfer, the fallback
signal for regular files, broken-pipe draining, and the drain's EOF
termination. These outcomes and `PumpError` values are intentionally returned
to the Python boundary, where command observation owns telemetry, so this
internal Rust path does not add a second logging or metrics surface.

Unix Rust tests share pipe creation, duplicated-file wrapping, result helpers,
and descriptor-state checks through `test_support`. Re-use that module for
descriptor-backed test setup; keep production code independent of test helpers.

## Rust property testing and verification

Rust-level tests for `cuprum-rust` live with the crate under
`rust/cuprum-rust/src/`. Use them for pure decoder, parsing, state-machine, and
adapter logic where Python integration tests would only cover a few examples.

Property tests use [proptest](https://docs.rs/proptest/latest/proptest/) as a
development dependency. Prefer generated payloads and small helper functions
that expose pure behaviour. The UTF-8 decoder tests generate arbitrary byte
vectors and chunk split points, then compare the decoded output with
`String::from_utf8_lossy` as the oracle.

Kani harnesses are reserved for bounded verification of small, high-value state
spaces. Gate Kani-only modules and helpers with `#[cfg(kani)]`, and share pure
test helpers behind `#[cfg(any(test, kani))]` when both proptest and Kani need
the same simulation path. Register new custom cfg names in the workspace lint
configuration so `unexpected_cfgs` warnings remain meaningful.

Run the normal project test gate from the repository root:

```bash
make test
```

`make test` runs the Python pytest batches before the crate tests through
`cargo nextest`, including proptest cases compiled under `#[cfg(test)]`. Run
the complete Rust lint and formatting gates before committing Rust changes:

```bash
make check-fmt
make lint
```

Run Kani separately because it is a bounded model checker rather than a normal
unit-test runner. The Kani installer places the verifier under `~/.kani`; the
dynamic library path is required when invoking the crate harnesses. Resolve the
toolchain library directory from the installed version rather than hard-coding
it:

```bash
KANI_VERSION=$(cargo kani --version | awk '{print $2}')
cd rust && \
  LD_LIBRARY_PATH="$HOME/.kani/kani-${KANI_VERSION}/toolchain/lib" \
  cargo kani --package cuprum-rust
```

When adding new Kani proofs, keep the bounds explicit with attributes such as
`#[kani::unwind(N)]`, include `kani::cover!` statements for the intended
boundary cases, and avoid broad symbolic comparisons that force Kani through
large allocation-heavy standard-library internals unless the proof genuinely
requires that surface.

## Python linting

Cuprum uses a three-tier Python lint gate. Ruff is the first tier and remains
the fast, broad lint pass for formatting-adjacent checks, import order,
docstring *style*, security checks, naming, complexity, and Ruff's native
Pylint-derived rules. `interrogate` is the second tier and enforces docstring
*presence* at 100 per cent across the `cuprum` package. Pylint is the third
tier and runs through the `leynos/pylint-pypy-shim` package under PyPy.

The decisions are recorded in
[ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md) and
[ADR-004: Interrogate docstring-coverage gate](adr-004-interrogate-docstring-gate.md).
The short version is:

- Ruff owns fast feedback and the primary rule set, including docstring style.
- `interrogate` owns docstring coverage: it fails the gate when any
  documentable node — including nested closures, dunder methods, properties,
  and stub classes — lacks a docstring that Ruff's `D` rules do not require.
- Pylint owns selected checks that Ruff does not cover, especially logging
  interpolation, pattern matching, generator control flow, environment
  handling, subprocess safety, and selected readability checks.
- Pylint runs through the PyPy shim so that the third tier is isolated from the
  project virtual environment and matches the lint approach used by
  `leynos/episodic`.
- `$(PYLINT)` pins Pylint itself with
  `--with 'pylint==$(PYLINT_VERSION)'` because the shim revision and Pylint
  package version are separate sources of lint behaviour.

Run the complete lint gate with:

```bash
make lint
```

`make lint` performs the following commands in order:

1. `$(RUFF) check`
2. `$(UV_RUN_ENV) uv run interrogate --fail-under 100 cuprum`
3. The PyPy-backed `pylint-pypy` command stored in `$(PYLINT)`, with
   `$(PYLINT_TARGETS)` appended.

Each tier must pass before the next runs. When investigating a lint failure,
fix the Ruff findings first, then the `interrogate` gaps, then rerun
`make lint` to reach the Pylint tier.

### Spelling policy

The lint and Markdown gates run pinned `typos` 1.48.0 with British English and
Oxford `-ize` conventions. Before checking maintained Markdown, the generator
refreshes the shared estate dictionary into an untracked local cache only when
the authority is newer, then merges `typos.local.toml`. The generated
`typos.toml` is reviewed and committed so a clean, network-restricted checkout
can still enforce the last known-good policy.

Add repository-only proper names or quoted upstream terms to
`typos.local.toml`; never edit generated entries in `typos.toml` by hand. The
gate also runs the helper's Python 3.13 tests with at least 90% line coverage.

Ruff must be invoked through the project virtual environment, not as a floating
host tool. The `RUFF` variable expands to `$(UV_RUN_ENV) uv run ruff`, and the
`ruff` probe lives in `VENV_TOOLS` so `make` verifies that the locked
dependency from `uv.lock` is available before running `fmt`, `check-fmt`, or
`lint`. Continuous Integration (CI) and local runs must keep using this
`uv run` path for Ruff linting and formatting so preview-rule changes only
arrive through an explicit lockfile update. `interrogate` is also invoked via
`uv run` in the `lint` recipe, but it is not included in `VENV_TOOLS` and so is
not gated by the probe; it relies on `uv sync` having installed it into the
locked virtualenv.

Because `interrogate` requires a docstring on every documentable node,
documenting a large module can take it over the project's 400-line ceiling
enforced by Pylint's `too-many-lines`. Split the module by feature rather than
suppressing the limit; this is why the pipeline dataclasses live in
`cuprum/_pipeline_types.py` (re-exported from `cuprum/_pipeline_internals.py`)
rather than inline.

### Lint Makefile variables

The root `Makefile` exposes the following lint-related variables:

<!-- markdownlint-disable MD013 -->

| Variable               | Default                                                                      | Purpose                                                                    |
| ---------------------- | ---------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `VENV_TOOLS`           | `pytest ruff`                                                                | Tools that must resolve through `uv run` from the locked virtualenv.       |
| `RUFF`                 | `$(UV_RUN_ENV) uv run ruff`                                                  | Locked Ruff command used by `fmt`, `check-fmt`, and `lint`.                |
| `PYLINT_PYTHON`        | `pypy`                                                                       | Python interpreter requested by `uv tool run` for the Pylint tier.         |
| `PYLINT_TARGETS`       | `benchmarks conftest.py cuprum tests`                                        | Directories and files passed to `pylint-pypy`.                             |
| `PYLINT_PYPY_SHIM_REF` | `726d09f968b4d729ee4b29c71fc732e744854f3b`                                   | Pinned revision of `leynos/pylint-pypy-shim`.                              |
| `PYLINT_PYPY_SHIM`     | `git+https://github.com/leynos/pylint-pypy-shim.git@$(PYLINT_PYPY_SHIM_REF)` | Install source used by `uv tool run`.                                      |
| `PYLINT_VERSION`       | `4.0.5`                                                                      | Pylint package version supplied to `uv tool run` through `--with`.         |
| `PYLINT`               | Derived command                                                              | Full PyPy-backed Pylint command used by `make lint`.                       |
| `LOCAL_TOOL_ENV`       | Derived `PATH`                                                               | Adds local binary directories before invoking host and `uv`-managed tools. |
| `UV_ENV`               | `UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools`                               | Keeps `uv` cache and tool installs local to the worktree.                  |
| `UV_RUN_ENV`           | `$(LOCAL_TOOL_ENV) $(UV_ENV)`                                                | Shared environment for locked `uv run` commands such as `$(RUFF)`.         |

<!-- markdownlint-enable MD013 -->

Override these variables only for local diagnosis. For example, to lint a
single module with the configured second tier:

```bash
PYLINT_TARGETS=cuprum/sh.py make lint
```

Do not change `PYLINT_PYPY_SHIM_REF` casually. Updating the pinned shim
revision changes the lint runtime and must be reviewed like any other toolchain
update.

### Episodic lint policy

Cuprum imports the lint policy used by `leynos/episodic` rather than inventing
a separate house style. The imported policy consists of:

- Ruff `target-version = "py312"` for Cuprum's supported Python baseline.
- Ruff banned `typing.*` generic aliases, requiring modern built-in generics or
  `collections.abc` and `contextlib` equivalents.
- Test-file exceptions for assertion-heavy tests and pytest method conventions.
- A focused Pylint configuration that disables all messages by default, then
  enables only the selected messages that complement Ruff.
- A PyPy-backed Pylint invocation through the pinned shim repository.

This means new code should prefer:

```python
from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def names(values: cabc.Iterable[str]) -> list[str]:
    return list(values)
```

Use `typing as typ` for `TYPE_CHECKING`, casts, aliases, and other `typing`
helpers that are not banned. Use `collections.abc` imports inside
`typ.TYPE_CHECKING` when annotations are deferred and the names are only needed
for type checking.

### `pyproject.toml` lint configuration

The canonical lint configuration lives in `pyproject.toml`:

- `[tool.ruff]` sets line length, preview mode, and target Python version.
- `[tool.ruff.lint]` selects the active Ruff rule families.
- `[tool.ruff.lint.per-file-ignores]` records test-specific exceptions.
- `[tool.ruff.lint.flake8-import-conventions]` and
  `[tool.ruff.lint.flake8-import-conventions.aliases]` enforce import aliases
  such as `typing as typ` and `collections.abc as cabc`.
- `[tool.ruff.lint.flake8-tidy-imports.banned-api]` bans deprecated
  `typing.*` aliases and explains each replacement.
- `[tool.ruff.lint.pylint]` sets Ruff's Pylint-derived thresholds.
- `[tool.pylint.main]`, `[tool.pylint.design]`, and
  `[tool.pylint."messages control"]` configure the second-tier Pylint pass.

When changing lint policy, update both `pyproject.toml` and this guide. If the
change alters the architecture of the lint gate, update
[ADR-003](adr-003-two-tier-python-linting.md) as well.

## Maturin pin synchronization and native wheel tests

The `tests/helpers/maturin.py` module provides shared helpers for tests that
validate the maturin version pin contract and native wheel build output.

**Pin synchronization** (`test_maturin_pins_are_synchronized`) Asserts that the
maturin version declared in `pyproject.toml`,
`.github/workflows/build-wheels.yml`, and
`.github/actions/build-wheels/action.yml` are identical. When updating the
maturin pin, update all three locations and run this test to confirm they are
in step.

**Aarch64 manylinux container pin**
(`test_manylinux_aarch64_container_is_pinned_to_sha256` and
`test_manylinux_aarch64_container_is_referenced_by_build_step`) Asserts that
`MANYLINUX_AARCH64_CONTAINER` in `.github/workflows/build-wheels.yml` is pinned
to an SHA-256 digest and that `build-wheels.yml` uses the pinned variable in
the Linux aarch64 maturin build step.

When refreshing this container, update the value in
`MANYLINUX_AARCH64_CONTAINER` to
`ghcr.io/rust-cross/manylinux_2_28-cross@sha256:<digest>` and keep the inline
comment to the original mutable reference:
`# ghcr.io/rust-cross/manylinux_2_28-cross:aarch64`.

The deterministic tests assert that the live workflow value is correctly formed
and consumed by the aarch64 build step. The property-based tests prove that the
shared regex accepts every valid 64-character hexadecimal digest and rejects
the unbounded space of mutable tags and truncated digests, giving confidence
beyond any single example.

To update the pinned digest, resolve the tag digest for
`ghcr.io/rust-cross/manylinux_2_28-cross:aarch64`, replace only the value in
`MANYLINUX_AARCH64_CONTAINER`, and rerun:

```bash
uv run pytest cuprum/unittests/test_maturin_build.py \
    -k "manylinux_aarch64_container"
```

**Installed version check** (`test_installed_maturin_matches_expected_pin`)
Skipped automatically when `maturin` is not on `PATH`. When it is present,
asserts that the installed version matches the pinned development dependency.

**Wheel build snapshot** (`test_maturin_wheel_build_snapshot`) Requires the
Rust toolchain (`cargo` and `rustc`). Builds a native wheel into a temporary
directory, extracts normalized metadata and layout information, and compares
the result against a
[syrupy](https://github.com/syrupy-project/syrupy) snapshot stored at
`cuprum/unittests/__snapshots__/test_maturin_build.ambr`.

To update the snapshot after a maturin or PyO3 bump, run:

```bash
uv run pytest cuprum/unittests/test_maturin_build.py \
    --snapshot-update -k test_maturin_wheel_build_snapshot
```

## Workflow pins and Dependabot

Dependabot owns the upgrade of GitHub Actions and reusable workflows, including
calls into `leynos/shared-actions`. Contract tests that assert a caller's exact
commit SHA create a lockstep dependency: every time Dependabot opens a bump PR,
the test fails until a human edits the pinned constant to match. That defeats
the purpose of automated dependency updates and turns a routine bump into a
manual chore.

Contract tests may still verify the *shape* of a reusable-workflow caller. They
must not verify the specific SHA value.

- Do assert the workflow references the correct reusable workflow path.
- Do assert the ref is pinned to a full 40-character commit SHA, not a
  mutable branch such as `main` or `rolling`.
- Do assert the expected `on:` triggers, least-privilege `permissions:`, and
  the inputs the caller relies on.
- Do not hard-code the current SHA value as an expected string. Match it with
  a pattern instead.
- Do not fail a test purely because Dependabot bumped the pinned SHA.

```python
import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

def test_uses_pinned_full_sha(caller_step):
    ref = caller_step["uses"].split("@")[-1]
    assert SHA_RE.match(ref), f"expected a 40-hex commit SHA, got {ref!r}"
```

If a workflow's behaviour genuinely depends on a feature only present from a
particular commit onwards, express that as a comment or a changelog note, not
as a test assertion on the SHA string.

## Compile-time UI tests (trybuild)

The Rust crate at `rust/cuprum-rust/` uses
[trybuild](https://github.com/dtolnay/trybuild) to validate PyO3 macro
behaviour at compile time. Tests live under `rust/cuprum-rust/tests/ui/`:

- `tests/ui/pass/` — Rust files that **must compile** without error.
- `tests/ui/fail/` — Rust files that **must fail** compilation with diagnostics
  matching the corresponding `.stderr` file.

Run compile-time UI tests with:

```bash
cd rust && cargo test compile_time_ui
```

To update `.stderr` expectation files after a PyO3 or compiler upgrade:

```bash
cd rust && TRYBUILD=overwrite cargo test compile_time_ui
```

Inspect the updated `.stderr` files before committing to confirm that each fail
test still represents a genuine compile-time error.

## Design decisions

### Deterministic fixtures over random data

Fixtures are generated from an SHA-256 counter-mode seeded stream rather than
from `os.urandom` or `random`. This makes every profiling run reproducible from
the same `--seed` and `--raw-bytes` arguments, enabling artefact comparison
across runs and across machines without storing large binary files in the
repository.

### Extracted helper methods in `__post_init__`

`TeeProfileWorkerConfig.__post_init__` delegates to private helpers rather than
containing all validation inline. This keeps the cyclomatic complexity of each
method below the project threshold of 9 while preserving the single-class
boundary. Inlining the helpers back into `__post_init__` would restore a
complexity of 13 and is explicitly rejected.

### Table-driven validation in `TeeProfileDriverConfig.__post_init__`

`TeeProfileDriverConfig.__post_init__` uses a table of `(name, value, minimum)`
triples to validate numeric bounds in a single loop, reducing measurable
cyclomatic complexity while preserving exact error messages.

### Scenario matrix order is a stable contract

The default scenario matrix order is fixed and documented. Callers, snapshot
tests, and CI artefact directories all depend on it. It must not be reordered
without updating snapshot files and any downstream tooling.

## Pipeline stdio policy and cwd conversion

Two canonical helpers own the subprocess spawn flags used by the subprocess
spawn paths:

- `_get_stage_stream_fds(idx, last_idx, capture_or_echo=...)` in
  `cuprum/_pipeline_stage_streams.py` is the single source of truth for the
  PIPE-versus-DEVNULL stdio selection when spawning pipeline stages. The first
  stage reads stdin from `DEVNULL`, later stages from a `PIPE`; intermediate
  stages always pipe stdout, while the final stage pipes stdout only when
  output is captured or echoed; stderr is piped exactly when output is captured
  or echoed. `_spawn_pipeline_processes` routes through this helper — do not
  re-derive the flags inline at pipeline-stage spawn sites, and do not use it
  for single-command spawning.
- `_cwd_arg(cwd)` in `cuprum/_subprocess_context.py` renders an optional
  working directory (`str | Path | None`) into the `cwd` argument for
  `asyncio.create_subprocess_exec`. Every spawn site must use it, so the
  conversion cannot drift between single-command and pipeline paths.

Re-use policy: any new spawn site must call `_cwd_arg`, and pipeline-stage
spawn sites must call `_get_stage_stream_fds` rather than copying the policy.
Changes to stdio selection (for example, adding stdin handling to pipelines)
belong in `_get_stage_stream_fds` so pipeline-stage behaviour and the
exhaustive tests in `cuprum/unittests/test_stage_stream_fds.py` stay
authoritative. That test module covers the full finite input domain (stage
position × capture/echo) and asserts agreement with the single-command policy
on the overlapping cases.

## Output behaviour carrier

`RunOutputOptions` (`capture`, `echo`) is the canonical carrier for command
output behaviour. Public command execution should accept or construct this
object rather than threading separate `capture` and `echo` keyword arguments
through new APIs. Keep that pairing intact so stdout/stderr handling stays
explicit, testable, and compatible with the `IOOptions` deprecation path.

`SafeCmd.run` / `run_sync` accept `RunOutputOptions` via the `output` parameter
and pass it straight through to `_prepare_execution_observation`, which reads
`output.capture` / `output.echo` for the observation tags. `Pipeline.run` /
`run_sync` use the same `output` parameter and resolve it before building the
pipeline execution config. There is no parallel internal `(capture, echo)`
value object: the former `_IOBehaviour` was redundant with `RunOutputOptions`
and has been removed. `IOOptions` remains only as a deprecated subclass alias
that emits a `DeprecationWarning`.

Internal adapters may translate legacy or aggregate configuration into
`RunOutputOptions` at the boundary. For example, the concurrent runner converts
its `_ConcurrentRunConfig` flags into `RunOutputOptions` once before calling
`SafeCmd.run`. Avoid reintroducing parallel output-option structures unless a
new boundary genuinely has different semantics; in that case, document the
translation rule here and cover it with behavioural tests.

`Pipeline.run` and `Pipeline.run_sync` retain `capture` and `echo` keyword
arguments only for compatibility. Those flags emit `DeprecationWarning` and
must not be combined with `output=RunOutputOptions(...)`; mixed usage raises
`ValueError` before any deprecation warning is emitted, so warning filters do
not obscure the documented ambiguity error.

## Subprocess stdin injection

When `stdin: StdinInput` is passed to `SafeCmd.run()`, the following sequence
executes:

1. `StdinInput.resolve(ctx)` encodes `text` via the execution-context
   encoding/errors, or returns `data` bytes unchanged.  Mutual exclusion is
   enforced at `StdinInput` construction time by `__post_init__`.
2. The resolved bytes are stored on `_SubprocessExecution.stdin_data`.
3. `_spawn_subprocess` opens `stdin=asyncio.subprocess.PIPE` when
   `stdin_data is not None`; otherwise `stdin=None` (inherit parent).
4. `_spawn_stdin_writer` creates an `asyncio.Task` that calls `_write_stdin`,
   which writes the bytes, drains the pipe, and closes it.  `OSError` and
   `RuntimeError` failures are logged to `cuprum.stdin` and emitted as a
   `stdin_error` trace event so operators can observe early-close scenarios
   without execution disruption.  Successful writes emit a `stdin` event with a
   byte count.  The metrics adapter increments `cuprum_stdin_bytes_total` for
   successful writes and `cuprum_stdin_errors_total` for failure events.
5. In the streaming path (`_run_subprocess_with_streams`), the stdin writer
   task runs concurrently with the stdout/stderr consumer tasks.  On
   `TimeoutError`, `_handle_stream_timeout` cancels/gathers the stdin task
   before raising `_SubprocessTimeoutError`.  On `asyncio.CancelledError`, the
   task is explicitly cancelled and gathered before re-raising.
6. In the non-streaming path (`_execute_subprocess`), the same writer task is
   created and awaited after `_wait_for_exit_code` completes.

`_execute_with_hooks(cmd, execution, tracking)` is the single site that runs
`_execute_subprocess`, iterates after-hooks, and co-ordinates cancellation-safe
cleanup of pending hook tasks via `asyncio.shield`. It replaces the try/except
ladder that previously lived inline in `SafeCmd.run`, keeping the public method
to a minimal orchestration skeleton (plan event, before-hooks dispatch,
delegation).

`_build_stream_config(execution)` centralizes construction of the
`_StreamConfig` used by the streaming execution path
(`_run_subprocess_with_streams`). Extracting it removes one branch from that
function, reducing its cyclomatic complexity below the CodeScene threshold, and
makes the stdout-sink resolution logic testable in isolation.

Passing no `StdinInput` leaves subprocess stdin inherited from the parent
process, preserving the pre-feature behaviour.
