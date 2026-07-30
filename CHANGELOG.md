# Changelog

## [Unreleased]

### Breaking changes

- **`ExecHook` import path (breaking):** Import `ExecHook` from top-level
  `cuprum` or its definition site, `cuprum.events`. The former
  `cuprum.context.ExecHook` re-export has been removed; only the import path
  changes, not the hook signature or registration behaviour.

### Changed

- **Source spelling enforcement:** Check tracked Python and Rust source as well
  as Markdown for en-GB-oxendict spelling, including code identifiers, so
  contributor changes can now fail the spelling gate on source-code drift
  ([#249](https://github.com/leynos/cuprum/issues/249)).

## [0.2.0] - 2026-06-21

<!-- markdownlint-disable-next-line MD024 -->
### Changed

- **Environment overlays (breaking):** Document that scoped `env(...)` overlays
  resolve against the live `os.environ` at subprocess spawn time, so callers
  that depended on an import-time or scope-entry snapshot must pass explicit
  values through the overlay or `ExecutionContext.env` instead
  ([#175](https://github.com/leynos/cuprum/pull/175), [d2e2b92](https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6)).

[0.2.0]: https://github.com/leynos/cuprum/commit/d2e2b921bde69b8162ba0ca37ed68d36c5d6c8a6
