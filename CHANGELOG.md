# Changelog

## [0.2.0-beta2] - 2026-10-17

_Applications upgrading from 0.1 should follow the
[0.2.0 migration guide](docs/v0-2-0-migration-guide.md)._

### Changed

- **Builder keywords named after execution options are rejected (breaking):**
  `sh.make()` builders serialized every keyword into a child `--name=value`
  argument, so `python("-c", script, cwd=repo_dir)` produced `--cwd=<path>`
  instead of changing the child's working directory —
  `git("tag", cwd=repo_dir)` ran in the ambient directory, and a tool that
  ignores unknown flags succeeded in the wrong place with no error. The builder
  now raises `TypeError` for the `ExecutionContext` field names (`env`, `cwd`,
  `cancel_grace`, `native_pump_cleanup_grace`, `timeout`, `stdout_sink`,
  `stderr_sink`, `encoding`, `errors`, `tags`, `env_mode`, `process_group`) and
  for `run_sync`'s parameters (`output`, `timeout`, `context`, `stdin`), with a
  message naming the correct spelling:
  `cwd is an execution option; pass ExecutionContext(cwd=...) to run_sync`.
  Pass the value to `run_sync(context=..., timeout=..., stdin=...)` instead. A
  command line that genuinely takes such a flag still receives it positionally
  (`python("--cwd=<dir>")`), and names merely resembling the reserved ones —
  `working_dir`, `stdin_file` — keep rendering as flags
  ([#513](https://github.com/leynos/cuprum/issues/513)).
- **`bytes` builder arguments are rejected (breaking):** `sh.make()` builders
  and `build_argv` raise `TypeError` for a `bytes` value in any position,
  instead of passing its `repr` — `b'release'` rather than `release` — to the
  child. Decode caller-held filesystem bytes with `os.fsdecode()` first
  ([#512](https://github.com/leynos/cuprum/issues/512)).
- **Builder arguments are a typed contract (breaking):** `sh.make()` builders
  accept exactly the new public `ArgValue` alias,
  `str | int | float | bool | pathlib.Path | os.PathLike[str]`, and
  `SafeCmdBuilder` becomes a protocol that states it, so a static checker
  rejects an unsupported argument at the call site. At run time any other type
  raises `TypeError` naming the type rather than reaching the child as its
  `repr`, and a `__fspath__` returning `bytes` is rejected likewise
  ([#483](https://github.com/leynos/cuprum/issues/483)).
- **`sh.make` resolves the active scoped catalogue (breaking):** When no
  `catalogue` argument is supplied, `sh.make` now resolves the innermost active
  `scoped(catalogue=...)` before falling back to `DEFAULT_CATALOGUE`, which
  applies only outside any catalogue scope. A call that previously inherited
  `DEFAULT_CATALOGUE` inside a catalogue scope which does not contain the
  program now raises `UnknownProgramError` when the builder is created, rather
  than at run time. Code that builds from a catalogue the enclosing scope does
  not allow, or that relies on the default catalogue inside a catalogue scope,
  needs an explicit `catalogue=` argument. A scope carrying only an allowlist
  (`ScopeConfig(allowlist=...)`) leaves the active catalogue unchanged. The
  resolved catalogue is bound to the builder, so it survives the scope's exit;
  see [Two enforcement points](docs/users-guide.md#two-enforcement-points) for
  how this interacts with the allowlist check at run time
  ([#514](https://github.com/leynos/cuprum/issues/514)).
- **New `ExecPhase` value (breaking for fail-closed hooks):** `settled` closes
  each observed execution exactly once and carries a bounded `terminal_outcome`
  (`exit_zero`, `exit_nonzero`, `timeout`, `cancelled`, or `error`), including
  spawn failures and cancellations. Exhaustive hooks must handle the new phase.
  Catalogue lookup and allowlist validation happen before observation and are
  outside this contract. The bundled adapters now account terminal outcomes from
  `settled`: `cuprum_failures_total` counts every outcome except `exit_zero`,
  so timeouts, cancellations, and spawn failures are included, and
  `cuprum_duration_seconds` is observed from `settled`, while `TracingHook`
  closes its spans there ([#441](https://github.com/leynos/cuprum/issues/441)).
- **New `ExecPhase` value (breaking for fail-closed hooks):** `ExecPhase`
  gains `group_teardown`, published when a teardown leaves an outcome Cuprum
  could not resolve: a refused signal, an unsignalable process group, or a
  capped settlement wait. It carries `owns_group`, a bounded `group_outcome`,
  and `settle_s`. Exhaustive hooks must handle the new phase
  ([#438](https://github.com/leynos/cuprum/issues/438)).
- **Windows native I/O accepts only synchronous-compatible handles:** A direct
  native-shim caller that supplies a raw Windows handle is now rejected
  deliberately, and the rejection is counted as
  `RustPumpHandoffOutcome.WINDOWS_RAW_HANDLE_REJECTED`
  (`windows_raw_handle_rejected`) on `cuprum_rust_pump_handoff_total`. Normal
  pipeline hops still fall back to the Python pump; dashboards and exhaustive
  outcome handling need the new value
  ([#428](https://github.com/leynos/cuprum/issues/428)).
- **`Pipeline` moves to `cuprum.sh.pipeline`:** The `Pipeline` class left
  `cuprum/sh/safe_cmd.py`, which now holds only `SafeCmd`. `SafeCmdBuilder` is
  unaffected and stays in `cuprum/sh/builder.py`, where it already lived.
  `cuprum.sh.Pipeline`, `cuprum.Pipeline`, the `|` operator, and type
  resolution all still work as before. The one observable difference is
  `Pipeline.__module__`, which now names the new module rather than
  `cuprum.sh.safe_cmd`. Each of the two modules imports the other at the foot
  of its file, after the class it defines exists, so `typing.get_type_hints`
  still resolves the names in their quoted annotations. The split is what made
  room for `SafeCmd.start()`, which this release adds: with `Pipeline` in its
  own module, `safe_cmd.py` had 257 lines, so the 84 that `start` then
  contributed left it at 341 rather than carrying it past the repository's
  400-line module ceiling. See the
  [ADR-007](docs/adr-007-subprocess-execution-module-boundaries.md) addendum of
  2026-10-12 for the deferred-import direction the two classes now take.
- **The spawn seam moves to `cuprum._subprocess_spawn`:**
  `_SubprocessExecution`, `_spawn_subprocess`, and `_start_owned_process` left
  `cuprum/_subprocess_execution.py`, which had grown past the 400-line module
  ceiling, and `cuprum._subprocess_execution` re-exports the bundle and the
  stream-wiring names so existing importers are unaffected. `_spawn_subprocess`
  is deliberately _not_ re-exported: the name is resolved from its own module's
  globals, so a patch applied to a copy would leave the real spawn running.
  Code that refuses a spawn patches the module whose globals resolve the seam
  for the path it is intercepting: `cuprum._subprocess_spawn` for `run()` and
  `cuprum._line_stream.coordinator` for `lines()`, while `SafeCmd.start()`
  resolves it through `cuprum._process_handle.tasks`. See the
  [ADR-007](docs/adr-007-subprocess-execution-module-boundaries.md) addendum of
  2026-10-11.
- **Line-callback event emission stops rebuilding invariant metadata:** The
  observe-hook path resolved `program`, `argv` (including the full
  program-prefixed tuple), `cwd`, `env`, `pid`, tags, and the execution
  correlation token once per _line_, inside the same per-line `_EventDetails`
  construction. A private `_LineEventEmitter` now binds them once per observed
  stream, after spawn when the `pid` is known, and each line adds only the line
  text and a fresh monotonic timestamp to a fresh frozen `ExecEvent`. The
  observable contract is unchanged: the same payloads and field order, a
  distinct event object and clock read per line, unchanged hook ordering and
  failure propagation, and the same dispatcher. On the wrap-76 callback
  workload the construction share of the consume subtree fell from 34.2928% to
  29.9087% (median of three matched pairs) and median wall time fell 30.96%
  profiled and 26.28% unprofiled. The gain is concentrated in line-callback
  workloads; see the
  [evidence record](docs/tee-hotpath-line-event-emission-5-2-1.md) and the raw
  captures under
  [`docs/profiling/5-2-1-line-event-emission/`](docs/profiling/5-2-1-line-event-emission/README.md).

### Added

- **Opt-in telemetry redaction:** `RedactionPolicy` applies caller-configured
  literal and regular-expression rules to selected text in structured logging,
  tracing, plain-text start records, argv-derived GitHub Actions group titles,
  and observation consumers wrapped with `redacting_hook`. It is inert unless
  configured, does not discover unconfigured secrets, and never changes command
  execution or captured results. Each destination opts in independently;
  overlapping rule matches are merged against the original text before the
  opaque mask is inserted. Redaction failures raise `RedactionError` without
  falling back to raw text. Metrics remain unchanged with bounded labels
  ([#442](https://github.com/leynos/cuprum/issues/442)).
- **Scoped executable bindings:**
  `bind_executable(program, path_or_resolver, *, allow_relative=False)` binds a
  catalogued program to the executable it should run inside the enclosing
  scope, so a pinned absolute path, a virtual-environment interpreter, or a
  controlled test replacement no longer has to become the program's catalogue
  identity. The logical `Program` still governs the allowlist, project
  metadata, and telemetry, while the binding decides only what a _permitted_
  program executes; resolution therefore happens strictly after allowlist
  enforcement, and a refused program never calls its resolver. `ExecutablePath`
  and `ExecutableBinding` are the value types, `resolve_binding` the pure
  resolution helper, `InvalidExecutableBindingError` the rejection, and
  `merge_executable_bindings` the overlay-only composition rule.
  `CuprumContext.resolve_executable()` inspects the effective binding without
  running anything. The path argument accepts any `os.PathLike`, not only
  `pathlib.Path`, so a `PurePosixPath` binds the way `executable_path()`
  already accepted it; a value that is neither path-like nor callable is
  rejected with `TypeError` instead of being stored as a resolver that fails at
  spawn time. A resolver must return a `str`, so a resolver returning `None`
  cannot be mistaken for "no binding" and silently run the catalogued name
  instead. `resolve_binding()` raises `TypeError` for any other result; every
  execution instead reports a wrong return type, or a resolver that raises, as
  `ExecutableResolutionError`, naming the logical program and chaining the
  original exception. `ExecEvent` and `CommandResult` gain `resolved_path`,
  carrying the executed executable or `None` when nothing was bound; the
  logging adapter projects it as `cuprum_resolved_path` and the tracing adapter
  as `cuprum.resolved_path`, and it is deliberately absent from the metrics
  labels, which stay low-cardinality. `ExecEvent.cwd` reports the directory the
  caller configured, normalized as a `Path`; only a relative _binding_ is
  anchored at the execution's directory, and that anchoring is not reflected
  back onto the event. Path validation is advisory: construction rejects an
  empty path, a relative path unless `allow_relative=True`, and NUL or `..`
  segments, but it cannot close the window between the check and the `exec`, so
  filesystem ownership, permissions, and read-only deployment remain the
  operator's responsibility
  ([#440](https://github.com/leynos/cuprum/issues/440)).
- **Opt-in process-group ownership:**
  `ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)` opts in; the
  field accepts a `ProcessGroupPolicy`. `INHERIT` remains the default and tears
  down only the direct child, exactly as before. `OWN_GROUP` spawns the child
  as its own session and process-group leader on POSIX (`PGID == PID`) and
  tears the whole group down through `os.killpg`, so a descendant the direct
  child left behind — including one that ignores `SIGTERM` and keeps an
  inherited pipe open — is reclaimed and the run settles within the documented
  bounds instead of waiting out the drain's grace window. For an owning run,
  settlement is the group rather than the direct child, so a descendant is
  still reached after the child has exited and even after it has been reaped;
  `INHERIT` keeps the direct-child-only behaviour, where a reaped child settles
  the run and no group is signalled. Only the group the spawn created is
  signalled, so unrelated processes and the caller's own group are untouched.
  The guarantee is bounded and stated as such: a descendant that leaves the
  group with `setsid()` or `setpgid()` is out of reach, as are ancestors and
  siblings. `OWN_GROUP` raises `ValueError` on Windows, which has no POSIX
  process groups and would need a Job Object whose assignment cannot be made
  atomic with process creation, so containment could begin only after a child
  might already have spawned a descendant. A teardown signal the kernel refuses
  with an `OSError` wider than the tolerated race — a group this process may
  not signal, or a name already handed on — is reported as
  `cuprum.teardown_signal_failed`, naming the signal, so a failed teardown is
  not read as an orderly one; a target that had already gone stays silent, as
  that race is expected. The refusals, an unsignalable group, and a capped
  post-`SIGKILL` settlement are each published as a `group_teardown` execution
  event besides the record, carrying a bounded outcome category. The metrics
  adapter projects that event to `cuprum_group_teardowns_total{outcome}` and,
  on the settlement timeout alone, a `cuprum_group_settle_seconds` histogram,
  so a teardown that left a process alive is a countable series. See
  [ADR-021](docs/adr-021-process-group-ownership.md)
  ([#438](https://github.com/leynos/cuprum/issues/438)).
- **`EnvMode`:** Select inherited, additive-overlay, or replacement child
  environments without mutating process-global state
  ([#434](https://github.com/leynos/cuprum/issues/434)).
- **`UNSET`:** Explicitly remove an inherited variable while composing a child
  environment ([#434](https://github.com/leynos/cuprum/issues/434)).
- **`env_mode` fields:** `ScopeConfig`, `CuprumContext`, and
  `ExecutionContext` now carry the selected environment policy
  ([#434](https://github.com/leynos/cuprum/issues/434)). The effective policy
  also reaches observers: `ExecEvent` carries an `env_mode` field on every
  phase, which the logging and tracing adapters project as an extra and a span
  attribute. The metrics adapter labels `cuprum_executions_total` and
  `cuprum_failures_total` with it, so a non-zero exit under a replacement
  policy is distinguishable from an ordinary overlay one. A spawn failure — for
  example, a replacement policy whose missing `PATH` leaves a bare program name
  unresolvable — emits no `start` or `exit` event, but is still counted through
  its `settled` event. The per-line stream counters deliberately omit the
  label, because a line's environment says nothing that its execution's mode
  does not already carry.
- **`SafeCmd.start()`:** Start a command and hold it. The child is spawned and
  its PID announced before the call returns, so the caller decides what happens
  next, and an owned `RunningCommand` is returned rather than a
  `CommandResult`. A spawn that cannot start belongs to `start()` itself: it
  raises the failure the spawn produced — `FileNotFoundError` for an absent
  executable, for instance — and returns no handle, so there is no orphaned
  child to own and nothing for the caller to clean up. Await its `wait()` for
  the outcome — with an optional deadline, whose expiry stops the child exactly
  as a timed-out `run()` does and raises `TimeoutExpired` carrying the output
  captured before it, while the outcome of the child it stopped is recorded for
  every later `wait()`, `terminate()`, `kill()`, or teardown on the same handle
  — or read the settled outcome from `result`. Deliver a POSIX signal with
  `signal()` without settling the run; stop the child gracefully with
  `terminate()` (`SIGTERM`, the cancel grace, then `SIGKILL` if it is still
  there); or compel it with `kill()`, which sends `SIGKILL` without asking so
  the recorded outcome reports the forced signal rather than whichever one
  happened to end the child. Signals are never the caller's race to lose: a
  child that has already exited raises `SignalAfterExitError`, and a platform
  with no route to a caller-named signal raises `UnsupportedSignalError`. The
  refusal holds for every collection the handle's own reap performs: where the
  platform can be asked, the delivery is checked against the kernel as well as
  the published exit code; where it cannot be asked, the check collects the
  exited child instead, so the refusal still names how the child ended rather
  than a placeholder. On every platform the check is taken together with the
  kill under the reaper's own lock, so the handle's own reap cannot free a PID
  between them — whether the reap waits for the child and collects once, or
  polls to collect it because the platform cannot say when it is waitable, and
  whether the collection happens in the reap or in the delivery's own guard. A
  collection the handle does not own is a separate matter: it belongs to code
  the library cannot serialize against, and a refusal cannot always name the
  code for one. Where nothing was published, it reports `-1` rather than
  inventing an exit status. Using the handle as an `async with` block settles
  the run on every exit — a child still running is torn down the same way a
  cancelled `run()` does, `SIGTERM`, the cancel grace wait, then `SIGKILL` —
  and never suppresses the block's own error. Every control operation is
  idempotent: a repeated or concurrent wait reports the one settlement rather
  than building a second result, and no child is left unreaped. Capture is
  forced on, so a stopped child still reports the output it produced
  ([#437](https://github.com/leynos/cuprum/issues/437)).
- **`RunningCommand`:** The owned handle `SafeCmd.start()` returns, exposing
  `pid`, `exec_id`, the settled `result`, and the `wait()`, `signal()`,
  `terminate()`, and `kill()` control operations
  ([#437](https://github.com/leynos/cuprum/issues/437)).
- **`SignalAfterExitError`:** Raised when a signal is requested for a child
  that has already exited. A `ProcessLookupError` subclass carrying the child's
  exit code and the PID it belonged to
  ([#437](https://github.com/leynos/cuprum/issues/437)).
- **`UnsupportedSignalError`:** Raised when the platform cannot deliver a
  caller-named signal, carrying the signal that was asked for and the platform
  that declined it ([#437](https://github.com/leynos/cuprum/issues/437)).
- **Control observation:** What a control operation _did_ to its child is now
  reportable, which the exit code alone cannot express — a `-15` is the same
  whether the child took the first `SIGTERM` or needed `SIGKILL` after its
  grace window ran out, and a stop refused because the child had already gone
  leaves no trace at all. `observe_control` registers a `ControlHook` on a
  context-local channel and returns a registration handle; every completed
  `signal()`, `terminate()`, and `kill()` then reports one `ControlEvent`
  carrying the closed `ControlOperation` and `ControlOutcome`, the measured
  `duration_s`, and the run's `exec_id` where the handle safely provides one,
  the `signal_number` where one was asked for, and the child's `pid` where the
  handle had one. Refusal is a first-class outcome rather than an inference
  from a missing delivery: `REFUSED_UNSUPPORTED` is a platform limitation no
  retry can fix, `REFUSED_EXITED` is an ordinary racing outcome, and each is
  paired with the public error the caller receives. Emission is fail-open, so
  an observer that raises cannot replace the refusal the caller is owed or
  interrupt the child cleanup the operation is performing; a raising hook, and
  one that returns a value rather than `None`, are reported at `WARNING` and
  the remaining hooks continue. A caller who registers nothing pays no clock
  read and allocates nothing. `record_control_events(hook)` joins the channel
  to tracing by recording each event on the execution span the run's
  `TracingHook` has already opened, keyed by `exec_id`, so an operation can
  complete after the run settles without needing a span of its own; and
  `ControlMetricsHook` projects bounded counters and a duration histogram whose
  only labels are the closed `operation` and `outcome`, re-validated against
  the enums at emission. PID reaches the event and the span event but never a
  label, because its domain is unbounded and a recycled PID would fold two
  unrelated children into one series
  ([#437](https://github.com/leynos/cuprum/issues/437)).
- **Byte-exact result mode:** `SafeCmd.run_bytes()` / `run_bytes_sync()` and
  `Pipeline.run_bytes()` / `run_bytes_sync()` return a subprocess's output as
  the child's own bytes, through `BytesCommandResult` and
  `BytesPipelineResult`. The new classes declare the same fields as
  `CommandResult` and `PipelineResult` — measurements included — except that
  `stdout` and `stderr` are `bytes | None`, so a binary payload survives the
  round trip instead of being decoded with a lossy error handler. Capture is
  the only thing the mode changes: text stays the default, `run()` never
  returns bytes and `run_bytes()` never returns text, echo sinks and idle
  heartbeats behave as they do in text mode, and a timeout still carries
  partial output in the same field. A caller-supplied
  `RunOutputOptions.on_line` is refused with `ValueError` before the child
  spawns, because a decoded-text callback beside a byte-exact capture would be
  a second, contradictory contract for one stream; structured observation
  registered with `sh.observe()` is unaffected and still receives decoded
  lines, since capture and line emission travel on separate channels. That
  holds even under `ExecutionContext(errors="strict")`: a byte-exact run's
  observation and echo render a _view_ of the child's bytes and replace
  undecodable input, so an ambient hook cannot raise from the read loop and
  cost the byte-exact run the bytes it had already captured.

  The replacement view is scoped to byte mode, so text mode is unchanged. A
  strict text run still raises from the line reader and from echo, exactly as
  it did before the byte-exact mode existed; its views decode under
  `ExecutionContext.errors` in step with the capture. Only a byte-exact run
  gets the non-aborting view, and only where the capture is the child's own
  bytes.

  An external cancellation re-raises `asyncio.CancelledError` rather than being
  reported as a timeout.

  The mode an execution used is observable. `ExecEvent` carries a
  `capture_mode` field on every phase — `CaptureMode.TEXT` or
  `CaptureMode.BYTES` — for the reason `env_mode` is carried: the mode is
  resolved before the child spawns and describes the whole execution rather
  than one measurement, and without it a byte-exact run and a text run of the
  same command are identical in every counter and span attribute, leaving a
  timeout or teardown failure unattributable to the mode that hit it. It is a
  trusted typed field rather than an observation tag, since a caller may shadow
  any tag key. The logging adapter projects it as `cuprum_capture_mode`, the
  tracing adapter as `cuprum.capture_mode`, and the metrics adapter as a
  `capture_mode` label on `cuprum_executions_total` and
  `cuprum_failures_total`. Only the mode travels; no captured payload, argument
  vector, or other unbounded value joins it. Two mode-boundary diagnostics are
  recorded without rendering their inputs: a refused `on_line` logs the mode it
  turned away, and a mode/payload invariant mismatch logs the mode, stream, and
  the two types ([#444](https://github.com/leynos/cuprum/issues/444)).
- **Opt-in recovery from a closed echo destination:** A presentation sink whose
  destination has closed reports `BrokenPipeError`, which aborted the run and
  took the otherwise-captured result with it. Passing
  `RunOutputOptions(broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT)` now
  disables echoing for the affected stream only, so capture, line observation,
  and child reaping continue and `run_sync` still returns a `CommandResult`
  carrying one `RelayFallback` whose `error_category` is `broken_pipe`. The
  transition is reported once through `CommandResult.relay_fallbacks` /
  `PipelineResult.stages[i].relay_fallbacks`, the echo observation channel, and
  a structured `cuprum.stream` `WARNING`; `EchoMetricsHook` counts it on its own
  `cuprum_echo_broken_pipe_total` series, so a sink that cannot encode stays
  distinguishable from a reader that keeps disconnecting. Only
  `BrokenPipeError` is affected — any other sink `OSError` still propagates —
  and `BrokenPipePolicy.STRICT` remains the default, so every caller that does
  not opt in keeps the existing behaviour byte-for-byte
  ([#435](https://github.com/leynos/cuprum/issues/435)).
- **PEP 561 typing marker:** The package now ships `cuprum/py.typed`, so a type
  checker treats `cuprum` as a typed package and reads its annotations instead
  of falling back to `Any` for every import. The marker is empty, which under
  PEP 561 declares the package fully typed; the marker travels in the pure
  Python wheel, the native wheels, and the published source distribution
  ([#501](https://github.com/leynos/cuprum/pull/501)).
- **Source distribution and GitHub Release:** Each release now publishes a
  source distribution beside the wheels, attests build provenance for every
  file, and publishes a GitHub Release carrying the same files as PyPI.
  Building from the source distribution yields the pure Python package
  ([#488](https://github.com/leynos/cuprum/pull/488)).
- **Aggregate stream telemetry in profiling reports:** `worker-result.json`
  now reports bounded completed-operation totals, and `read-size-sweep.json`
  adds their cross-sample summary. The reports consume the opt-in production
  stream observer and add no per-read or per-chunk production telemetry
  ([#367](https://github.com/leynos/cuprum/issues/367)).
- **Documentation for 0.2.0:** The users' guide opens with a reading map,
  adds tested examples for capture and echo, the idle heartbeat, and migrating
  from `subprocess`, and documents the control, echo, and line-stream
  observation channels. The
  [0.2.0 migration guide](docs/v0-2-0-migration-guide.md) now leads with an
  upgrade checklist covering every breaking change since 0.1
  ([#634](https://github.com/leynos/cuprum/pull/634)).
- **Streaming stdin:** `StdinStream(chunks=...)` feeds an async producer's
  chunks to the child one at a time, writing and draining each before pulling
  the next, so input larger than the parent would buffer is delivered without
  holding the whole payload. A `str` chunk is encoded with the context's
  `encoding` and `errors`, a `bytes` chunk verbatim. A producer failure ends
  the run as `StdinSourceError` with the producer's exception chained, and an
  early child-side close is recorded as a `stdin_error` observation rather than
  failing the run ([#445](https://github.com/leynos/cuprum/issues/445)).
- **Explicit standard-stream redirection:** `RunOutputOptions.stdout` and
  `.stderr` accept a `StdioTarget` — a library pipe, the inherited parent
  stream, a file **cuprum owns** (opened before the spawn, closed in a
  `finally` immediately after), or a **borrowed** descriptor or file object
  cuprum never closes and flushes before the spawn. Contradictory combinations
  are rejected at construction: capture or echo with a redirected stream, one
  path for both streams, and a `stdin` that names a file or descriptor, since
  only `pipe()` and `inherit()` are meaningful for an input stream
  ([#445](https://github.com/leynos/cuprum/issues/445)).

<!-- markdownlint-disable-next-line MD024 -->
### Fixed

- **Native-wheel CI suppresses Maturin's missing `build-backend` warning:**
  `uv_build` remains the PEP 517 backend for the pure-Python wheel, while the
  Linux, macOS, and Windows CI lanes invoke Maturin directly for native wheels.
  Those invocations now suppress only the intentional backend mismatch warning,
  leaving other Maturin diagnostics visible
  ([#430](https://github.com/leynos/cuprum/issues/430)).

## [0.2.0-beta1] - 2026-09-24

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

<!-- markdownlint-disable-next-line MD024 -->
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

<!-- markdownlint-disable-next-line MD024 -->
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

[0.2.0-beta2]: https://github.com/leynos/cuprum/compare/v0.2.0-beta1...v0.2.0-beta2
[0.2.0-beta1]: https://github.com/leynos/cuprum/releases/tag/v0.2.0-beta1
