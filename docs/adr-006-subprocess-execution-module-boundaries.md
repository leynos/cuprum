# Architectural decision record (ADR) 006: Subprocess execution module boundaries

## Status

Accepted on 2026-07-18. Cuprum divides the private subprocess execution
implementation by runner orchestration, stdin handling, and timeout handling.

## Date

2026-07-18.

## Context and problem statement

`cuprum/_subprocess_execution.py` combined subprocess spawning, stdout/stderr
consumer coordination, supplied-stdin lifecycle management, timeout
translation, and exit-event accounting. The module exceeded the project's
module-size policy and carried a `too-many-lines` suppression, obscuring the
distinct lifecycles that maintainers need to modify and test.

The split must preserve the private execution contract: `SafeCmd.run()` keeps
the same observable results, cancellation behaviour, timeout translation, and
event emission.

## Decision drivers

- Remove the module-size suppression by creating cohesive modules.
- Give stdin diagnostics and timeout translation clear ownership.
- Keep spawning and stream-consumer orchestration in one coordination module.
- Preserve existing private import compatibility where it remains necessary.
- Make the specialized lifecycles independently testable.

## Options considered

### Option A: retain the combined module and suppression

Keep all execution concerns in `_subprocess_execution.py` and retain the
`too-many-lines` suppression.

This avoids import changes but keeps unrelated lifecycles coupled and leaves a
policy exception in a core implementation module.

### Option B: split by lifecycle concern

Keep runner orchestration in `_subprocess_execution.py`; move stdin writing and
its logger to `_subprocess_stdin.py`; and move timeout translation plus shared
exit-event accounting to `_subprocess_timeout.py`.

This makes ownership explicit while retaining the runner as the composition
root.

### Option C: move all execution helpers into a generic utilities module

Create a broad helpers module without distinguishing the lifecycle each helper
belongs to.

This reduces the original file size but creates an ambiguous dumping ground and
does not improve ownership.

| Topic | Combined module | Lifecycle split | Generic helpers |
| --- | --- | --- | --- |
| Ownership | Mixed | Explicit | Ambiguous |
| Module-size policy | Suppression required | Compliant | Likely to drift |
| Test isolation | Coupled | Focused | Mixed |
| Spawn coordination | Local | Local | Fragmented |

_Table 1: Trade-offs for organizing private subprocess execution._

## Decision outcome / proposed direction

Choose Option B. `_subprocess_execution` remains the composition root for
spawning and stream consumers. `_subprocess_stdin` owns `_emit_stdin_error`,
`_write_stdin`, and `_spawn_stdin_writer`, including the `cuprum.stdin` logger.
`_subprocess_timeout` owns timeout details/errors, timeout translation, and the
exit-event helpers shared by timeout and normal completion paths.

The runner imports specialized helpers; the specialized modules do not create
subprocesses or expose public command APIs. `_resolve_timeout` remains defined
in `_subprocess_context`, and `cuprum.sh` imports it from that definition site
rather than through a redundant execution-module re-export.

## Goals and non-goals

### Goals

- Create coherent private module boundaries that remove the size suppression.
- Retain existing observable execution and error behaviour.
- Make stdin and timeout paths directly importable for focused tests.

### Non-goals

- Change the public `SafeCmd` or timeout API.
- Change process spawning, cancellation, or stream-consumer semantics.
- Introduce a new public module surface.

## Known risks and limitations

- Private import paths used outside the package may need adjustment because
  specialized helpers now live in their owning modules.
- The modules remain coupled through private execution-context types; that is
  intentional because the runner remains the composition root.

## Consequences

### Positive

- Each lifecycle has an obvious implementation home and focused tests.
- The `too-many-lines` suppression is no longer necessary.
- The stdin logger lives with the stdin behaviour it reports.

### Negative

- Imports span three private modules instead of one.
- Maintainers must preserve the boundaries when adding execution behaviour.
