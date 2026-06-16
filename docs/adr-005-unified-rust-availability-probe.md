# ADR-005: Unify Rust availability probes via cached dispatch resolver

_____________________________________________________________________

## Status

Accepted on 2026-06-16. This ADR records the decision to unify Rust
availability probing behind the cached dispatcher resolver.

## Date

2026-06-16

_____________________________________________________________________

## Context

Cuprum exposes `cuprum.is_rust_available()` and also resolves backend
selection through internal dispatch. Before this change, those call paths could
use different underlying implementations, risking inconsistent answers when module
state changed during process lifetime (especially under tests using
`set_rust_availability_for_testing`).

The implementation now needs one source of truth for availability decisions so
all dispatch and user-facing checks remain aligned.

_____________________________________________________________________

## Decision

Use `cuprum._backend._check_rust_available()` as the shared resolver for:

1. Stream backend dispatch in `get_stream_backend()`.
2. Public helper `cuprum.rust.is_rust_available()`.

`_check_rust_available()` remains cached (`lru_cache`) and honours
`set_rust_availability_for_testing()`. `cuprum._rust_backend.is_available()` is
retained as the raw, uncached import probe for lower-level/internal callers only.

_____________________________________________________________________

## Consequences

- Public and internal users observe the same cached result when they need the
  dispatch-relevant availability value.
- Test overrides remain reliable because all consumers route through the same
  resolver and cache-clear entry point.
- Documentation now calls out raw versus cached probe semantics to avoid
  accidental direct use of `cuprum._rust_backend.is_available()`.
