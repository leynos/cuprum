# Changelog

## [Unreleased]

### Added

- **Pipeline fail-fast telemetry:** A pipeline now emits one
  `pipeline_fail_fast` `ExecEvent` when a non-final stage is the first to fail,
  published before every other still-running stage — upstream producers and
  downstream consumers alike — is terminated, and carrying that stage's
  existing `exec_id` alongside `stage_index`, `stage_count`, `exit_code`, and
  `duration_s`. `MetricsHook` counts it as
  `cuprum_pipeline_fail_fast_total`, labelled only by `program` and `project`;
  `TracingHook` records it as a `cuprum.pipeline_fail_fast` span event on the
  failing stage's open span; the structured logging adapter renders it at
  `LogLevels.fail_fast_level` (WARNING by default).

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

## [0.2.0] - 2026-06-21

### Changed

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

[0.2.0]: https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6
