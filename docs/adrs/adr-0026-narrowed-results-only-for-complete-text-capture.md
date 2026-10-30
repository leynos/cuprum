# Architectural decision record (ADR) 026: Narrow captured results only for complete text

## Status

Accepted on 2026-10-19.

## Date

2026-10-19.

## Context and problem statement

[ADR-024](adr-0024-captured-command-output-types.md) decided that "every
single-command text run that captures output constructs `CapturedResult`". It
was written when capture was one boolean: either both streams were retained as
text, or neither was. Two later changes break that equivalence.

Per-stream capture policies
([issue #443](https://github.com/leynos/cuprum/issues/443)) let a caller
capture one stream and not the other. `RunOutputOptions` now resolves to a
`(capture_stdout, capture_stderr)` pair, and either member may be `None`.

`FileBackedCapture` reports a stream it spilled as absent from the payload. The
text is in the file named by the capture record, not in `stdout`, because
retaining it as well would mean holding the whole stream on the heap — the
memory cost the policy exists to avoid. `test_capture_spill.py` pins that
report, and `test_capture_spill_cleanup.py` relies on it.

`CapturedResult` is a subclass that redeclares `stdout` and `stderr` as `str`
and validates them in `__post_init__`. It therefore cannot represent a
single-stream capture, nor a text-mode spill, without either inventing an empty
string where the child wrote nothing or moving the type back to `str | None`
and losing the guarantee the class exists to give.

## Decision drivers

- Keep ADR-024's runtime guarantee: a caller of a default, fully captured
  single-command run must not have to coalesce `None` with `or ""`.
- Report what a run actually produced. A spilled stream's text is at a path the
  result names; a stream that was never captured is `None`. Neither is `""`.
- Keep one result class for the payloads of a text run, so callers who never
  read `captures` are unaffected by the policy vocabulary.
- Do not resurrect a run-wide `capture` boolean on the execution bundle: the
  policy pair is the model, and a second flag would be a second source of truth
  for the same question.

## Options considered

1. **Narrow whenever the run captured anything, filling absent streams with
   `""`.** Rejected. It reports a spill, and an uncaptured stream, as an empty
   one, which is the mistake `require_captured()` and ADR-024 were introduced
   to prevent.
2. **Narrow only when both settled payloads are decoded text.** Chosen. The
   predicate is a property of what the run produced, not of the configuration
   that produced it, so it cannot disagree with the payloads it guards.
3. **Remove `CapturedResult` and rely on `require_captured()`.** Rejected. It
   discards ADR-024's default-run typing guarantee and the compatibility
   argument recorded in the users' guide.

## Decision outcome

`cuprum._subprocess_execution._build_command_result` constructs
`CapturedResult` exactly when both settled payloads are `str`, through
`cuprum._result_assembly._is_captured_text_pair`. Every other text run
constructs `CommandResult`, which carries the same payloads and the same
capture records. A run that captured nothing and a run whose capture is
reported elsewhere are therefore both ordinary results, and only a run whose
text is wholly present is narrowed.

`captures` is threaded into the `CapturedResult` construction as well, so the
record naming a spilled or bounded stream travels with either class. For a
`FileBackedCapture` that spilled, `captures` is the only part of the result
that names the file.

A default `RunOutputOptions()` run resolves both streams to `FullCapture`, so
both payloads are `str` and ADR-024's contract holds unchanged. This ADR
narrows the condition under which that contract is claimed; it does not weaken
it.

## Consequences

- A caller who captures one stream, or spills one to a file, receives a
  `CommandResult`. `require_captured()` still rejects it, naming the stream
  that is not text, which is the correct answer for a stream whose text is in
  an artefact.
- A caller reading `.stdout` from `run()`/`run_sync()` under an explicit
  `RunOutputOptions` already receives a `CommandResult` statically; the runtime
  class now agrees with that annotation for per-stream and spilled runs.
- The static overloads are unchanged. Calls with omitted `output` or
  `output=None` remain typed as returning `CapturedResult`, which is what they
  return while both streams are captured in full.
