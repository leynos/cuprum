# Changelog

## [0.2.0-beta1]

<!-- markdownlint-disable-next-line MD024 -->
### Fixed

- **Capture preserved when echo sinks reject unicode:** A text-only echo sink
  whose encoding cannot represent the subprocess output (for example a Windows
  CP1252 console echoing UTF-8 `ś`/`ń`) no longer aborts stream draining with
  an escaping `UnicodeEncodeError`. Echoing is disabled for only the affected
  stream while capture completes, a `cuprum.stream` `WARNING` records the first
  failure, sinks exposing a binary `buffer` keep receiving the original bytes,
  and other I/O errors still propagate [^1]. Registering `EchoMetricsHook` via
  `cuprum.echo_observation.observe_echo` additionally counts one
  `cuprum_echo_encoding_failures_total` increment per affected stream, labelled
  only by the bounded `stream` (`stdout` or `stderr`) and `error_category`
  (`unicode_encode`) values; the hook is opt-in and no payload or sink metadata
  becomes a metric label.
- **`IOOptions(echo=True)` echoes again:** The deprecated compatibility alias
  overrode the new per-stream resolver with a warning-only initializer, so
  `IOOptions(echo=True)` left both streams silent. It now resolves the
  inherited per-stream fields before emitting the deprecation warning.
- **Native pump no longer wedges a hop it could not duplicate:** Extracting a
  transport's descriptor and duplicating it for the Rust worker are separate
  steps, so a short-lived process whose descriptor asyncio closed in between
  used to fail the duplication. That failure was re-raised, which left the
  writer transport open — the downstream stage never saw EOF and the pipeline
  hung until its deadline. The failure now declines the fast path instead and
  the hop completes on the Python pump, reported like any other decline with a
  `duplicate_fds_unavailable` reason and a `duplicate_writer_failed` hand-off
  outcome. Two failures still report to the caller: an executor rejection, and
  a failure to duplicate descriptors Cuprum already owns, which means
  descriptor exhaustion rather than a race. Those now close the writer
  transport before the error propagates, so a downstream stage exits and the
  failure reaches the caller instead of the pipeline waiting out its deadline.
  Intermittent pipeline hangs on `auto` or `rust` backends are what this
  addresses.
- **Native pump workers are pooled instead of one thread per hop:** Repeated
  native hand-offs used to start a thread per submitted pump and never reclaim
  it, so a long-lived process with many pipelines grew a thread for every hop
  ever taken. Submitted pumps now run on a small pool that keeps up to four
  idle workers for reuse and lets the rest exit once their pump settles.
  Concurrency stays unbounded by design: a native pump cannot finish until a
  later hop in the same pipeline drains its pipe, so queueing a submission or
  waiting for a free worker would deadlock the very pipelines the pool exists
  to serve. Only idle retention is bounded, and `submit` never blocks.
- **Deferred native-pump cleanup releases its paused reader:** When a
  cancellation's cleanup grace expires before the Rust worker settles, the
  caller now closes the paused reader transport at expiry, while its event loop
  can still run the close, rather than leaving it for the completion callback
  that outlives that loop. A hop that was only paused kept its descriptor and
  its subprocess transport alive past `loop.close()`, which surfaced later as
  an unclosed-transport report and a `RuntimeError: Event loop is closed`
  raised from the transport's own finalizer. The deferred callback still closes
  its borrowed worker reader, restores callback-owned state, and emits
  `cleanup_deferred`; resuming the already-closed transport is a no-op. A
  release that fails is recorded at `DEBUG` as `rust_pump_teardown_failed` with
  `cuprum_site="reader_close"`.
- **Shutdown signals from pump hooks are no longer lost on hand-off:** A
  `KeyboardInterrupt`, `SystemExit`, or `asyncio.CancelledError` raised while a
  pump hook observed a `handoff` event used to be logged as
  `pump_handoff_observer_failed` and discarded, contrary to the pump channel's
  contract that shutdown signals always propagate. Hand-off events now follow
  the same policy as every other pump event: ordinary hook exceptions are still
  reported and absorbed, while shutdown signals reach the caller.
- **Native wheels cover every supported Python:** Releases built native wheels
  for CPython 3.13 only, so installations on 3.12 and 3.14 silently fell back
  to the pure Python wheel and lost Rust acceleration. The extension now
  targets the CPython 3.12 stable ABI, so each platform's single `cp312-abi3`
  wheel loads on 3.12 and every later version. Free-threaded builds still use
  the pure Python wheel. See ADR-016.

### Added

- **`ProgramCatalogue.from_project()`:** Build a single-project catalogue from
  an existing `ProjectSettings` without repeating the
  `ProgramCatalogue(projects=(...))` wrapper
  ([#374](https://github.com/leynos/cuprum/issues/374), part of
  [#361](https://github.com/leynos/cuprum/issues/361)).
- **`ProgramCatalogue.from_programs()`:** Build the single-project catalogue
  that a standalone script needs from its programs alone — for example
  `ProgramCatalogue.from_programs("git", "cargo")` — instead of spelling out
  `ProjectSettings` and the `ProgramCatalogue(projects=(...))` wrapper.
  Programs may be `Program` values or strings, the default project name joins
  their base names with `-`, and `name=` overrides it. An empty call raises
  `ValueError`, while a repeated program still raises `DuplicateProgramError`.
  `ProjectSettings.documentation_locations` and `noise_rules` now default to
  empty tuples, so a project that needs neither can omit them
  ([#396](https://github.com/leynos/cuprum/issues/396)).
- **Command execution measurements:** `CommandResult` now includes the
  wall-clock `started_at` timestamp and monotonic `duration`, plus child user,
  system CPU-time, and maximum RSS fields where the platform can attribute
  usage to the reaped child. Linux and macOS direct commands use child-specific
  `wait4` results; Linux RSS is normalized from KiB to bytes and macOS RSS is
  already in bytes. Platforms without that interface retain aggregate CPU-only
  accounting and leave `max_rss_bytes` as `None`; Windows and pipeline stages
  leave all three resource fields as `None`. Existing six-argument positional
  construction remains valid, with timing fields defaulting to `0.0`. The
  terminal `exit` event also carries these figures, plus a
  `resource_usage_mode` naming their source; the logging and tracing adapters
  project them as extras and span attributes. The metrics adapter adds
  `cuprum_resource_usage_measurements_total` and the three
  `cuprum_child_max_rss_bytes`, `cuprum_child_user_cpu_seconds`, and
  `cuprum_child_system_cpu_seconds` histograms, each carrying a
  `resource_usage_mode` label.
- **Idle heartbeat for quiet children:** `RunOutputOptions` accepts
  `idle_after` and `on_idle`, so a run that produces no output for a given
  number of seconds says so instead of leaving a blank CI log to be
  interpreted. With `idle_after` set and no callback, Cuprum writes one bounded
  keepalive line — `[cuprum] still running cargo (idle 30s, total 4m10s)`, at
  most 512 bytes including its newline, ASCII-safe and control-safe — to the
  parent's stderr, reporting the total elapsed time and the time since the last
  observed output and repeating for each further interval of silence; any
  output on a monitored stream resets the interval. `on_idle` receives the same
  two durations as a synchronous `(elapsed_total, elapsed_idle)` callback and
  replaces the built-in line rather than joining it. A callback that raises an
  ordinary exception, or a diagnostic destination that refuses the line,
  disables the channel for the remainder of that run with one sanitized
  `cuprum.idle` warning; the child's exit status, capture, and echo are
  unchanged, and `KeyboardInterrupt` and `SystemExit` are not absorbed. A
  callback that returns a value instead of `None` — including a synchronous
  wrapper around an asynchronous one, which returns a coroutine — is reported
  once and then silences the channel for the remainder of the run, on the same
  terms as a raising callback rather than repeating the report every interval.
  The keepalive starts a fresh line whenever the echo it would otherwise join
  ended mid-line, including when a caller points `stdout_sink` and
  `stderr_sink` at one sink and the child's newline-less stdout shares the
  diagnostic's destination. A pipeline reports one aggregate clock over its
  outward-facing output — the final stage's stdout and every stage's stderr,
  never inter-stage transfers — labelled `pipeline output idle`. The feature is
  off by default, adds no timer, task, or pipe to a run that does not ask for
  it, never terminates a process, and never extends a timeout. A run may watch
  its streams without retaining them: `capture=False, echo=False, idle_after=…`
  drains them while leaving `stdout` and `stderr` as `None`. A value that
  converts to a float but is not one — the string `"30"`, say — is normalized
  at construction rather than accepted and then left for the schedule's
  arithmetic to reject from inside the run. The built-in line is written to the
  configured `stderr_sink` synchronously on the run's event loop, so that sink's
  `write` and `flush` must return promptly. The heartbeat reports absent
  output, not absent progress, so it is never a deadlock diagnosis
  ([#359](https://github.com/leynos/cuprum/issues/359)).

- **Bounded mirrored lines:** `RunOutputOptions.max_echo_line_bytes` defaults to
  64 KiB and limits each echoed logical line, including retained child bytes,
  the encoded truncation marker, and its line ending. Captured output remains
  complete. Set the option to `None` to restore chunk-for-chunk mirroring. The
  preferred marker is `… [truncated N bytes]`; sinks whose encoding cannot
  represent the ellipsis receive the ASCII-compatible `... [truncated N bytes]`
  marker instead. A bound too small for the complete marker or a CRLF ending
  abbreviates the echoed marker or omits the ending to preserve the bound.
  Truncation is also reported through the echo observation channel with its
  stream and dropped-byte count.
- **Per-stream echo control while capturing:** `RunOutputOptions` accepts
  `echo_stdout` and `echo_stderr`, each defaulting to the existing `echo`
  shorthand, so a caller can capture a stream silently while the other still
  mirrors to the parent — for example, capturing the `cargo metadata --locked`
  JSON document without printing it to a CI log. Capture stays a single joint
  boolean and continues for a stream that is not echoed; `ConcurrentConfig`
  forwards the same per-stream fields, which are keyword-only, so positional
  callers keep binding `context` and `fail_fast` as before. The change is
  additive: existing `echo=True` callers resolve both streams to `True` exactly
  as before.
- **`SafeCmd.lines()`:** Iterate a command's decoded output lines as they
  arrive. Each yielded `LineEvent` carries the stream it arrived on (`stdout` or
  `stderr`), monotonic seconds since the command started (`at`), and the
  decoded text without its line terminator. Lines preserve arrival order within
  each stream; capture and echo stay governed by the usual `RunOutputOptions`
  and are not disabled by iterating. After iteration completes, the returned
  `LineStream` exposes the run's `CommandResult` on its `result` attribute.
  Cancelling a task that is iterating `lines()`, or closing the `LineStream` via
  `aclose()` or an `async with` block, tears the subprocess down the same way
  a cancelled `run()` does: `SIGTERM`, the cancel grace wait, then `SIGKILL`.
  Breaking out of the loop on its own does not: `async for` never closes a
  custom iterator, so the stream must be closed. Timeouts behave identically to
  `run()`.
- **`LineEvent`:** The frozen payload a line observer receives, carrying
  `stream`, `at`, and `text`.
- **`LineHook`:** The synchronous callable type a line observer must satisfy.
- **`LineStream`:** The async iterator `SafeCmd.lines()` returns, exposing
  the final `CommandResult` once iteration completes.
- **`LineStreamName`:** The closed literal type of a line event's stream
  name (`"stdout"` or `"stderr"`).
- **`RunOutputOptions.on_line`:** Register a synchronous line callback that
  receives the same `LineEvent` values while `run()` executes. Independent of
  `capture` and `echo`; registering it keeps the stream on the Python pathway.

- **Per-command echo-fallback diagnostics:** Every `CommandResult` — including
  each pipeline stage's result — now carries `relay_fallbacks`, a defaulted
  trailing tuple of frozen `RelayFallback` records (`stream` and
  `error_category`, reusing the existing echo vocabulary) describing the
  handled echo-disablement transitions of that command's own streams: one
  record per affected drain, ordered stdout-then-stderr, empty when nothing was
  handled, and never affecting `exit_code` or `ok` [^2]. Diagnostics are
  collected without a registered observer and with capture disabled, are
  isolated per command, stage, and nested or concurrent run, and on a timeout
  or cancellation that prevents a result the already-emitted echo events stay
  available through `observe_echo` with no new exception payload fields. The
  `cuprum.stream` warning for this transition now carries only stable
  categorical extras (`cuprum_operation`, `cuprum_stream`, `cuprum_transition`,
  `cuprum_error_category`) and no longer attaches the exception object or sink
  encoding: `UnicodeEncodeError.object` retains the rejected input, so neither
  the payload nor the original exception may reach the log, the events, or the
  result records, and metric labels stay bounded. Lading can consume these
  records and the existing echo observation channel
  ([lading#253](https://github.com/leynos/lading/issues/253)); this alone does
  not let Lading delete `stream_relay.py`, whose text-first and broken-pipe
  semantics differ from Cuprum's binary-first policy, so a linked downstream
  migration issue owns caller migration, thread-name utility removal, and the
  helper's final deletion.

- **Pipeline fail-fast telemetry:** A pipeline now emits one
  `pipeline_fail_fast` `ExecEvent`, marking a termination decision, when a
  non-final stage is the first to fail and at least one other stage is still
  running, published before every other still-running stage — upstream
  producers and downstream consumers alike — is terminated, and carrying that
  stage's existing `exec_id` alongside `stage_index`, `stage_count`,
  `exit_code`, and `duration_s`. `stage_index` and `stage_count` are typed
  fields rather than tags, so a caller that sets its own `pipeline_stage_index`
  or `pipeline_stages` tag cannot shadow the stage the pipeline actually acted
  on. `MetricsHook` counts it as `cuprum_pipeline_fail_fast_total`, labelled
  only by `program` and `project`; `TracingHook` records it as a
  `cuprum.pipeline_fail_fast` span event on the failing stage's open span; the
  structured logging adapter renders it at `LogLevels.fail_fast_level` (WARNING
  by default).

- **`observe_pump`:** Register a hook for Rust-pump routing events in the
  current context, returning a detachable `PumpHookRegistration`. The channel
  is separate from `sh.observe`, so an existing observer is untouched.
- **Native Rust-pump cleanup telemetry:** Add `cleanup_started` and
  `cleanup_completed` `PumpEvent` phases, with completion-only
  `PumpEvent.duration_s`, the unlabelled `cuprum_rust_pump_cleanup_total`
  counter and `cuprum_rust_pump_cleanup_duration_seconds` histogram, and the
  cleanup `DEBUG` records. The native worker retains descriptor ownership until
  cleanup completes, so these events and records describe the
  cancellation-cleanup contract.
- **Bounded native-pump cancellation cleanup:**
  `ExecutionContext.native_pump_cleanup_grace` sets the finite, non-negative
  caller wait (0.5 seconds by default). When the grace limit expires,
  cancellation returns `CancelledError` while worker-owned descriptors remain
  quarantined until the completion callback can safely clean them up. The
  `cleanup_grace_expired` and `cleanup_deferred` pump events, together with the
  unlabelled `cuprum_rust_pump_cleanup_grace_expired_total` and
  `cuprum_rust_pump_cleanup_deferred_total` metrics, report the bounded and
  eventual outcomes. The dedicated native-pump executor is independent of
  `asyncio.run()` shutdown, so the caller-facing bound remains effective for
  synchronous execution; a late completion still finalizes descriptors after
  the originating event loop has closed.
- **`PumpEvent`:** The frozen event a pump hook receives, carrying the routing
  `phase` and, for a decline, the `reason` for the decline.
- **`PumpHook`:** The synchronous callable type a pump observer must satisfy.
- **`PumpHookRegistration`:** The handle `observe_pump` returns, usable as a
  context manager or detached explicitly.
- **`RustPumpDeclineReason`:** The closed enum of reasons an inter-stage hop
  falls back from the Rust pump to the Python one, bounding the `reason` label.
- **`UNKNOWN_DECLINE_REASON`:** The fixed label a decline carrying no recognized
  reason degrades to, so a malformed event cannot widen the label domain.
- **`PumpMetricsHook`:** A pump observer that counts routing decisions against
  any `MetricsCollector`.
- **`cuprum_rust_pump_declined_total{reason}`:** Incremented once per hop that
  fell back to the Python pump, labelled with the decline reason.
- **`cuprum_rust_pump_failed_after_cancel_total`:** Incremented once,
  unlabelled, per Rust-pump worker failure recovered after its hop was
  cancelled.

- **GitHub Actions presentation sink:** Add opt-in
  `cuprum.sinks.GitHubActionsSink`, passed via `RunOutputOptions(sink=...)` on
  command and pipeline runs. The sink activates only where workflow commands
  are meaningful: `open_session` reads `GITHUB_ACTIONS` from the parent
  environment per run and frames only on the runner's `true` value, passing
  `force=True` overrides the check for local reproduction or non-standard
  runners, and outside Actions an inactive sink writes nothing. A framed run
  writes `::group::<program args>` before the subprocess starts, shields the
  group with a random stop-commands lease so child output cannot inject
  workflow commands, and writes `::endgroup::` at teardown; a run that ends in
  a non-zero exit, timeout, or error emits exactly one `::error::` annotation
  carrying only the bounded run label and a categorical detail. Capture, exit
  codes, and returned results are unchanged, runs without a sink are
  byte-for-byte identical to before
  ([#360](https://github.com/leynos/cuprum/issues/360)).

- **Timeout and teardown telemetry:** Emit `timeout` and `teardown_error`
  `ExecEvent` phases. `timeout` carries `operation="wait"`, `error_type`,
  `timeout_s` (the configured timeout), and `timeout_mode`, which distinguishes
  an elapsed deadline from an immediate non-positive expiry; `teardown_error`
  instead carries `operation="drain"` and `error_type` (the comma-joined
  failure classes), with both timeout fields unset. Both phases are accompanied
  by a structured `cuprum.timeout` log channel, the `cuprum_timeouts_total` and
  `cuprum_teardown_errors_total` metrics counters, and ancillary tracing span
  events. Adoption is additive: existing hooks, the `TimeoutExpired` exception
  and its payload, and the `start` / `exit` events are unchanged, so no caller
  has to do anything, and telemetry failures cannot mask `TimeoutExpired` or
  `CancelledError` ([#271](https://github.com/leynos/cuprum/pull/271)).
- A public `TimeoutMode` type alias is exported from `cuprum.events`, naming
  the two stable `timeout_mode` values (`"elapsed_deadline"` and
  `"non_positive_immediate"`), and `ExecEvent.timeout_mode` is now annotated
  with it instead of a bare `str`
  ([#271](https://github.com/leynos/cuprum/pull/271)).
- **`RunOutputOptions.group` and `RunOutputOptions.annotate_failure`:** Two
  opt-in flags that frame a run on GitHub Actions with no sink to construct.
  `group=True` frames the run in a collapsible log group with its stop-commands
  lease; `annotate_failure=True` turns a failed run into an `::error::`
  annotation. They are a zero-ceremony spelling of `GitHubActionsSink`, which
  they construct and store as the run's sink, so the execution layer stays
  unaware of workflow commands and `RunOutputOptions(sink=...)` still wins
  where a caller wants the adapter directly. The flags are independent —
  annotation without framing is supported, and a suppressed group takes its
  lease with it — and inherit the adapter's environment gate, so they are inert
  unless `GITHUB_ACTIONS == "true"`. A non-`bool` `RunOutputOptions` flag raises
  `ValueError`; direct adapter toggle values retain `TypeError`. Both flags
  default to `False`, so unflagged runs are unchanged. The flags are a spelling
  of the adapter decision in
  [ADR-013](docs/adr-013-opt-in-github-actions-presentation-sink.md), which
  records why they construct the sink rather than teach the execution layer
  workflow commands ([#375](https://github.com/leynos/cuprum/issues/375)).
- **`GitHubActionsSink` framing toggles:** The adapter's constructor takes
  `emit_group=` and `emit_annotation=` to switch the two halves of its frame
  off independently, matching the flag vocabulary above
  ([#375](https://github.com/leynos/cuprum/issues/375)).

### Breaking changes

- **`ProgramCatalogue.visible_settings` is now a property:** Prefer
  `catalogue.visible_settings` over the former callable spelling. Existing
  `catalogue.visible_settings()` callers remain supported during the next-minor
  migration and receive the same cached, read-only mapping of project names to
  `ProjectSettings`
  ([`079d6698`](https://github.com/leynos/cuprum/commit/079d6698cfdc833928628b0c3278a5cb7d646d9b)).
- **New `ExecPhase` value (breaking for fail-closed hooks):** `ExecPhase` gains
  `pipeline_fail_fast`. Observe hooks that match exhaustively on phase and
  reject unknown values will raise on it until updated, and Cuprum re-raises
  observe-hook failures rather than swallowing them. Cuprum's own adapters are
  updated in the same change; third-party hooks written the same fail-closed
  way need an explicit arm.
- **New `ExecPhase` value (breaking for fail-closed hooks):** `ExecPhase` gains
  `capture_eof_grace_expired` when a capturing timeout drain exhausts its
  bounded EOF grace with one or more readers still pending. Existing hooks that
  reject unknown phases need an explicit arm. The event adds
  `operation="drain"`, `eof_grace_s`, and `pending_readers` to the common
  `ExecEvent` fields, including `pid` and the required `exec_id`; it never
  contains captured stream payloads. `MetricsHook` counts it as
  `cuprum_capture_eof_grace_expired_total`, labelled only by `program` and
  `project`, and `TracingHook` records a correlated
  `cuprum.capture_eof_grace_expired` span event.
- **`ExecHook` import path (breaking):** Import `ExecHook` from top-level
  `cuprum` or its definition site, `cuprum.events`. The former
  `cuprum.context.ExecHook` re-export has been removed; only the import path
  changes, not the hook signature or registration behaviour.

### Changed

- **Benchmark ratchet measures the pipeline, not worker start-up:** The
  `benchmark-ratchet` job compared a within-run Rust-to-Python ratio over
  payloads where the interpreter start, the `cuprum` import, and the
  per-iteration set-up were most of both means, so a runner-to-runner swing in
  that fixed cost could move the ratio past the 30% threshold on its own — the
  false positive reported against
  [PR #158](https://github.com/leynos/cuprum/pull/158) and analysed in
  [#219](https://github.com/leynos/cuprum/issues/219). The job now measures a
  single 64 MiB payload (`--ci-ratchet`, labelled `ratchet`) at five worker
  iterations and twenty hyperfine runs, where streaming dominates what is timed;
  `ci_benchmark_ratchet_profile.py` rejects any scenario outside its 32 MiB to
  128 MiB band. Both the payload change and the iteration change are
  sampling-protocol changes, so `BENCHMARK_PROFILE_VERSION` was bumped to
  `pipeline-worker-release-ratio-v5` and the rolling window refills with
  compatible `main` samples; until the second sample lands the comparison falls
  back to a single-sample bar where the flat threshold decides alone. The
  workflow's `--max-regression`, `--noise-sigmas`, and `--history-window`
  values are now pinned to `benchmarks/ratchet_history.py`'s single
  authoritative defaults by a CI contract test, and the sample-recording and
  baseline-upload steps are contract-tested to depend only on the measurement,
  never on the ratchet's verdict. `--ci-ratchet` also defaults its
  `--worker-iterations` to the count the job measures at, so a local
  reproduction records the same protocol the gate will judge rather than
  silently planning a sample the history cannot be compared against. See
  [§13.9](docs/cuprum-design.md) and the
  [noise measurements](docs/debugging/debugging-plan-2026-09-16-ratchet-overhead-noise.md).

- **Maturin 1.15.0:** The development, wheel-workflow, and composite-action
  maturin pins now agree on 1.15.0, and the PyO3 0.29 line is confirmed
  compatible with the new backend
  ([#332](https://github.com/leynos/cuprum/pull/332)).

- **Pure-Python stream read size:** The private parent-side stream read size is
  now 65536 bytes, selected from a fresh 15-round interleaved sweep. Large tee
  workloads improved by 22.9997% against the same-session 4096-byte control,
  with no measured regression across echo, text-sink, PTY, or line- callback
  scenarios. The value is not a public configuration option; see the
  [sweep record](docs/tee-hotpath-read-size-sweep-2026-08-29.md).
- **Source spelling enforcement:** Check tracked Python and Rust source as well
  as Markdown for en-GB-oxendict spelling, including code identifiers, so
  contributor changes can now fail the spelling gate on source-code drift
  ([#259](https://github.com/leynos/cuprum/pull/259)).

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

<!-- markdownlint-disable-next-line MD024 -->
### Fixed

- **CRLF line emission at a read boundary:** A carriage return at the end of a
  stream read now remains pending until the next decoded text or end of file. A
  following line feed therefore completes the existing line rather than
  producing a spurious empty stdout or stderr line event.

- **Partial capture on timeout:** A capturing `run()` or `run_sync()` that times
  out now reports text for both streams, preserving partial output when readers
  have not yet observed EOF. The bounded drain grace avoids dependence on
  event-loop scheduling while leaving non-capturing teardown prompt
  ([#292](https://github.com/leynos/cuprum/issues/292)).

- **Repeated cancellation during teardown:** Repeated cancellation arriving
  during timeout or fail-fast teardown no longer strands a `SIGTERM`-immune
  child process; the shielded teardown wait is now retried until it completes,
  so the `SIGKILL` escalation and reap always run
  ([#271](https://github.com/leynos/cuprum/pull/271)).
- Cleanup now completes before a cancellation arriving mid-cleanup propagates.
  Stream consumers, the stdin writer, and background observe-hook tasks are
  reconciled through a shielded, cancellation-resistant wait, so a cancelled
  run no longer unwinds while the tasks it owns are still live
  ([#271](https://github.com/leynos/cuprum/pull/271)).
- An observe hook raising on a pipeline stage's terminal `exit` event during a
  timeout no longer replaces the `TimeoutExpired` nor stops the remaining
  stages emitting their `exit` events
  ([#271](https://github.com/leynos/cuprum/pull/271)).
- `TracingHook` no longer accumulates span entries for executions that never
  emit an `exit` event (external cancellation, a stdin-writer failure, or a
  terminal `teardown_error`); the registry of open spans is now bounded and
  evicts the oldest, ending it as failed
  ([#271](https://github.com/leynos/cuprum/pull/271)).

[^1]: <https://github.com/leynos/cuprum/issues/348>
[^2]: <https://github.com/leynos/cuprum/issues/356>
