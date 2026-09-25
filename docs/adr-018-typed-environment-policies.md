# Architectural decision record (ADR) 018: Typed environment policies

______________________________________________________________________

## Status

Accepted.

## Date

2026-09-21

______________________________________________________________________

## Context and problem statement

Cuprum's public execution context previously treated every environment mapping
as an additive overlay over the live parent process environment. An empty
mapping therefore inherited every variable, and omitted values could not remove
credentials, build settings, or continuous-integration metadata. That prevented
faithful migration from `subprocess.run(env=...)`, whose supplied mapping is a
complete child environment.

The execution API needs explicit replacement and deletion semantics while
retaining the existing live-overlay default. The resulting policy must apply
identically to direct commands and pipeline stages, remain safe for nested
`ContextVar` scopes, and never mutate `os.environ`.

## Decision

Expose `EnvMode` with `INHERIT`, `OVERLAY`, and `REPLACE` values, plus the
singleton `UNSET` marker. `OVERLAY` remains the default for `CuprumContext`,
`ScopeConfig`, `env()`, and `ExecutionContext`.

Environment policy composition is pure. An `OVERLAY` or `INHERIT` child keeps
its parent's effective mode and merges its values over the parent overlay. A
`REPLACE` child discards every parent overlay and establishes replacement mode.
`UNSET` markers survive composition, so deletion is applied only when a child
environment is rendered for spawning.

Rendering a replacement policy starts from `{}`. Every other mode starts from a
live copy of `os.environ`; a policy with no entries returns `None` so the
subprocess API inherits unchanged. Rendering then applies string values and
removes keys marked `UNSET`. Neither path writes to `os.environ`.

Scoped policy layers are below the per-call `ExecutionContext` layer. Inner
scopes win over outer scopes until a replacement boundary discards them, and
the per-call mapping wins over the resulting ambient policy. `cwd` remains a
separate per-call setting. On POSIX, replacement policies affect executable
lookup through their `PATH` just as they affect the child's environment.

## Consequences

- Existing callers keep the additive live-overlay behaviour without changes.
- Callers can now build minimal child environments and explicitly remove
  inherited secrets or build settings.
- A deliberately minimal replacement environment must include `PATH` when a
  child executable is referenced by a bare name.
- Observation records retain the composed overlay without reading live process
  state and label replacement-mode stages so consumers can identify the
  boundary.
