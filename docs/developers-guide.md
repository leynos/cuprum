# Developers' guide

This guide is for maintainers. It captures the operational scope for build,
test, lint, release, debugging, and extension workflows and acts as the source
of truth for day-to-day contributor expectations. For the system design, see the
[design document](cuprum-design.md); for where code lives, see the
[repository layout](repository-layout.md). Accepted architectural decisions are:

- [ADR-002: Additional Rust components](adr-002-additional-rust-components.md)
- [ADR-003: Two-tier Python linting](adr-003-two-tier-python-linting.md)
- [ADR-004: Interrogate docstring-coverage gate](adr-004-interrogate-docstring-gate.md)
- [ADR-005: Unify Rust availability probe](adr-005-unified-rust-availability-probe.md)
- [ADR-006: Split cuprum/context.py into a context package](adr-006-context-package-split.md)
- [ADR-007: Subprocess execution module boundaries](adr-007-subprocess-execution-module-boundaries.md)

## Rust availability probing

Stream backend availability is resolved through one cached entry point:
`cuprum._backend._check_rust_available()`.

`_check_rust_available()` first checks the testing override
(`set_rust_availability_for_testing`). While active, that override
short-circuits availability resolution before the raw import probe runs, and
`set_rust_availability_for_testing()` clears both
`_check_rust_available.cache_clear()` and `get_stream_backend.cache_clear()`;
otherwise the resolver falls back to that raw import probe. Cached answers only
drift if a long-lived interpreter survives a wheel swap or another out-of-band
import-path or installation-state change.

User callers should use `cuprum.is_rust_available()`, which delegates to
`_check_rust_available()`, so the public answer and dispatch resolver cannot
diverge within a process.

Issue `#128` is resolved in this path: the public helper and backend dispatch
now share the same cached resolver.

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

### Rejection classifiers for `safe_path` and `git_ref`

`cuprum/builders/args.py` exposes `classify_path_string` and `classify_git_ref`
alongside the `PathRejection` and `GitRefRejection` enums. These are the single
source of truth for *why* an input is rejected: each enum member's value is the
exact `ValueError` message, and `safe_path` / `git_ref` raise directly from the
classifier, so the categories cannot drift from the validators. The intended
reuse policy is:

- These are **developer-facing** helpers, deliberately omitted from the
  module's `__all__` (whose public surface is `safe_path`, `git_ref`, and their
  `SafePath` / `GitRef` return types). They remain importable for in-tree use
  and tests, but are not advertised as end-user API.
- **In-tree callers and tests** may depend on the classifiers to assert on the
  rejection *category* (rather than a brittle message substring) — this is what
  the property tests in `cuprum/unittests/test_args_validators_property.py` do.
- The enum member *names* are the stable contract; enum *values* (messages) may
  be reworded, so match on the member, not the string.
- New validation rules must be added to the classifier (and a matching enum
  member) rather than inline in the validators, keeping the reason taxonomy
  authoritative. Preserve the declared member order — the classifier returns
  the first matching category.

### Stream-backend resolution seam

`cuprum/_backend.py` keeps the same public contract for `get_stream_backend`
(the algorithm documented in the design document's stream-backend section) but
factors its internals into three private helpers so each can be reasoned about
and property tested in isolation:

- `_parse_backend_value(raw)` — pure parsing of a `CUPRUM_STREAM_BACKEND` value
  (whitespace/case normalization, empty → `AUTO`, unknown → `ValueError`),
  taking the raw string so tests inject values without mutating `os.environ`.
- `_probe_rust_availability(requested)` — the impure availability probe,
  encapsulating each mode's failure policy (`PYTHON` never probes; `AUTO`
  tolerates a probe `ImportError`; forced `RUST` propagates it).
- `_resolve_backend(requested, *, rust_available)` — the pure decision core
  that never leaks `AUTO` and raises `ImportError` for forced-`RUST`-unavailable
  (`rust_available` is keyword-only).

`get_stream_backend` composes them inside one boundary `try`/`except`, so a
forced-`RUST` failure always emits the `cuprum.stream_backend_unavailable`
warning whether the probe reports unavailable or itself raises. This is an
internal decomposition for testability; the observable behaviour and precedence
are unchanged.

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

## `cuprum/context/` package layout

`cuprum/context.py` exceeded the 400-line ceiling and mixed four concerns with
different audiences and change cadences, so it is now a package whose context
surface is re-exported from `cuprum/context/__init__.py`:

- `cuprum/context/env_overlay.py` — pure overlay merging
  (`merge_env_overlays`, `resolve_env`, `_coerce_env_overlay`); no `ContextVar`
  dependency.
- `cuprum/context/core.py` — the domain dataclasses (`CuprumContext`,
  `ScopeConfig`), the `ContextError` package-level root of the domain exception
  hierarchy and its `ForbiddenProgramError` subclass, timeout validation, and
  the before/after hook type aliases.
- `cuprum/context/state.py` — the `ContextVar` plumbing (`current_context`,
  `get_context`, and the internal set/reset helpers).
- `cuprum/context/registration.py` — `scoped`, the `_TokenRegistration`
  base, the registration handles, and the `allow`/`before`/`after`/`env`/
  `observe` factories.

Most context importers are unaffected. `ExecHook` is the exception: its
definition site is `cuprum.events`, it remains available from top-level
`cuprum`, and it is not re-exported by `cuprum.context`. Keep each module under
400 lines; new context features go in the module matching their concern.

## Pipeline execution helper contracts

Pipeline and command execution use small internal helpers whose names expose
their command-query separation contract:

The separate `_enforce_allowlist` and `_collect_hooks` operations supersede the
former combined `_run_before_hooks` helper. Keep authorization as an explicit
command and hook collection as a side-effect-free query; do not recreate the
combined boundary in `_pipeline_types`.

- `_enforce_allowlist(cmd)` is a command.  It reads the active context and
  raises `ForbiddenProgramError` when `cmd.program` is not allowed.  It must
  run before any before-hook dispatch.
- `_collect_hooks(ctx)` is a query.  It copies the hooks already present on
  `ctx` and does not enforce access, dispatch hooks, or mutate the context.
- `_emit_exec_event(hooks, event)` is a dispatcher query over hook return
  values.  It invokes synchronous observe hooks inline, schedules awaitable
  hook results as `asyncio.Task` instances, and returns those tasks to the
  caller.  If a later hook raises, `_ExecEventEmissionError` carries the tasks
  scheduled before the failure so cleanup can still await them.
- `_write_to_stream_writer(writer, chunk)` is a write command with a semantic
  result.  It returns `_WriteOutcome.OPEN` after a successful write/drain and
  `_WriteOutcome.CLOSED` when the downstream pipe closes early.  The caller
  keeps writer ownership and closes it exactly once.

`SafeCmd.run()` enforces the allowlist, then collects hooks from the current
context, emits the `plan` event, runs before-hooks, and delegates subprocess
execution to `_execute_with_hooks`.  Pipeline execution follows the same
ordering per stage: `_build_pipeline_observations` enforces every stage before
collecting hooks, then `_emit_plan_events_and_run_before_hooks` emits and
dispatches.

Observe hooks can return awaitables.  Single-command execution and pipeline
execution both keep a `pending_tasks` list and pass it into every
`_StageObservation`.  For pipelines, all stage observations share that list on
the same asyncio event loop.  Appending a task is synchronous Python bytecode
and the event loop does not pre-empt an `emit()` call halfway through the list
append, so no explicit lock is required.  Do not call `_StageObservation.emit`
from a worker thread; marshal back to the execution loop first.

Private helpers emit diagnostic logs rather than installing a global metrics or
tracing backend.  Hook scheduling and hook failures use the
`cuprum._observability` logger with structured `extra` fields such as
`cuprum_phase`, `cuprum_program`, `cuprum_error_type`, and
`cuprum_scheduled_task_count`.  Stream early-close decisions use warning-level
records on the `cuprum._streams` logger and include `cuprum_discarded_bytes`
when upstream bytes are drained after the downstream writer has closed.
Suppressed writer cleanup failures remain debug-level diagnostics with
`cuprum_operation` and `cuprum_error_type`, because they are expected during
already-closed pipe teardown.  User-facing metrics and spans remain the
responsibility of observe-hook adapters such as `MetricsHook` and
`TracingHook`, which consume `ExecEvent` values without coupling core execution
to a telemetry vendor.

`MetricsHook` keeps that consumption in two halves. The pure
`_metric_operations` reducer maps an `ExecEvent` to a tuple of `_CounterOp` and
`_HistogramOp` records, and `_apply` is the only step that reaches the
`MetricsCollector`. The reducer is therefore the single source of truth for
which counters and observations each phase yields, and it can be property
tested without a collector at all — `test_metrics_adapter_stateful.py` drives
random event streams through it and checks the accumulated totals against an
independent phase-count oracle.

Two consequences are worth preserving when changing the mapping. Labels are
projected only when the reducer yields at least one operation, so a `plan`
event never touches `event.program` or the project tag. And the reducer is
total over `ExecPhase` — all seven phases (`plan`, `start`, `stdout`,
`stderr`, `stdin`, `stdin_error`, `exit`) have an arm — and fail-closed beyond
it: any other phase raises `_UnhandledMetricsPhaseError` rather than being
silently dropped. That is deliberate, and its cost is worth stating plainly. A
hook exception is not swallowed, so adding a value to `ExecPhase` without
adding an arm here would raise for every caller that has already registered
`MetricsHook`. A new phase therefore cannot reach metrics without a decision in
this reducer. The structured logging adapter is fail-open by contrast,
formatting an unrecognized phase generically.

Applying the operations is deliberately non-atomic, and that is a contract
collectors rely on rather than an implementation detail. An `exit` event yields
up to two operations — the failure counter, then the duration observation —
applied as separate collector calls in that order, so a collector that raises
on the second leaves the first applied. The exception propagates out of the
hook and is not swallowed: `_emit_exec_event` logs `observe_hook_failed` and
wraps the error in `_ExecEventEmissionError` so already-scheduled observe tasks
survive cleanup, then `_StageObservation.emit` unwraps it and re-raises the
collector's original exception — so a raising collector fails the user's
command. A collector that must not do that has to swallow its own errors. The
labels are extracted once before the loop and are read-only within it. A
collector must therefore treat each call as independent and never infer a
duration observation from a failure increment. No operation identifier
is passed either, so a repeated call increments again — nothing here is
idempotent, and the hook never retries. See the metrics-hook dispatch figure in
[the design document](cuprum-design.md) for the full statement, and
`test_metrics_adapter_stateful.py` for the case that pins it.

### Choosing a test shape per observe hook

The three observe-hook adapters are verified differently, and the difference is
driven by whether the hook accumulates state across events rather than by
preference:

Table 1: verification shape for each observe hook, and why

| Hook | Shape | Why |
| --- | --- | --- |
| `TracingHook` | `RuleBasedStateMachine` (`test_tracing_span_stateful.py`) | holds `_active_spans` keyed by `ExecId`; the interesting bugs are correlation and drain failures across interleaved events |
| `MetricsHook` | `RuleBasedStateMachine` (`test_metrics_adapter_stateful.py`) | accumulates counters and histograms, checked against an independent phase-count oracle |
| `structured_logging_hook` | `@given` properties (`test_logging_adapter_properties.py`) | holds no state at all: one record per event, no map to drain |

A state machine over the logging hook would generate interleavings that cannot
distinguish any two implementations, because nothing carries between events.
Its risks are per-event and shape-dependent instead: an `extra` key colliding
with a reserved `LogRecord` attribute — which raises inside the caller's
logging stack, not in cuprum — a phase falling through the level map, and a tag
value that `JsonLoggingFormatter` cannot serialize. `ExecEvent.tags` is typed
`Mapping[str, object]`, so arbitrary values are in contract, which is why
`_json_serializable` and `json.dumps(..., default=str)` both exist and why the
generator must produce values that are not JSON-native.

Only `TracingHook` has an active map, so "the active map drains correctly" is a
claim about that hook alone; `test_tracing_span_stateful.py` asserts it directly
by cross-checking `hook._active_spans` against a model after every step.

Stream pumping continues to drain the upstream reader after
`_write_to_stream_writer` reports `_WriteOutcome.CLOSED`.  `_pump_stream`
closes the writer in a `finally` block, so writer cleanup runs after success,
exceptions, and cancellation.  Tests cover this contract at four levels:

- direct helper tests in `cuprum/unittests/test_cqrs_helpers.py`
- runtime behaviour and stream-drain properties in
  `cuprum/unittests/test_stream_pump_runtime_behaviour.py`
- observe-task assertions in `cuprum/unittests/test_observe.py`
- CQRS hook and task-scheduling behaviour in
  `cuprum/unittests/test_cqrs_hook_behaviour.py`, covering allowlist
  enforcement, before/after/observe hooks, and async observe-task scheduling

## Timeout and fail-fast reducer verification

The temporal, branch-heavy subprocess-timeout and pipeline-cleanup paths are
backed by two pure reducers, each verified twice: Hypothesis samples them
randomly, and CrossHair confirms the same invariants symbolically over a
bounded state space. Keep both layers when changing either reducer.

`_resolve_timeout_payload` (`cuprum/_subprocess_timeout.py`) — the
timeout-payload seam. The symbolic contracts confirm that:

- a carried `_SubprocessTimeoutError` returns exactly its own timeout, stdout,
  stderr, and exit time, independently of the fallback (the fallback in that
  contract deliberately holds different values and a `None` configured timeout,
  so a resolver that consulted it would return a wrong field or raise);
- a bare `TimeoutError` with a configured timeout present returns exactly the
  fallback's timeout, stdout, stderr, and exit time;
- a bare `TimeoutError` with `configured_timeout is None` raises
  `_SubprocessInvariantError` rather than inventing a timeout.

`_stages_to_terminate` (`cuprum/_process_lifecycle.py`) — the fail-fast
selection. The symbolic contracts confirm that:

- every selected index is in range, unique, and ordered;
- no selected index is the `failure_index`;
- every selected index corresponds to a stage whose `done` flag is `False`;
- the selection is exactly the set of unfinished, non-failed indices;
- cleanup is idempotent — after marking the selected stages done, a second
  invocation selects nothing.

Run the verification with either of:

```bash
uv run pytest -q cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py
uv run crosshair check \
  cuprum/unittests/test_subprocess_timeout_reducers_crosshair.py \
  --analysis_kind=PEP316
```

The ordinary Hypothesis module remains alongside it:

```bash
uv run pytest -q cuprum/unittests/test_subprocess_timeout_reducers.py
```

### Deliberate bounds

The symbolic domains are kept small and finite so CrossHair exhausts them
rather than returning `CANNOT_CONFIRM`:

- pipelines are capped at three stages, with `failure_index` constrained to a
  valid index by precondition;
- the per-stage `done` flags are encoded as a single bounded integer bitmask
  rather than a symbolic list of symbolic booleans, which is what makes the
  space enumerable;
- timeouts and exit times come from a three-value enumeration instead of
  unrestricted floats, and stdout/stderr from a three-value enumeration that
  includes `None`. The reducers only ever copy these values, so representative
  values suffice — what matters is that carried and fallback values stay
  distinguishable, which the enumerations preserve.

These checks run in CI rather than only on demand: the module matches the
`cuprum/unittests/test_*.py` pattern in the Makefile's `PYTEST_TARGETS`, so
`make test` collects and executes it. `check_states` requires
`MessageType.CONFIRMED`, so an available-but-unconfirmed result
(`CANNOT_CONFIRM`) or a refuted postcondition (`POST_FAIL`) fails the run
instead of being downgraded to a skip or a warning. Availability is probed
through the shared helpers in `cuprum/unittests/_crosshair_support.py`, which
degrade to a skip only for a missing CrossHair dependency (`ImportError`) or an
interpreter whose opcode set the tracer cannot handle
(`crosshair.tracers.TraceException`, as with the `CALL_KW` gap on early Python
3.15 betas, issue `#109`); every other failure is re-raised. Supported
interpreters therefore confirm the contracts rather than skipping them.

### Pipeline stream module boundaries

A pipeline's byte movement is split three ways, so each module has one reason
to change:

Table 1: modules owning each part of a pipeline's byte movement

| Module                    | Owns                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------- |
| `_pipeline_streams.py`    | *how* bytes cross one hop — backend choice, the Rust hand-off, the Python pump        |
| `_pipeline_pipe_tasks.py` | the *tasks* carrying them — creation per stage pair, cancellation, outcome collection |
| `_pipeline_stream_fds.py` | the raw-descriptor lifecycle behind the Rust hand-off                                 |

`_pipeline_pipe_tasks` imports `_pump_stream_dispatch` from
`_pipeline_streams`, never the reverse, so the dependency runs one way: task
bookkeeping depends on the pump, and the pump knows nothing about tasks.
Callers that need pipe-task helpers — `_pipeline_wait`, `_process_lifecycle`,
`_pipeline_internals` — import them from `_pipeline_pipe_tasks` directly rather
than through a re-export, which would reintroduce the cycle.

`_surface_unexpected_pipe_failures` lives with the tasks because it encodes
which task outcomes a pipeline tolerates: `BrokenPipeError` and
`ConnectionResetError` are the expected result of a downstream stage exiting
early, and everything else is a genuine failure that must reach the caller.

### Rust pump raw-descriptor lifecycle

Routing an inter-stage hop through the Rust pump means taking the raw pipe
descriptors back from asyncio for the duration of the transfer.
`cuprum/_pipeline_stream_fds.py` owns that hand-off, keeping its
partial-failure paths in one place rather than inlined in the pump:

- `_extract_stream_fd` pulls the raw descriptor out of an asyncio transport,
  returning `None` when the transport does not expose one.
- `_BlockingModeGuard` is the FD-state object. `engage()` switches both
  descriptors to blocking mode while capturing their prior modes, rolling back
  a partial change if the second switch fails; `restore()` returns them to the
  captured modes. This is what stops a descriptor being left blocking.
- `_paused_reader` is a context manager that pauses the reader transport and
  resumes it on every exit path, including exceptions and cancellation. Only a
  pause that took effect is resumed. It yields whether the descriptor may be
  handed over: a *failed* pause answers `False`, because asyncio may still be
  consuming the reader, and the caller falls back to the Python pump rather
  than racing it. A transport exposing no `pause_reading` answers `True`, since
  there are no callbacks to suspend. A transport exposing `pause_reading` but no
  `resume_reading` answers `False` without being paused: its pause hook is
  evidence of callbacks that would race the pump, but a pause nothing could
  undo would strand the Python fallback on the same stream.

Cancellation is handled explicitly. `run_in_executor` cannot interrupt the
worker thread running the Rust pump, and that thread still owns both
descriptors, so `_await_rust_pump` shields the executor future and drains it
before propagating `CancelledError`. Restoring the blocking mode or resuming
the transport any earlier would hand the descriptors back to asyncio while
native code was still mid-transfer.

The module's reuse policy is narrow: further descriptor-lifecycle concerns for
this hand-off belong here, but the seams are not a general-purpose descriptor
utility. Anything serving a different caller should be designed against that
caller's real requirements instead of widening these.

#### Observing a declined hand-off

Each of those partial failures ends the same way: the hop falls back to the
Python pump and completes correctly. That is the point — the fall-back is not
an error, and nothing surfaces to the caller. It does mean a deployment that
has quietly stopped taking the fast path is indistinguishable from one that
never had it, which is the question an operator actually asks.

`_log_rust_pump_declined` in `cuprum/_pipeline_streams.py` records each decline
against the `cuprum._pipeline_streams` logger, following the same convention as
the pipeline fail-fast records: a `cuprum_action` of `rust_pump_declined` plus a
`cuprum_reason` naming the seam that refused.

Table 2: `cuprum_reason` values and the seam each one reports

| `cuprum_reason`             | Seam that declined                                                 |
| --------------------------- | ------------------------------------------------------------------ |
| `raw_fd_unavailable`        | `_extract_stream_fd` found no descriptor on at least one transport |
| `reader_pause_failed`       | `pause_reading()` raised, so asyncio may still be consuming        |
| `blocking_mode_unavailable` | `_BlockingModeGuard.engage` could not switch both descriptors      |

These are logged at `DEBUG`, deliberately. A fall-back is a per-hop routing
decision rather than a fault, so promoting it to a warning would make a
correctly-working pipeline noisy on any platform where the fast path does not
apply. To diagnose fast-path coverage, raise that one logger:

```python
import logging

logging.getLogger("cuprum._pipeline_streams").setLevel(logging.DEBUG)
```

`cuprum/unittests/test_pipeline_streams_observability.py` pins each reason to
the real code path that emits it, so a decline that stops being recorded fails
the suite rather than going unnoticed.

A pump failure can also be masked by cancellation. `asyncio.wait` never
retrieves a future's outcome, so when a hop is cancelled
`_report_pump_outcome_after_cancel` consumes the worker's future and records
any error it carried under a `cuprum_action` of
`rust_pump_failed_after_cancel`, with the traceback attached via `exc_info`.
The caller is told about the cancellation it asked for; without this record the
pump's own error would resurface at collection time as an unretrieved-exception
warning, detached from the hop that caused it.
`cuprum/unittests/test_pipeline_streams_cancellation.py` pins the field.

Neither record has a counter beside it, and that is a deliberate limit rather
than an oversight. `MetricsHook` is the only seam in the library that reaches a
metrics collector, and it dispatches on `ExecEvent.phase`, whose `case _` arm
raises `_UnhandledMetricsPhaseError` (`cuprum/adapters/metrics_adapter.py`).
Introducing a phase for these events would therefore raise inside every
`MetricsHook` a caller has already registered rather than being ignored by it,
so the counter cannot be added without first changing that contract.
Cardinality is not the obstacle: `_RustPumpDeclineReason` is a closed
three-member `StrEnum`, so a `reason` label is bounded by construction. This
matches Proposal 3 of [ADR-002](adr-002-additional-rust-components.md), which
holds Rust pump counters out of the public runtime API until their stability
and cardinality are proven. Until a metrics path exists that does not run
through `ExecPhase`, `cuprum_action` and `cuprum_reason` are the supported way
to count these events, aggregated by the log pipeline.

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

When echoing, `_drain` writes raw bytes to sinks with a `.buffer`. For
text-only sinks, it owns an incremental decoder configured with
`config.encoding` and `config.errors`, then flushes that decoder at end of
stream. This preserves multibyte characters that span read chunks.

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
(`bytearray`), the line-emitting variant's `codecs.IncrementalDecoder`, and any
text-only echo decoder. The `on_chunk` callback closes over the line decoder
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

`cuprum/adapters/_support.py` keeps the three telemetry adapters from repeating
the same event projection and in-memory collector locking. It owns the
canonical logging/tracing `(key, value)` projection helper for optional
execution fields, the `project` tag helper, the shared unhandled-phase debug
log, and `_LockedStore` with its lock-guarded `reset()`. Each adapter retains
backend-specific key names and value shaping, such as tuple versus list `argv`,
at its call site.

This module was extracted to prevent three-way projection drift and keep each
adapter within its cohesion budget. It is importable only by adapter modules:
do not add backend rendering, logging configuration, event construction, or
general-purpose utilities. Production imports stay adapter-only; contract tests
such as `cuprum/unittests/test_adapter_projection.py` may import
`_event_common_fields` to pin the projection contract. Add an adapter-visible
`ExecEvent` field once to `_event_common_fields`; new in-memory collectors
derive from `_LockedStore` and implement `_clear()` while its lock is held.

`cuprum/unittests/test_adapter_projection.py` pins this contract with
Hypothesis properties and redacted per-phase syrupy snapshots.

### Build and test worker controls

`make test` runs pytest serially by default. Set `PYTEST_WORKERS` to a positive
worker count to enable xdist explicitly. Set `BUILD_JOBS=-jN` to pass the same
count to Rust test commands and, through `CARGO_JOB_ENV`, to both
`RAYON_NUM_THREADS` and `CARGO_BUILD_JOBS`.

## Tracing adapter span lifecycle

`cuprum/adapters/tracing_adapter.py` provides `TracingHook`, an observe hook
that turns the `ExecEvent` stream into OpenTelemetry-style spans. It depends
only on the `Tracer` and `Span` protocols, so any backend that implements them
can be plugged in. `cuprum/adapters/tracing_memory.py` supplies
`InMemoryTracer` and `InMemorySpan`, the reference doubles used by tests and
examples: `InMemoryTracer` collects spans in memory and protects its span store
through the shared `_LockedStore` lock (its mutators, and `reset()`, run under
that lock), while `InMemorySpan` is a plain mutable record that provides no
synchronization of its own.

**Phase dispatch.** `TracingHook.__call__` matches every `ExecEvent.phase` in a
single `match`, and each phase falls into exactly one of four categories: span
lifecycle (`start` opens a span, `exit` ends it), span event (`stdout`,
`stderr`, and `stdin_error` record a `cuprum.<phase>` event on the already-open
span), deliberately ignored (`plan` and `stdin` carry no tracing semantics), or
unhandled (the `case _` logs via `_log_unhandled_phase` instead of failing
silently or raising). A new phase should be slotted into this policy rather
than given an ad-hoc side path.

**State model.** `TracingHook` keeps `_active_spans`, a dictionary keyed by
`ExecEvent.exec_id` (the per-execution correlation token), guarded by an
internal `threading.Lock`:

- **start** builds the span outside the lock, then, under the lock, swaps the
  mapping atomically — capturing any span already registered for that `exec_id`
  (a duplicated or reused token) and installing the replacement in one critical
  section. The detached stale span is then marked failed and ended *outside*
  the lock, so an arbitrary `Span` that blocks on I/O in `set_status()`/`end()`
  cannot stall other executions' handlers; each replaced span is still ended
  exactly once because it is already unreachable via the map.
- **stdout/stderr/stdin_error** all route through the single
  `_record_span_event` helper: it looks up the span for the event's `exec_id`
  under the lock, then, outside the lock, copies whichever of the `line`,
  `operation`, `error_type`, and `note` fields are set on the event onto a
  `cuprum.<phase>` span event (for example `cuprum.stdout` or
  `cuprum.stdin_error`). New event-recording phases should extend this shared
  field set rather than add a bespoke per-phase method. The helper never sets
  the span status or ends the span — only `exit` does that — so a `stdin_error`
  (the child process may legitimately ignore its stdin) is recorded as a
  diagnostic without failing or closing the execution span. `stdout`/`stderr`
  recording is gated by the hook's `record_output` flag; `stdin_error` is
  recorded unconditionally, so a stdin-write failure stays diagnosable even
  when line-by-line output recording is switched off.
- **exit** removes (pops) the span for the event's `exec_id` under the lock,
  then sets the exit attributes and status and ends the span outside the lock.

Keying on `exec_id` rather than PID is what stops a recycled PID, or delayed
output/exit from an earlier execution, from attaching to a later execution's
span. `pid` is retained only as the `cuprum.pid` span attribute for
observability.

**Legacy or manual events.** An event whose `exec_id` is `None` (a legacy or
hand-constructed event) cannot be correlated, so it is ignored rather than
guessed from PID: a `start` without an `exec_id` creates no span, and `stdout`/
`stderr`/`stdin_error`/`exit` without one are dropped. Every event Cuprum
itself emits carries an `exec_id`, so this only affects hand-built event
streams.

## Canonical `_TokenRegistration` handle base

All `ContextVar`-backed scope-registration handles — `AllowRegistration`,
`HookRegistration`, and `EnvRegistration` in `cuprum/context/registration.py` —
derive from one canonical `_TokenRegistration` base. The base owns the `_token`/
`_detached` pair, the idempotent `detach()`, the context-manager protocol, and
the `_install(new_ctx)` step that sets the derived context and captures the
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
register/detach sequences across all token-backed handle types (nesting,
context-manager exit, LIFO detach, double-detach), plus an example test pinning
the documented non-LIFO hazard.

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
- `submission_indices[i]` is the original position of `final_results[i]` in
  `inputs`, so the submission order is recoverable after compaction.

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

## ConcurrentResult submission mapping

`ConcurrentResult` (in `cuprum/concurrent.py`) exposes the compacted results
alongside a submission-stable mapping so callers can relate any result — or
failure — back to the command they submitted:

- `submission_indices` is a tuple parallel to `results`; each entry is
  the original submission index of the corresponding result. It defaults to the
  identity sequence when omitted (the constructor treats `None` as "omitted"),
  and a supplied sequence whose length differs from `results` raises
  `ValueError`.
- `failure_submission_indices` maps each entry of `failures` through
  `submission_indices`, yielding the original submission positions of the
  failed commands. Unlike `failures` (positions within the possibly compacted
  `results`), it is stable across collect-all and fail-fast modes.

## Environment overlay resolution

The user-facing `env(...)` context manager and the related `ScopeConfig` field
carry an *overlay-only* mapping that is layered on top of the live `os.environ`
at subprocess spawn time. The implementation sits in
`cuprum/context/env_overlay.py` and is built on three cooperating helpers:

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
comparison. Its CI profile places each matched Python/Rust scenario pair next
to each other and records ten measured runs per command, reducing temporal
runner drift and three-sample outliers. Dry-run plans record
`benchmark_profile_version` and `worker_iterations`; ratchet comparison skips
older baseline artefacts whose profile metadata does not match the current
benchmark shape.

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
- The `_EnvBackendSelector` concurrency coverage is itself split across two
  modules sharing a common support module, keeping each file's responsibility
  count within the cohesion budget:
  - `cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py` — the
    `_BACKEND_LOCK` `RLock` reentrancy guarantee, plus same-thread re-entrant
    selector rejection, recovery, and the structured warning log (snapshot).
  - `cuprum/unittests/test_tee_profile_worker_concurrency.py` — concurrent
    `run_tee_profile_worker` race-freedom across backend pairs and
    `CUPRUM_STREAM_BACKEND` preservation under concurrent, interleaved access.
  - `cuprum/unittests/_tee_profile_concurrency_support.py` — the coordinating
    backend selectors and race harness, plus the timeout constants, worker
    plumbing, and the thread join/assert helper used by those tests.
  - `cuprum/unittests/_tee_profile_signalling_lock.py` — the instrumented
    `_SignallingRLock` wrapper that flags first-observed lock contention.
  - `cuprum/unittests/_tee_profile_backend_support.py` — backend-availability
    detection and the Hypothesis backend strategies/parametrization shared by
    the concurrency and reentrancy modules.
- `cuprum/unittests/test_tee_profile_worker_selector_metrics.py` covers the
  selector observability metrics (`lock_wait_seconds`,
  `reentrant_rejection_count`): their accumulation, thread-locality, reset per
  run, and presence in the worker result payload.

Keeping the concerns in separate files makes the coverage boundary explicit: a
change to command construction touches the core module, a change to the CLI
contract touches the CLI module, a change to backend locking or the selector
state machine touches one of the two concurrency modules (with shared
scaffolding in the support module), and a change to selector metrics touches
the metrics module.

### `_EnvBackendSelector` concurrency invariants

The two concurrency modules verify the `_EnvBackendSelector` state machine that
serializes process-local backend selection. The selector is backed by a
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
run the two concurrency modules together:

```bash
uv run pytest cuprum/unittests/test_tee_profile_worker_selector_reentrancy.py \
  cuprum/unittests/test_tee_profile_worker_concurrency.py
```

## Rust availability probe stack

The Rust availability API uses a single source of truth:

- `cuprum.rust.is_rust_available()` is the public entry point and returns
  `cuprum._backend._check_rust_available()`.
- `_backend._check_rust_available()` is `functools.lru_cache(maxsize=1)` and
  short-circuits to `set_rust_availability_for_testing()` while an override is
  active; otherwise it delegates to the private `_rust_backend.is_available()`
  probe.
- `get_stream_backend()` reads the same cached resolver, so
  `CUPRUM_STREAM_BACKEND` dispatch and calls to `cuprum.is_rust_available()`
  stay aligned.
- Tests can force a deterministic result via
  `set_rust_availability_for_testing(is_available=...)`, which also clears the
  two relevant caches.

Keep tests that bypass `cuprum.is_rust_available()` focused on this private
layer, because `_rust_backend.is_available()` is an uncached import probe
without lifetime caching.

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
(`From<PumpError> for PyErr`). The overflow variants surface as plain
`OSError`. The non-fatal write classification (broken pipe / connection reset)
lives on the enum as `PumpError::is_nonfatal_write`, replacing the free
function the splice and read/write paths previously shared. New failure
conditions get a variant here rather than a stringly-typed
`io::Error::other(...)`.

### Preserving the operating-system error code

The `Io` variant needs care, because PyO3's own `From<io::Error> for PyErr`
loses the number. It selects the exception *type* from `io::ErrorKind` and then
constructs it with a single argument, the error's `Display` string — and Python
populates `OSError.errno` and `OSError.strerror` only when it receives **two or
more** arguments. The number therefore survived in the message text and nowhere
a caller could branch on, forcing callers to parse English to tell `EBADF` from
`EPIPE`. This was issue `#265`; `io_error_to_py_err` now handles it.

The construction is platform-specific, so it sits behind a `cfg`-selected
`os_error_to_py_err`:

- **Unix** — `raw_os_error` *is* an `errno`, so the two-argument form
  `OSError(code, strerror)` is exactly right. It also fixes the exception type
  for free: CPython maps the errno to the matching subclass itself, so
  `OSError(32, ...)` *is* a `BrokenPipeError` and reading a directory raises
  `IsADirectoryError`. That is the same subclass selection PyO3 reached for
  through `ErrorKind`, taken from the authoritative source instead of a
  parallel table.
- **Windows** — `raw_os_error` carries a `GetLastError` code, not an `errno`.
  Passing it as an `errno` would assign an unrelated number
  (`ERROR_INVALID_HANDLE` is 6, which as an `errno` is `ENXIO`) and pick the
  subclass from it. The five-argument form `OSError(errno, strerror, filename,
  winerror, filename2)` is the one that carries a native code: given a
  `winerror`, CPython ignores the `errno` argument, derives `errno` from the
  Win32 code, and selects the subclass from the derived value, so all three
  agree.

Two details are worth knowing before changing this code. First, `io::Error`
renders a raw OS error as `"{strerror} (os error {code})"`, so the suffix is
stripped before it becomes `strerror` — otherwise the number appears twice, as
`"[Errno 9] Bad file descriptor (os error 9)"`. `strip_os_error_suffix` is
anchored to *this* error's own code so it can never truncate a message
belonging to a different one; three proptests pin that. Second, an `io::Error`
with no `raw_os_error` — one synthesized in Rust rather than returned by a
syscall — has no number to preserve, so PyO3's `ErrorKind` mapping remains the
best available and is used unchanged.

`cuprum/unittests/test_rust_errno.py` covers both arms, with each set of
assertions scoped to the platform whose taxonomy it names — the POSIX cases
name an `errno` and the subclass CPython derives from it, the Windows case
names a `winerror` and the `errno` CPython derives from *that*. The Windows
case does not hard-code either expectation: it reads them back from an
`OSError` it builds from the observed `winerror`, so it pins the derivation
without depending on which Win32 code the failure happens to raise.

What actually executes is narrower than what is written, so do not read a
green run as coverage of both arms:

- **POSIX** — the cases run natively on Linux whenever the extension is built,
  so a local `make develop` followed by `make test-extension` executes them.
  `cuprum/unittests/test_rust_errno.py` is one of the `EXTENSION_TEST_TARGETS`,
  so CI's `extension-tests` job runs them with
  `CUPRUM_REQUIRE_RUST_EXTENSION=1`: a build that never produced the extension
  fails rather than skipping quietly (`#258`).
- **Windows** — the `#[cfg(windows)]` arm is compiled natively on
  `windows-2022` by the wheel build (`.github/workflows/build-wheels.yml`,
  reached unconditionally from `ci.yml`, so it runs on every pull request),
  which catches a Windows arm that does not build or type-check. No job runs
  the Python suite on Windows, so nothing executes the `winerror` assertions;
  that gap is tracked by `#277`.

That wheel build is a plain `maturin build`, so it compiles the Windows arm
without `-D warnings`. It therefore catches a Windows arm that fails to
compile, and only that. Warn-level regressions pass it — and the cheapest way
to introduce one is to gate a helper on `#[cfg(unix)]` incorrectly, which makes
it dead code on Windows rather than a compile error. `make lint-windows` closes
that gap by running Clippy for the Windows target with the same `-D warnings`
posture the host gate uses:

```bash
rustup target add x86_64-pc-windows-msvc
make lint-windows
```

No Windows machine is needed: Clippy type-checks without linking, so the target
standard library is sufficient. `PYO3_CROSS_PYTHON_VERSION` is required and the
Makefile sets it, because PyO3 cannot probe an interpreter for the target
platform and so the ABI version has to be stated explicitly; keep the
`WINDOWS_PYTHON_VERSION` variable in step with the `python-version` the Windows
wheel job builds against.

`make lint-windows` is deliberately not part of `make lint`. It hard-fails when
the target standard library is absent rather than skipping, and requiring every
contributor to install a second standard library before any lint run is a poor
trade for a check this narrow. CI's `lint-test` job installs the target and
runs it on every pull request, which is where it has to hold.

This is the only check in the repository that reads the `#[cfg(windows)]`
branches with warnings denied. A trybuild case cannot substitute for it:
trybuild compiles its fixtures for the host target, so on a Linux runner it
sees the `#[cfg(unix)]` arm and never the Windows one.

## Rust FD-borrow ownership contract

The pump and consume entry points in `rust/cuprum-rust/src/lib.rs` sort every
descriptor they touch into a *borrowed* or a *consumed* role. `pump_stream`
borrows its reader and consumes its writer; `consume_stream` borrows its reader
and takes no writer at all. The borrow half is centralized in one helper,
`with_borrowed_reader`. The helper rebuilds a `StreamHandle` from the
caller-owned raw descriptor, wraps it in `ManuallyDrop`, and runs the caller's
closure against it. `ManuallyDrop` suppresses the close on *every* exit path —
a normal return and unwinding from a panicking operation alike — so a
descriptor the Python side still owns is never closed by Rust. `pump_stream` and
`consume_stream` both route their reader through the helper, keeping the
"borrow this FD without owning it" rule in a single place.

This supersedes an earlier pattern that reconstructed the handle and called
`std::mem::forget` after the inner operation returned. Because a panic unwinds
past the trailing `forget`, that pattern dropped — and therefore closed — the
caller-owned descriptor on the unwind path, exposing the Python transport to a
double close of the same FD (the `#125` panic-unwind hazard). `ManuallyDrop`
holds regardless of how the scope exits, so no drop guard or `forget` call is
required.

There is deliberately no borrowed *writer* variant. The writer FD handed to
`pump_stream` is consumed: it must close on drop — including during unwinding —
so downstream readers observe EOF. Reconstruct the writer with
`stream_from_raw` (which yields an owning handle) and let it drop; reserve
`with_borrowed_reader` for descriptors whose ownership stays with the caller.
The helper's safety contract obliges the caller to guarantee `fd` is a valid
open descriptor (or Windows handle) for the duration of the call and that
ownership remains with the caller; in return the helper guarantees it never
closes `fd`.

The contract is checked at two levels, which are deliberately not
interchangeable. `rust/cuprum-rust/src/lib_tests.rs` holds the
integration-level regression tests: they open *real* descriptors, run the
helper both normally and through a panicking operation, and assert the borrowed
FD is still open afterwards. That is the only place actual `close(2)` behaviour
is exercised, and it stays the authority on it. Alongside them,
`rust/cuprum-rust/src/fd_ownership_kani_proofs.rs` carries a bounded Kani proof
of the ownership invariant itself: a borrowed reader FD is never closed by Rust
on a normal or an unwinding exit, and a `pump_stream` writer is always consumed
and closes exactly once to signal EOF.

Kani does not interpret I/O, so the proof cannot use real descriptors. It runs
against the pure model in `fd_ownership_model.rs` — gated
`#[cfg(any(test, kani))]`, so `make test` exercises it as ordinary unit tests
too — where `ModelFd` records a close on drop instead of issuing one. The
`ManuallyDrop` wrapper, the closure call, and the early-exit edge are all real
Rust, so Rust's own drop elaboration decides the outcome rather than any
hand-written accounting. Because Kani compiles panics as aborts, the unwind
path is modelled with `?`: a `?` early return and a real unwind both leave the
frame without running the statements that follow the operation, so
reintroducing the superseded trailing-`mem::forget` makes the proof fail (that
mutation was run to confirm the proof is not vacuous). Being a bounded model
checker, Kani establishes this over an explicitly bounded state space — the two
exit modes, and at most three repeated borrows — rather than for all
executions. Active verification tracking, including whether Verus adds anything
beyond the Kani model once that model is complete, lives in issue `#89`.

## Rust splice-loop and drain contract

The Linux zero-copy path in `rust/cuprum-rust/src/splice.rs` follows one
canonical loop. `try_splice_pump` performs the first `splice_once` solely to
detect support: `EINVAL` on that first call signals unsupported descriptor
types and the read/write fallback. Every outcome thereafter — including the
first call's, which is fed into the loop — is handled by the same arms: `Ok(0)`
ends the transfer, `Ok(n)` accumulates, a non-fatal write error (broken pipe /
connection reset) drains the reader and reports the bytes transferred so far,
and anything else propagates.

`splice_once` retries at the syscall level: an interrupted splice (`EINTR`) is
re-issued rather than surfaced, matching the Unix read and write policies, so a
signal delivered mid-transfer does not spuriously fail an otherwise-healthy
pump. The retry loop is factored into `splice_once_with`, which a regression
test drives with an injected `EINTR`, mirroring the `read_raw_fd` coverage, and
a proptest exercises the retry across any number of interruptions and either
terminal outcome. The accumulation loop is factored the same way into
`accumulate_splices`, whose `next` (splice) and `drain` side effects are
injected so a proptest can assert the `Ok(0)` / `Ok(n)` / broken-pipe-drain /
fatal transitions over generated outcome sequences. Exhaustive bounded
model-checking of these paths with Kani remains tracked in issues `#87` and
`#88`.

`drain_reader` routes through the canonical raw-fd read helper
(`io_utils::read_raw_fd`), so it shares the Unix read policy with the
read/write fallback: interrupted reads (`EINTR`) retry instead of silently
ending the drain, end of file terminates it, and other errors propagate.
Behavioural tests in the module cover full pipe-to-pipe transfer, the fallback
signal for regular files, broken-pipe draining, the drain's EOF termination,
and the syscall-level `EINTR` retry.

The read/write fallback's write half routes through `io_utils::write_all_unix`,
whose partial-write loop is factored into `write_all_unix_with` — the
injectable write seam mirroring `read_raw_fd_with`. Unit tests drive it over a
scripted single-write operation so the write policy is exercised
deterministically without real descriptors: interrupted writes (`EINTR`) retry,
zero progress raises `WriteZero`, partial writes accumulate, over-long progress
surfaces `BufferRangeExceeded`, non-fatal short writes (broken pipe /
connection reset) report the bytes transferred so far, and other errors
propagate. Proptests confirm `PumpError::is_nonfatal_write` and
`map_short_write_error` suppress exactly `BrokenPipe`/`ConnectionReset` while
preserving the accepted byte total. These raw-fd helpers live in the `io_utils`
directory module (`io_utils/mod.rs`, with tests in `io_utils/tests.rs`), split
from the former single file to stay within the per-module line cap.

The read/write fallback's control flow — how each read and write outcome moves
the running byte total and the latched `writer_open` flag — is factored into a
pure, `io::Error`-free state machine in `src/pump_machine.rs`.

[Cuprum design](cuprum-design.md) §13.7, "Read/write fallback state machine",
is the normative reference for this API: it carries `advance`'s full signature
and the reasoning behind its shape. The notes below record the conventions that
govern using it, and name the types so a reader arriving from the `io_utils`
sections above knows what to look for.

`advance` is the machine's only production entry point. It takes a `PumpState`
by mutable reference, the raw read length, and a closure that performs one
write, and it returns a `Flow` — `Continue` to keep reading, or `Stop` on end
of input. `PumpState` holds exactly the two values the loop must not get wrong,
the running `total_written` total and the latched `writer_open` flag, exposed
through `total_written()` and `writer_open()` accessors rather than as public
fields, so only the machine may move them.

`advance` owns both decisions the loop would otherwise have to make for itself:
a zero-length read is end of input, anything else is a chunk; and the write
runs **only** for a chunk read while the writer is still open.
`pump_stream_files_readwrite`, the property tests, and the bounded proofs all
go through it, so there is one definition of that policy rather than three
copies that could drift.

The write closure yields a `WriteEvent`, the machine's `io::Error`-free view of
one write: `Complete { bytes }` accepted the whole chunk and leaves the writer
open, while `Closed { bytes }` carries the bytes accepted before a broken pipe
or connection reset and latches the writer shut for good. The sole production
adapter that builds one is `io_utils::classify_write`, which collapses a
`WriteOutcome` and the non-fatal error partition into those two variants; the
test-only `classify_write_with` shares its mapping through a `write(2)` seam.
Genuinely fatal failures never reach the machine at all — they propagate as
`PumpError` — which is what keeps `pump_machine` free of `io::Error` and so
tractable for Kani. Re-use policy: a new caller of the machine must classify
its writes through `classify_write` rather than constructing a `WriteEvent`
inline, otherwise "non-fatal" stops having one definition.

The precondition is enforced by the type rather than by a check. Internally
`advance` builds a private `Transition` — `Wrote(WriteEvent)`, `Drained`, or
`Eof` — and `Wrote` is constructible only on the branch that has already
established the writer is open. An earlier API took a read event and an
`Option<WriteEvent>` independently, which let a caller pass a write for an EOF
read or for a closed writer; both were silently ignored, so the precondition
lived in a doc comment. Making those states unrepresentable also removed the
defensive re-check inside `step`, which had been masking exactly the mistake it
looked like it was guarding against.

That encapsulation is what the proptests and Kani proofs assume rather than
establish — neither can observe a transition it cannot spell — so it is pinned
separately by the compile-fail case
`rust/cuprum-rust/tests/ui/fail/pump_transition_unreachable.rs`. That case
includes `src/pump_machine.rs` as a child module, reproducing the relationship
`lib.rs` has with it, and asserts the compiler's refusal of an attempt to build
a `Transition::Wrote` and pass it to `step` from the parent. Widening either
item, even to `pub(crate)`, changes the diagnostic and fails the case, so the
"`advance` is the only constructor" claim cannot quietly stop being true.

Whether the write *ran* is what the tests assert, not merely its effect: a
dropped precondition would leave the resulting state identical and differ only
in having performed a spurious write, so the drivers report invocation.

Extracting the decision this way lets it be checked without descriptors:
proptests fold random scripts of `(read_len, WriteEvent)` through `advance` to
assert the total is monotonic, the writer never reopens once a broken pipe
latches it closed, a closed writer drains without accruing bytes, the loop
stops exactly on a zero-length read, and the write runs exactly when the
transition permits it. Kani proves the same invariants over unbounded byte
counts and arbitrary starting states in `src/pump_machine_kani_proofs.rs`
(`#[cfg(kani)]`, so outside the commit gate), closing the model-checking
follow-up from issue `#84`. Those proofs target `advance` rather than the
private `step`, because `advance` is where the precondition lives — proving
`step` in isolation would establish nothing about a closed writer.

Neither the proptests nor the proofs call `advance` directly. Both go through
`drive`, a `#[cfg(any(test, kani))]` helper beside it that supplies the write
closure and returns the `Flow` alongside whether the closure was invoked. That
is deliberate: it keeps the two harnesses driving the machine identically, and
it is where the "was the write attempted?" observable comes from. Add new
harnesses through `drive` rather than reconstructing the closure in each one.

The path emits bounded `tracing` diagnostics at the three boundaries operators
need visibility into: support detection logs a `debug` event when the
descriptors cannot splice and the read/write fallback takes over; the
`splice_once` `EINTR` retry logs a `trace` event per re-issue (the lowest
level, filtered out by default, so signal-heavy workloads stay quiet unless
trace is enabled); and the broken-pipe drain logs a `debug` event carrying the
`bytes_transferred` so far. Messages are static and low-cardinality. Following
the library convention, the crate emits instrumentation but installs no
subscriber — the embedding application (the Python command boundary) owns
subscriber configuration and higher-level command observation, and `PumpError`
values with transfer counts are still returned across that boundary as the
primary signal.

The read/write fallback loop is instrumented to the same standard. Each seam
emits a `debug` event per successful read or write (carrying the byte count and
platform), a `warn` event on every `EINTR` retry, and an `error` event on a
fatal read/write failure, a zero-progress write, or a length-conversion
overflow — so no fatal boundary stays silent. The `pump_stream_readwrite` and
`consume_stream` loops wrap these seams in an operation span
(`io_utils::operation_span`) that carries the `operation` and `buffer_size`
and, on completion, records `total_bytes` and the cumulative `EINTR` retry
counts as structured fields. `pump_stream_readwrite` reads and writes, so it
records both `read_retries` and `write_retries`; `consume_stream` only reads,
so it records `read_retries` alone (`write_retries` stays unset). The retry
counts are accumulated in operation-scoped thread-local counters that
`pump_stream_readwrite` and `consume_stream` explicitly reset at operation
start (right after entering the span, via `io_utils::reset_retry_counters`;
`operation_span` itself does not touch the counters), so the seams stay
parameter-free while the span still reports them.

One event in that loop comes from the pump's own state rather than a seam.
When the writer-close latch first closes — the downstream stage hung up and the
remaining reads only drain the upstream, the `head`-style early exit — the loop
emits a `debug` event carrying `bytes_transferred`, using the same field and
message as the splice path's broken-pipe report so a hang-up looks identical
whichever path handled it. It is observed in the loop rather than in
`pump_machine`, which stays free of I/O and logging so its bounded proofs stay
tractable.

The span is created at `error` level so the `warn`/`error` events keep their
operation context even under a `warn`/`error`-only production filter, where an
`info` span would be disabled; it emits no log line of its own.

Unix Rust tests share pipe creation, duplicated-file wrapping, result helpers,
and descriptor-state checks through `test_support`. Re-use that module for
descriptor-backed test setup; keep production code independent of test helpers.
The splice behavioural tests expose that shared `make_pipe` as an rstest `pipe`
fixture, so scenarios needing several independent pipes inject it once per
`#[from(pipe)]` parameter.

## Rust property testing and verification

Rust-level tests for `cuprum-rust` live with the crate under
`rust/cuprum-rust/src/`. Use them for pure decoder, parsing, state-machine, and
adapter logic where Python integration tests would only cover a few examples.

Property tests use [proptest](https://docs.rs/proptest/latest/proptest/) as a
development dependency. Prefer generated payloads and small helper functions
that expose pure behaviour. The UTF-8 decoder tests generate arbitrary byte
vectors and chunk split points, then compare the decoded output with
`String::from_utf8_lossy` as the oracle.

When a property fails, proptest writes the shrunk case to a seed file under
`rust/cuprum-rust/proptest-regressions/`. Commit that file: it re-runs the
failing case ahead of the generated ones, so the same regression cannot return
unnoticed.

Seeds are only worth keeping when they came from a genuine failure. Running
the Rust suite against deliberately-broken code — to prove a property is not
vacuous — also writes a seed, and that one pins a case that never failed
against correct code. Disable persistence for those runs so the file is never
created:

```bash
PROPTEST_DISABLE_FAILURE_PERSISTENCE=1 make test
```

If a seed file does appear after a mutation run, delete it rather than
committing it. Checking it in would imply a history the code does not have.

Snapshot tests use [insta](https://docs.rs/insta/latest/insta/) as a
development dependency, declared with `default-features = false` so the crate
pulls in none of insta's optional format integrations. Prefer insta where the
valuable assertion is the *exact* output text rather than a property of it —
`consume_snapshot_tests.rs` pins the UTF-8 replacement output of the
`consume_stream_files` read loop this way, so a regression in the loop, the
bounds-checked slicing, or the `final_chunk` handling surfaces as a concrete
text diff rather than a boolean failure.

These Rust-side cases are not the only coverage of these categories, and it is
worth knowing why they carry the load. `TestRustConsumeStream` in
`cuprum/unittests/test_rust_streams.py` already defines Python/Rust boundary
tests over the same four inputs — ASCII, multibyte UTF-8 split across a read
boundary, invalid UTF-8, and an incomplete trailing sequence — and each one
calls `rust_consume_stream` and compares the result against Python's own
replacement decoding, `payload.decode("utf-8", errors="replace")`.

Those cases do now execute in CI — see [Building the extension for
tests](#building-the-extension-for-tests) — but they did not until `#258` was
resolved, because `make build` only runs `uv sync --group dev` and never
compiled `cuprum._rust_backend_native`. Running them also surfaced `#265`,
where an `OSError` crossing the boundary lost its `errno`; that is fixed too,
under [Preserving the operating-system error
code](#preserving-the-operating-system-error-code).

The two layers verify different things and neither replaces the other: the
snapshots and properties cover the `consume_stream_files` read-and-decode loop,
while `TestRustConsumeStream` covers the exported surface a caller actually
touches. Keep both when changing either.

Those snapshots are written inline with `insta::assert_snapshot!(value, @"...")`
rather than as separate `.snap` files, which keeps the expected text beside the
case that produces it and leaves no snapshot files to review or prune. Accept a
deliberate change by editing the inline literal; `cargo insta` is not required
for the inline form.

### Building the extension for tests

Several test modules exercise the compiled PyO3 extension and are gated on it
being importable. `make build` does not build it — that target only syncs
dependencies — so build it explicitly:

```bash
make develop
```

That runs `maturin develop` against `rust/cuprum-rust/Cargo.toml` in the
project virtual environment, preceded by `ensurepip` because maturin resolves
its own script through the interpreter's `sysconfig` scheme, which needs pip
present in the environment. CI's `extension-tests` job runs the same target, so
a local run and a CI run build the extension identically. The Makefile keeps
only a pointer to this section rather than repeating the reasoning.

Every CI job that installs the extension and then runs against it goes through
this target — `extension-tests` and `benchmark-ratchet`, and no others. The
wheel build is not one of them: `.github/workflows/build-wheels.yml` runs
`maturin build` to produce a distributable artefact rather than installing it
into a virtual environment, so it neither uses nor needs this target.

The `benchmark-ratchet` job needs an optimized build, and that is the only
thing it needs differently, so it passes
`make develop MATURIN_DEVELOP_FLAGS=--release` rather than restating the
three-step sequence. Keep it that way: a second copy of the sequence is how
the two drift, and the ratchet then measures a build nobody maintains.
`MATURIN_DEVELOP_FLAGS` is empty by default, because a debug build is what
contributors and the `extension-tests` job want.

Without it these modules skip rather than fail, which is the right default
locally — most changes do not need the native path rebuilt — and the wrong one
in CI, where a job that never built the extension reports a green run
indistinguishable from one that exercised the whole boundary. `make
test-extension` sets `CUPRUM_REQUIRE_RUST_EXTENSION=1` to make that silence
fatal:

```bash
make develop
make test-extension
```

Run that rather than `CUPRUM_REQUIRE_RUST_EXTENSION=1 make test`. Once the
extension is installed the full suite aborts the interpreter — see the `#124`
constraint below — so `make test-extension` runs only the gated modules. Their
list lives in one place, the Makefile's `EXTENSION_TEST_TARGETS`, which the CI
job consumes too, so the two cannot drift apart.

None of that wiring is exercised by ordinary tests — drop the guard variable
and the suite still passes — so two contract modules read it back, split by
what they read rather than by what they assert.

`test_extension_build_contract.py` covers the Makefile: that the recipe sets
the guard variable, and what `EXTENSION_TEST_TARGETS` must contain. It reads
the Makefile with `make --dry-run` rather than by parsing the file, because
the expanded recipe is the command line CI actually runs. It scrubs the
Makefile's `?=` variables from that nested `make`'s environment first. `make`
exports each of its command-line overrides under its own name, and a `?=`
assignment yields to a name already in the environment, so without the scrub
a run of `make test EXTENSION_TEST_TARGETS=…` would have the contract report
on whoever invoked it rather than on the repository — passing or failing
according to the command line rather than the wiring. Stripping `MAKEFLAGS`
alone does not prevent that, because the override travels under its own name
as well.

`test_extension_ci_contract.py` covers `ci.yml`: that the `extension-tests`
job runs `make develop` before `make test-extension`, that `benchmark-ratchet`
builds through the same target with `--release`, and that no job reintroduces
a second copy of the build sequence. It declares the workflow shapes it reads
— jobs, steps, and a step's `run:` — so that a misspelled key is a type error
rather than a `None` that quietly satisfies the assertion above it.

`EXTENSION_TEST_TARGETS` gets two separate checks, because neither implies the
other:

- A scan derives the extension-gated modules from the suite itself — those
  requesting the root `rust_streams` fixture, skipping with the shared "Rust
  extension is not installed" reason, or naming `cuprum._rust_backend_native`
  — and requires each one to be a declared target. This is the check that
  notices a *new* gated module being forgotten, which a hard-coded copy of
  today's list never would. The scan is textual and deliberately narrow, so it
  is a lower bound: a module gating through some other idiom goes unnoticed. A
  companion check fails when a signal stops matching anything, so a renamed
  fixture cannot quietly empty the scan instead of failing it.
- Modules that do not gate at all, but belong in the job anyway, are named
  explicitly alongside the reason. `test_extension_requirement_guard.py` is
  one: running it inside the guarded job is what proves the guard stays silent
  when the extension is present, rather than only that it fires when the
  extension is absent.

So add a newly gated module to `EXTENSION_TEST_TARGETS`. If it is boundary
coverage that never skips, add it to the companion list with its reason
instead.

Running `make test-extension` without having built the extension is safe: the
guard fails the run with a message naming `make develop`, which is the whole
point of it.

The check runs once per session in `conftest.py`, so it covers every gated
module regardless of how each one gates — fixture, module-level guard, or
availability probe — and a new module cannot opt out by skipping differently.
The decision itself lives in `tests/helpers/extension_requirement.py` rather
than in the root `conftest.py`, because a root conftest is shadowed by the
per-package one and so cannot be imported by name from a test module;
`test_extension_requirement_guard.py` covers it, and reaches the hook through
the plugin manager to check that it actually raises.

CI runs these in a dedicated `extension-tests` job rather than folding the
extension into `typecheck-test`. That is a deliberate constraint, not tidiness:
with the extension present, `test_pipeline.py` trips the file-descriptor close
race tracked by issue `#124` (roadmap 8.1.1) and aborts the interpreter part
way through the suite, reproducibly. Until that is fixed, the extension must
not be installed for the general test run. Widening the job is the natural
follow-up once `#124` lands.

Table 1: modules gated on the compiled extension

| Module | Covers |
| --- | --- |
| `test_rust_streams.py` | pump and consume entry points, including the four `TestRustConsumeStream` replacement scenarios that are the end-to-end regression coverage for `#105` |
| `test_rust_streams_boundary_property.py` | randomized payloads across the boundary |
| `test_rust_extension.py` | extension availability and module surface |
| `test_rust_splice.py` | the Linux `splice` fast path |
| `test_rust_errno.py` | `OSError.errno` and subclass selection across the boundary |
| `test_backend.py` | the extension-dependent backend-selection cases |
| `test_extension_requirement_guard.py` | the fail-loud guard itself |
| `tests/behaviour/test_rust_streams_behaviour.py` | the consumer-facing pump and consume scenarios |
| `tests/behaviour/test_rust_extension_behaviour.py` | availability agreeing with the installed native module |
| `tests/behaviour/test_stream_backend_pipeline.py` | pipelines dispatched through the Rust backend |

The behavioural modules are listed for the same reason as the unit ones: their
extension-dependent scenarios skip in the ordinary test jobs, so they were
never boundary coverage there either. Confirm with `pytest -rs` against a
virtual environment that has no extension — four scenarios report `Rust
extension is not installed`.

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

### Docstring structure

Public functions, classes, and methods require comprehensive NumPy-style
docstrings, with examples where appropriate. Prefer single-line docstrings for
private helpers, and use structured NumPy-style sections only to describe
non-obvious behaviour.

When a private helper needs an explanatory paragraph, first inspect whether it
combines query and command responsibilities or unrelated concerns. Split or
extract a focused helper when doing so makes the boundary or invariant local
and simpler. Retain the paragraph when the helper remains cohesive and the
explanation records an unavoidable local constraint.

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

### Ruff `ASYNC` (flake8-async) policy

Cuprum selects the Ruff `ASYNC` (flake8-async) rule family in
`[tool.ruff.lint]` so async-correctness lints run as part of `make lint`. The
two that matter for the subprocess surface are `ASYNC109` (async functions
that take a `timeout` parameter) and `ASYNC240` (blocking `pathlib` calls
inside an async function). The family guards the async subprocess code
against callee-owned deadlines and accidental blocking I/O.

Two suppressions are scoped as narrowly as possible rather than disabling the
family:

- **Public API (`# noqa: ASYNC109`).** `SafeCmd.run` and `Pipeline.run` keep
  their documented `timeout` parameter, which deliberately mirrors
  `subprocess.run(timeout=...)`. `ASYNC109` would instead have the caller own
  the deadline through `asyncio.timeout()`, but the parameter is public,
  documented ergonomics, so each definition carries a per-line
  `# noqa: ASYNC109` with a rationale comment rather than dropping the
  parameter. Internal helpers do not take a `timeout` parameter (see
  [ADR-007](adr-007-subprocess-execution-module-boundaries.md)); only the
  public surface is suppressed.
- **Test scaffolding (`per-file-ignore`).** `ASYNC109` and `ASYNC240` are
  ignored through `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` for
  exactly two modules — `cuprum/unittests/test_observe_stdin_early_close.py`
  and `tests/behaviour/test_execution_runtime.py`. Their async scaffolding
  polls a PID file with asyncio-only helpers, so a `timeout` parameter and
  blocking `pathlib` calls are acceptable there; the async-native path
  libraries (`trio.Path` / `anyio.Path`) that `ASYNC240` recommends are not in
  use. Naming the two paths rather than globbing `**/test_*.py` stops
  unrelated and future async tests inheriting the exemption silently. The
  rationale is recorded next to the ignore in `pyproject.toml`.

When changing either suppression, keep the `pyproject.toml` comments and this
section in step.

## Maturin pin synchronization and native wheel tests

These checks span three test modules, one per concern —
`test_maturin_pins.py`, `test_maturin_toolchain.py`, and
`test_maturin_build.py` — and the helpers behind them are split across three
boundaries.

The **pin-synchronization** checks live in
`cuprum/unittests/test_maturin_pins.py`, with their readers and regexes local
to that module: they read repository files, have a single consumer, and gain
nothing from indirection. Two exceptions sit in
`cuprum/unittests/_maturin_pin_support.py`, each because it genuinely has a
second consumer — the threshold this policy asks for before sharing anything:

- `read_expected_maturin_version` — the pin comparison here, and the wheel
  snapshot's `Generator` assertion in `test_maturin_build.py`.
- `MANYLINUX_CONTAINER_SHA256_RE` — the container-pin assertion here, and the
  generated references in `test_manylinux_container_ref_properties.py`.
  Sharing it through the support module keeps one test module from importing a
  private name out of another.

The **availability detectors** are tested together in
`cuprum/unittests/test_maturin_toolchain.py`: `toolchain_available` and
`maturin_script_locatable` answer adjacent questions about the same build, and
the wheel test is gated on both.

The **wheel build and toolchain detection** stay in `tests/helpers/maturin.py`,
because they wrap `subprocess` and `sysconfig` probing that does not inline
cleanly: build a wheel (`build_native_wheel_artifact`), report toolchain
availability (`toolchain_available`), and report separately whether maturin's
own script lookup can find its binary (`maturin_script_locatable`).

The build runs `python -m maturin` under the current interpreter, so it uses
whichever maturin that environment provides rather than selecting a version
itself. Two separate mechanisms enforce alignment with the declared pin.
`test_installed_maturin_matches_expected_pin` compares the `pyproject.toml` pin
against the installed maturin distribution's version, read from the current
interpreter's package metadata with `importlib.metadata.version("maturin")` —
the same interpreter that runs the build — and gates on that interpreter being
able to *import* `maturin`, not on a CLI being present on `PATH`. A launcher
found on `PATH` can belong to a different environment from the one the build
uses. The snapshot test asserts the built wheel's
`Generator` matches that same pin, so a wheel built by an unexpected maturin
fails the suite.

The **wheel-artefact snapshot** parsers (`wheel_build_snapshot` and its private
helpers) live in the sibling module `tests/helpers/maturin_wheel.py`, keeping
each module below the Pylint module-length limit. `tests/helpers/maturin.py`
re-exports `wheel_build_snapshot`, so import sites use
`from tests.helpers.maturin import wheel_build_snapshot` unchanged.

The re-use policy for all three is to stay that way — do not re-externalize
further helpers until a second concrete consumer exists and the shared
interface can be designed against real requirements rather than anticipated
ones.

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
uv run pytest cuprum/unittests/test_maturin_pins.py \
    -k "manylinux_aarch64_container"
```

**Installed version check** (`test_installed_maturin_matches_expected_pin`)
Skipped automatically when the `maturin` *module* cannot be imported by the
running interpreter. That is the right boundary rather than `PATH`, because the
build runs `python -m maturin`: a `maturin` launcher earlier on `PATH` can
belong to an entirely different environment from the one the build uses. When
the module is importable, asserts that the installed version matches the
pinned development dependency.

**Wheel build snapshot** (`test_maturin_wheel_build_snapshot`) Requires the
Rust toolchain (`cargo` and `rustc`). Builds a native wheel into a temporary
directory, extracts normalized metadata and layout information, and compares
the result against a [syrupy](https://github.com/syrupy-project/syrupy)
snapshot stored at `cuprum/unittests/__snapshots__/test_maturin_build.ambr`.

To update the snapshot after a maturin or PyO3 bump, run:

```bash
uv run pytest cuprum/unittests/test_maturin_build.py \
    --snapshot-update -k test_maturin_wheel_build_snapshot
```

### `maturin_script_locatable()` — native-wheel skip boundary

`maturin_script_locatable()` (in `tests/helpers/maturin.py`) is the shared
probe that decides whether the native-wheel build contract can actually run. It
mirrors `maturin.__main__.get_maturin_path`: maturin resolves its bundled
binary by scanning each `sysconfig` scheme's `scripts` directory for a file
named `maturin`, keyed off the running interpreter's `sys.prefix` — **not**
`sys.path` or `PATH`.

This is deliberately narrower than `toolchain_available()`, and the two answer
different questions:

- `toolchain_available()` — is the `maturin` module importable and are `cargo`
  and `rustc` on `PATH`? It uses `importlib.import_module` rather than
  `importlib.util.find_spec`, because the build runs `python -m maturin`,
  which needs the module to *import*: a module that is merely findable can
  still fail to import.
- `maturin_script_locatable()` — can maturin find its own compiled script the
  way `python -m maturin build` will at runtime?

The two disagree in layered or ephemeral interpreters — most importantly the
`uv run --with mutmut==3.6.0` overlay used by the mutation-testing workflow.
There, the project virtualenv is on `sys.path` (so the module imports and
`toolchain_available()` returns `True`), but `sys.prefix` points at a temporary
environment that never received maturin's script, so `python -m maturin build`
fails with ``Unable to find `maturin` script`` before it can invoke `cargo`.
This previously aborted the whole mutmut baseline. In a normal virtualenv (CI,
`build-wheels.yml`, local `uv run pytest`) `sys.prefix` matches the install
location, the probe returns `True`, and the real build runs — so the skip never
masks a genuine regression.

**Reuse policy.** Any test that shells out to `python -m maturin build` (or
otherwise depends on maturin locating its own binary) — for example a new
wheel-layout, packaging, or reproducibility test — should gate on **both**
`toolchain_available()` and `maturin_script_locatable()`, skipping with a
reason that names `sys.prefix` when the script is unreachable. Tests that only
import the `maturin` Python module, or that inspect pins/metadata without
building, need only the checks they already use and should **not** adopt this
probe. Reuse the existing helper rather than re-deriving the `sysconfig` scan;
extend `maturin_script_locatable()` in place if maturin changes how it locates
its binary.

## Rust stream buffer-size validation

`rust/cuprum-rust/src/lib.rs` validates the `buffer_size` argument to
`rust_pump_stream` / `rust_consume_stream` at the PyO3 boundary through a pure
`checked_buffer_size(i64) -> Result<usize, &'static str>` helper, wrapped by
`validate_buffer_size` (which maps the message to `PyValueError`). The contract
is: reject non-positive values, values that overflow `usize` on the target
platform, and values above `MAX_BUFFER_SIZE` (1 GiB, `1 << 30`) — the cap
guards against absurd allocations while comfortably exceeding any realistic
transfer buffer (the default is 64 KiB). `checked_buffer_size` is kept pure so
its boundaries are property tested directly in
`rust/cuprum-rust/src/buffer_size_tests.rs`; the Python-side error mapping is
exercised in `cuprum/unittests/test_rust_streams_boundary_property.py`. Keep the
`_streams_rs.py` wrapper docstrings, `docs/cuprum-design.md`, and the users'
guide aligned with this contract when the cap changes.

## Development dependency pins

Test tooling occasionally needs a temporary upper bound while an upstream
project catches up with a newer release. These pins live in `pyproject.toml`'s
`[dependency-groups]` `dev` group — dev-only, never in the runtime
`[project.optional-dependencies]` — each with an inline rationale and a
tracking link, and are lifted once the upstream fix lands.

- `pytest<9.1` — pytest 9.1 deprecates the nodeid/baseid path that `pytest-bdd`
  8.1.0 relies on for fixture registration, raising `PytestRemovedIn10Warning`
  under the behavioural suite. The constraint holds the dev environment on
  pytest 9.0.x until `pytest-bdd` migrates to the node-based fixture API. Track
  [pytest-bdd#823](https://github.com/pytest-dev/pytest-bdd/issues/823);
  remove the pin and this note once a released `pytest-bdd` supports pytest 9.1.

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
[trybuild](https://github.com/dtolnay/trybuild) to validate contracts that hold
at compile time and so cannot be observed by a runtime test: PyO3 macro
behaviour, and encapsulation boundaries such as the pump machine's private
`Transition`. Tests live under `rust/cuprum-rust/tests/ui/`:

- `tests/ui/pass/` — Rust files that **must compile** without error.
- `tests/ui/fail/` — Rust files that **must fail** compilation with diagnostics
  matching the corresponding `.stderr` file.

Run compile-time UI tests with:

```bash
cd rust && cargo test compile_time_ui
```

Committed `.stderr` fixtures must be regenerated with Rust 1.85.0, the
minimum-supported Rust compiler version used by CI. Update them after a PyO3 or
compiler upgrade:

```bash
cd rust && TRYBUILD=overwrite cargo +1.85.0 test compile_time_ui
```

Inspect the updated `.stderr` files before committing to confirm that each fail
test still represents a genuine compile-time error.

A fail case that pins an encapsulation boundary must include the real module
under test with `#[path]` rather than restating its shape, because a
hand-written copy would only prove the copy private.

Such a fixture may legitimately need to silence a lint the throwaway crate
trybuild builds cannot configure — that crate inherits no `[lints]` table, so
the workspace's `check-cfg = ["cfg(kani)"]` does not apply and the included
module's `#[cfg(kani)]` gates would otherwise bury the expected diagnostic in
`unexpected_cfgs` noise. Scope that suppression to the item that needs it:

- Do not use a crate-level inner attribute (`#![allow(...)]` or
  `#![expect(...)]`). It suppresses the lint for the probe code as well as the
  included module, so a diagnostic the case ought to report can vanish
  unnoticed. There are no crate-level `allow` or `expect` attributes anywhere in
  `rust/`, and no fixture needs the first one.
- Put an outer `#[expect(<lint>, reason = "...")]` on the `#[path]` module
  declaration instead, next to the `#[path]` attribute itself. Prefer `expect`
  over `allow`: if the production module later drops the gates that provoke the
  lint, an unfulfilled expectation fails the build rather than leaving a stale
  suppression behind.
- Every suppression carries a `reason` string naming the constraint, per the
  repository-wide rule that lint suppressions are tightly scoped and explained.

`tests/ui/fail/pump_transition_unreachable.rs` is the worked example.

Before committing one, verify it is not vacuous: weaken the production item it
guards, confirm the case then fails, and restore. Re-run that check after any
change to the fixture's attributes or structure, not only after a change to the
item under test — narrowing a suppression can alter which diagnostics reach the
`.stderr` fixture.

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

## Subprocess execution module boundaries

The subprocess execution implementation is split by lifecycle concern across
`cuprum/_subprocess_execution.py`, `cuprum/_subprocess_stdin.py`, and
`cuprum/_subprocess_timeout.py`. See [Cuprum design](cuprum-design.md) §8.1.5
and [ADR-007](adr-007-subprocess-execution-module-boundaries.md) for the
accepted rationale and compatibility constraints.

Keep these boundaries intact. New stdin pipe behaviour belongs in
`_subprocess_stdin`; timeout or exit-event policy belongs in
`_subprocess_timeout`; and orchestration that coordinates them belongs in
`_subprocess_execution`.

The subprocess wait path uses caller-owned deadlines: `asyncio.timeout()` was
adopted in place of `asyncio.wait_for()`, so the deadline is applied by the
caller rather than threaded through a `timeout` parameter. The wait logic is
split accordingly: `_wait_for_exit_code` awaits the process and terminates it
on cancellation, and no longer takes a timeout argument, while
`_wait_for_exit_code_within_timeout` applies `execution.timeout` around it. A
non-positive timeout is special-cased to expire immediately and
deterministically, because `asyncio.timeout()` alone would let a fast,
already-exited process race past a zero or negative deadline; this preserves
the behaviour of the previous `asyncio.wait_for()` implementation and is
guarded by regression tests in `cuprum/unittests/test_subprocess_timeout.py`.

Neither wait helper terminates unconditionally, and neither drains.
`_wait_for_exit_code` terminates the process only when the wait is cancelled —
which is also how an `asyncio.timeout` expiry arrives — while a successful wait
returns the exit code as soon as `process.wait()` completes, leaving the
process alone. `_wait_for_exit_code_within_timeout` terminates only on its
non-positive fast path, before any wait begins.

Draining is the caller's job either way: after termination it drains the stream
consumers exactly once via `_drain_stream_consumers` (see the stdin-injection
sequence below), and terminating the process before that drain is what lets it
reach EOF.

### Pipeline timeout and teardown

A pipeline enforces one deadline for the whole run, rather than a separate
deadline per stage. `_collect_pipeline_inputs`, in
`cuprum/_pipeline_internals.py`, awaits the pipeline against that single
deadline and maps its expiry to `TimeoutExpired`.

On expiry, `_collect_pipeline_inputs` calls `_terminate_timed_out_stages`
(in `cuprum/_process_lifecycle.py`) before it gathers stage output. Doing so
first lets the stage pipes reach EOF, so a captured pipeline cannot block on
a producer stage that is still running.

`_terminate_timed_out_stages` delegates to `_terminate_all_shielded`, which
runs the terminations in an owned, `asyncio.shield`ed task and holds off a
caller's cancellation until they finish before re-raising it. Without that
shielding, a cancellation landing in the grace-period wait would skip the
`SIGKILL` escalation and leave a `SIGTERM`-immune stage running.
`_terminate_all_shielded` also backs `_cleanup_spawned_processes` and
`_cleanup_pipeline_on_error`, and the fail-fast route in
`_terminate_pipeline_remaining_stages` uses the shared
`_await_teardown_shielded` helper directly.

The inter-stage pump tasks, created by `_create_pipe_tasks`, are created and
owned by `_collect_pipeline_inputs` rather than by `_wait_for_pipeline`. This
matters because a non-positive deadline gives `asyncio.wait_for` a zero
timeout, and that cancels `_wait_for_pipeline` before its body — and
therefore its `finally` — ever runs, so that `finally` cannot reconcile the
pumps. On the timeout route, `_reconcile_pipe_tasks` cancels and drains the
pump tasks instead.

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
   `TimeoutError` or `asyncio.CancelledError`, `_run_subprocess_with_streams`
   itself cancels and gathers the stdin writer via `_cancel_stdin_writer`, then
   drains the stdout/stderr consumers exactly once via
   `_drain_stream_consumers`.  `_wait_for_exit_code` has already terminated the
   process, so the drain reaches EOF.  Only after that draining does it call
   `_handle_stream_timeout`, which merely raises `_SubprocessTimeoutError`
   carrying the pre-drained stdout/stderr; on the cancellation path
   `CancelledError` is re-raised directly.
6. In the non-streaming path (`_execute_subprocess`), the same writer task is
   created and awaited after `_wait_for_exit_code` completes.  On
   `TimeoutError` or `asyncio.CancelledError` from `_wait_for_exit_code`,
   `_execute_subprocess` itself cancels and drains the writer task via
   `_cancel_stdin_writer` before the timeout is translated or the cancellation
   propagates, so a stdin drain blocked on an unread pipe cannot delay
   completion.

The `tests/helpers/stream_pipes.py` module provides
`drain_blocking_payload_size()`, a shared helper returning a stdin payload size
that reliably wedges the writer's `drain()`.  It probes the real OS pipe
capacity (via `fcntl(F_GETPIPE_SZ)` on Linux) and adds a mebibyte of headroom,
falling back to a conservative default on platforms that cannot probe it so
callers need no platform guard.  It exists solely to make blocked-`drain()`
regression tests deterministic and is shared by `test_safe_cmd_stdin.py` and
`test_observe_stdin_early_close.py`; new blocked-writer tests should reuse it
rather than hardcoding a payload size.

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
