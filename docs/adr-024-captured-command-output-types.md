# Architectural decision record (ADR) 024: Type captured command output

## Status

Accepted on 2026-10-15.

## Date

2026-10-15.

## Context and problem statement

`CommandResult.stdout` and `stderr` are `str | None`. With default text
capture, both values are always strings and an empty stream is `""`. The
optional type therefore makes callers repeatedly coalesce with `or ""`, even
though that also hides a missing capture request. An upload failure can then
lose the diagnostic that would explain the failure.

The distinction matters at runtime as well as to static checkers. `None` means
capture was disabled; `""` means capture ran, and the child wrote no text. The
result type should preserve that distinction. Runtime captured results express
the guarantee, and the capture-sensitive overloads let static checkers expose
it to callers of default single-command runs without an assertion.

## Decision drivers

- Make the completed single-command capture guarantee visible at runtime and to
  static checkers.
- Preserve `CommandResult` and its optional-stream contract for uncaptured runs
  and existing consumers.
- Make explicit-options callers check capture before consuming text, so an
  omitted capture request cannot become an empty diagnostic.
- Keep pipelines, partial timeout output, field layout, and measurement
  metadata compatible.

## Options considered

1. **Add `CapturedResult`, capture-sensitive `SafeCmd` overloads, and checked
   narrowing.** Chosen and implemented. Default `SafeCmd.run` and `run_sync`
   calls, including calls with `output=None`, are typed as returning
   `CapturedResult`. Calls with an explicit `RunOutputOptions` or a
   `RunOutputOptions | None` variable remain typed as `CommandResult` because
   those values may disable capture.
2. **Make every result stream `str` and add a captured flag.** Rejected. This
   changes `CommandResult`'s public field contract and still requires every
   consumer to branch on a second field. `None` already communicates the
   uncaptured state and is required for pipeline interior stdout.
3. **Make `RunOutputOptions` generic over its capture setting.** Deferred as a
   possible follow-up. It would widen the generic surface across options,
   pipelines, and wrappers; this change needs only the default-versus-explicit
   call distinction.

## Decision outcome

Add `CapturedResult` as a frozen, slotted subclass of `CommandResult` that
redeclares `stdout` and `stderr` as `str`. Its runtime validation rejects any
non-string stream. It keeps the six existing positional fields, the seventh
`relay_fallbacks` slot, and keyword-only measurement fields.

Every single-command text run that captures output constructs `CapturedResult`,
including an explicit `RunOutputOptions(capture=True)` call. A run with capture
disabled still constructs `CommandResult` with `None` streams. The overloads
type calls with omitted `output` or `output=None` as returning
`CapturedResult`. Calls with an explicit `RunOutputOptions` or a
`RunOutputOptions | None` variable are typed as returning `CommandResult`, even
when the runtime options instance enables capture. The implementation
annotations remain `CommandResult`.

`CommandResult.require_captured()` returns an existing `CapturedResult`
unchanged. If both streams are strings on an ordinary `CommandResult`, it
copies all fields into a `CapturedResult`. Otherwise, it raises `ValueError`,
names each stream that is unavailable as text, and points to
`RunOutputOptions(capture=True)`. No new exception type is introduced.

Pipeline methods and result construction remain unchanged. With capture,
final-stage stdout and each stage's stderr are strings at runtime, while an
interior stage's stdout remains `None` because it feeds the next process.
`require_captured()` can narrow the final stage but rejects an interior stage.
`TimeoutExpired.output` and `stderr` remain `str | bytes | None` and may be
partial because termination can precede stream completion.

## Consequences

- Default single-command callers receive strings for `stdout` and `stderr`
  without `or ""`, and the overloads expose that guarantee to static checkers.
- Explicit-options callers use `require_captured()` to prove both streams are
  present. This preserves the useful failure when capture was forgotten.
- Pipeline construction, timeout exceptions, and measurement-field
  compatibility remain unchanged. Default single-command runs now return the
  distinct `CapturedResult` dataclass. It does not compare equal to a
  `CommandResult` with the same fields, and `type(result) is CommandResult` no
  longer matches captured runs. Consumers relying on those checks should use
  `isinstance(result, CommandResult)` and compare fields individually.
- Runtime tests cover construction and `dataclasses.replace`; `ty` fixtures
  cover the overloads and optional fields on explicit-options results.

## Assumptions

- The project retains its existing decoded-text mode as the default.
- A generic capture parameter for `RunOutputOptions` remains a separate design
  decision and is not needed to express these overloads.
