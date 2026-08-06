# Changelog

## [Unreleased]

### Added

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
  fell back to the Python pump, labelled with the seam that refused.
- **`cuprum_rust_pump_failed_after_cancel_total`:** Incremented once,
  unlabelled, per Rust-pump worker failure recovered after its hop was
  cancelled.

### Breaking changes

- **`ExecHook` import path (breaking):** Import `ExecHook` from top-level
  `cuprum` or its definition site, `cuprum.events`. The former
  `cuprum.context.ExecHook` re-export has been removed; only the import path
  changes, not the hook signature or registration behaviour.

## [0.2.0] - 2026-06-21

### Changed

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

[0.2.0]: https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6
