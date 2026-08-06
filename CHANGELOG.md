# Changelog

## [Unreleased]

### Breaking changes

- **`ExecHook` import path (breaking):** Import `ExecHook` from top-level
  `cuprum` or its definition site, `cuprum.events`. The former
  `cuprum.context.ExecHook` re-export has been removed; only the import path
  changes, not the hook signature or registration behaviour.

### Added

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

## [0.2.0] - 2026-06-21

### Changed

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

[0.2.0]: https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6
