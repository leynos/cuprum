# Changelog

## [Unreleased]

### Added

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
### Breaking changes

- **New `ExecPhase` value (breaking for fail-closed hooks):** `ExecPhase` gains
  `pipeline_fail_fast`. Observe hooks that match exhaustively on phase and
  reject unknown values will raise on it until updated, and Cuprum re-raises
  observe-hook failures rather than swallowing them. Cuprum's own adapters are
  updated in the same change; third-party hooks written the same fail-closed
  way need an explicit arm.
- **`ExecHook` import path (breaking):** Import `ExecHook` from top-level
  `cuprum` or its definition site, `cuprum.events`. The former
  `cuprum.context.ExecHook` re-export has been removed; only the import path
  changes, not the hook signature or registration behaviour.

- **Timeout and teardown telemetry:** Emit `timeout` and `teardown_error`
  `ExecEvent` phases. `timeout` carries `operation="wait"`, `error_type`,
  `timeout_s` (the configured timeout), and `timeout_mode`, which
  distinguishes an elapsed deadline from an immediate non-positive expiry;
  `teardown_error` instead carries `operation="drain"` and `error_type` (the
  comma-joined failure classes), with both timeout fields unset. Both
  phases are accompanied by a structured `cuprum.timeout` log channel, the
  `cuprum_timeouts_total` and `cuprum_teardown_errors_total` metrics
  counters, and ancillary tracing span events. Adoption is additive:
  existing hooks, the `TimeoutExpired` exception and its payload, and the
  `start` / `exit` events are unchanged, so no caller has to do anything,
  and telemetry failures cannot mask `TimeoutExpired` or `CancelledError`
  ([#271](https://github.com/leynos/cuprum/pull/271)).
- A public `TimeoutMode` type alias is exported from `cuprum.events`, naming
  the two stable `timeout_mode` values (`"elapsed_deadline"` and
  `"non_positive_immediate"`), and `ExecEvent.timeout_mode` is now annotated
  with it instead of a bare `str`
  ([#271](https://github.com/leynos/cuprum/pull/271)).

### Changed

- **Source spelling enforcement:** Check tracked Python and Rust source as well
  as Markdown for en-GB-oxendict spelling, including code identifiers, so
  contributor changes can now fail the spelling gate on source-code drift
  ([#259](https://github.com/leynos/cuprum/pull/259)).

### Fixed

- **Repeated cancellation during teardown:** Repeated cancellation arriving
  during timeout or fail-fast teardown no longer strands a `SIGTERM`-immune
  child process; the shielded teardown wait is now retried until it
  completes, so the `SIGKILL` escalation and reap always run
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

## [0.2.0] - 2026-06-21

<!-- markdownlint-disable-next-line MD024 -->
### Changed

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

[0.2.0]: https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6
