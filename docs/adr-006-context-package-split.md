# ADR-006: Split cuprum/context.py into a context package

_____________________________________________________________________

## Status

Accepted on 2026-07-22. This ADR records the decision to split the monolithic
`cuprum/context.py` module into a `cuprum/context/` package. It follows the
work tracked in issue #116 and delivered in PR #157.

## Date

2026-07-22

_____________________________________________________________________

## Context and Problem Statement

`cuprum/context.py` had grown past the repository's 400-line-per-module
ceiling enforced by lint, and it mixed four concerns with different
audiences and change cadences: pure environment-overlay merging, the core
immutable domain types, the process-wide `ContextVar` plumbing, and the
registration handles and factories. Keeping all of this in a single file
made the boundaries between concerns implicit and left the module unable to
stay within the line-count ceiling as new context features were added.

_____________________________________________________________________

## Decision

Split the module into a `cuprum/context/` package with one module per
concern:

- `env_overlay.py` holds the pure environment-overlay merging logic, with no
  dependency on `ContextVar`.
- `core.py` holds the `CuprumContext` and `ScopeConfig` dataclasses, the
  `ContextError` root of the context-domain exception hierarchy and its
  `ForbiddenProgramError` subclass, timeout validation, and the hook type
  aliases.
- `state.py` holds the `ContextVar` plumbing and the associated set/reset
  helpers.
- `registration.py` holds `scoped`, the token registration base, the
  registration handles, and the `allow`/`before`/`after`/`env`/`observe`
  factories.

`cuprum/context/__init__.py` re-exports the existing public `__all__`
unchanged, so `from cuprum.context import …` continues to work without
changes at call sites. Each module stays under 400 lines, and new context
features should be added to the module matching their concern.

_____________________________________________________________________

## Consequences

- The public API surface is preserved, so no importer changes are required.
- Each module stays within the line-count and docstring-coverage gates.
- The domain exception hierarchy now has an explicit, importable
  package-level root (`ContextError`).
- Concern boundaries are enforced by module structure rather than by
  convention alone.
