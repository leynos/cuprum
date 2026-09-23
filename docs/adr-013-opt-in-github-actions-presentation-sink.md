# Architectural decision record (ADR) 013: Opt-in GitHub Actions presentation sink

## Status

Accepted on 2026-09-07. Cuprum frames a run's parent-facing output in GitHub
Actions workflow-command groups and annotates failures through an opt-in
presentation sink, `cuprum.sinks.GitHubActionsSink`.

## Date

2026-09-07.

## Context and problem statement

Cuprum's Continuous Integration (CI) callers run inside GitHub Actions jobs. A
job that executes several Cuprum commands sees their echoed output interleaved
in the step log, with no collapsible grouping per command, and a failed command
surfaces only through the step's exit status — the runner shows no error
annotation pointing at the failing command.

Both gaps can be closed with GitHub Actions workflow commands: `::group::` and
`::endgroup::` bracket a collapsible log section, and `::error::` publishes an
annotation. Emitting those commands is presentation, not execution: capture,
success semantics, and the returned result must stay exactly as they are, and
callers outside GitHub Actions must not pay for framing they never asked for.

Two risks shape the design. First, child output is untrusted: a command that
prints a workflow command would otherwise forge annotations or close groups it
does not own. Second, the run's terminal paths — success, non-zero exit,
timeout, cancellation, and earlier failure — are numerous and partially
asynchronous, so the framing close must survive cancellation and be idempotent.

## Decision drivers

- Keep the feature opt-in per invocation, with no behaviour change by default.
- Title each group with the command's program arguments, as CI callers read
  collapsed groups by their command line.
- Prevent child output from injecting workflow commands into the log.
- Emit exactly one error annotation per failed run, carrying only bounded,
  categorical detail rather than exception text or argument values.
- Preserve capture and the returned result on every path, including timeout and
  cancellation teardown.
- Keep protocol knowledge (workflow-command syntax) inside the adapter, so the
  execution layer stays presentation-agnostic.

## Options considered

### Option A: frame output unconditionally when a `CI` environment variable is set

Detect GitHub Actions through the environment and always emit group framing.

This surprises callers whose child output is itself parsed, doubles the cost of
every echo write, and makes local reproduction of CI behaviour implicit. It
also changes behaviour for every caller the moment the environment variable
appears, with no per-invocation opt-in and no way to reproduce or suppress the
framing deliberately.

### Option B: add `group`/`annotate` flags to `RunOutputOptions`

Teach the execution layer about workflow commands directly.

This spreads Actions-specific formatting across every terminal path of the
runner and pipeline implementations, couples the core to one CI vendor's log
format, and multiplies the places a future presentation change must touch.

### Option C: a presentation-sink protocol with an Actions adapter

Define a narrow adapter protocol in `cuprum.sinks.base`: an `OutputSink` opens
one `OutputSession` per run, the run routes echoed output through the session's
`log` writer, and the execution layer closes the session exactly once per
terminal path with a bounded `SessionOutcome` (a closed outcome set, an
optional exit code, and an optional categorical detail). `GitHubActionsSink`
implements the protocol with workflow commands; the execution layer never sees
them.

This keeps the framing knowledge in one adapter, leaves no-sink runs
byte-for-byte unchanged, and lets other presentation adapters (for example, a
future JUnit reporter) reuse the same protocol.

## Decision outcome / proposed direction

Option C. `cuprum.sinks.base` defines the protocol and the closed
`TerminalOutcome` set (`exit_zero`, `exit_nonzero`, `timeout`, `cancelled`,
`error`); `cuprum.sinks.github_actions` implements the adapter;
`RunOutputOptions.sink` carries the opt-in on both `SafeCmd` and `Pipeline`
entry points.

Framing order per run:

1. `::group::<title>` opens the group before the subprocess starts, titled
   with the program arguments for single commands (the adapter's `title`
   override wins; pipelines default to `pipeline`).
2. A fresh stop-commands bracket — `::stop-commands::<token>` with a
   cryptographically random per-session token — opens immediately after the
   group command, so the runner processes the group command itself and then
   stops interpreting child output as workflow commands for the rest of the run.
3. The run's echoed stdout and stderr flow through the session's `log`
   destination, inside the framing.
4. At teardown the session releases the lease before writing `::endgroup::`,
   so the runner processes the endgroup command itself.
5. A failed outcome emits exactly one `::error::` annotation after the lease
   release. The title is the bounded run label; the message is the categorical
   detail (`timeout`) or the outcome value. Exception text and argv never reach
   it.

The execution layer opens the session before the subprocess starts and
finalizes the run's `_SinkBracket` on every terminal path — success, non-zero
exit, timeout, cancellation, and spawn failure. The bracket is take-once, so
the first close wins and a later guard cannot overwrite an early, precise
outcome. The close runs before the shielded drain of observe-hook tasks: the
drain can aggregate a failing after-hook or observe task into a
`BaseExceptionGroup`, so closing afterwards would record that aggregate instead
of the precise timeout/cancellation outcome, and a drain that raised would skip
the close entirely. The adapter's own `close` idempotency remains a protocol
requirement (`OutputSession.close` in `cuprum.sinks.base`), now the backstop
rather than the primary mechanism. Pipelines map their first failing stage's
exit code onto the outcome; the annotation reports the pipeline, not one stage.

Echoed output reaches the adapter only when a run echoes (`echo=True` or a
pipeline's stage streams); capture is unaffected in every configuration.

### Activation: environment-gated, adapter-local

Per issue #360, `GitHubActionsSink` is inactive by default outside GitHub
Actions. `open_session` reads `GITHUB_ACTIONS` from the parent process
environment on every run — never cached at import or construction time — and
declines activation unless it holds the runner's exact value `true`; any other
value, including `1` or `TRUE`, keeps the sink inactive. An inactive sink
writes nothing: no `::group::`, no stop-commands bracket, no `::endgroup::`,
and no `::error` annotation, so the run keeps its plain destinations and
parent-facing output byte-for-byte.

This gate is adapter-local behaviour, not global auto-configuration: the
execution layer never inspects the environment, and other adapters (or a custom
`OutputSink`) keep whatever activation policy they define. Passing a sink does
not by itself guarantee workflow-command output outside GitHub Actions.
`GitHubActionsSink(force=True)` overrides the check deliberately, for local
reproduction of CI framing and non-standard runners; it is an explicit opt-in,
not an environment-triggered default.

## Goals and non-goals

### Goals

- One opt-in line opts a command or pipeline into Actions framing.
- A sink without an explicit `force` request stays inactive outside GitHub
  Actions, so runs keep their plain parent-facing output there.
- Child output cannot forge workflow commands while a group is open.
- Every terminal path closes the session exactly once, cancellation-safely.
- Failure annotations carry bounded, categorical information only.

### Non-goals

- Changing capture, exit codes, or the returned result types.
- Auto-enabling framing for callers who pass no sink, or adding global
  configuration; the `GITHUB_ACTIONS` gate lives inside the adapter and applies
  only to runs that opt in.
- Supporting other CI vendors' log formats through the same adapter (new
  adapters implement the protocol instead).
- Making annotations available for commands that do not run (plan-only paths).

## Known risks and limitations

- The stop-commands lease suppresses *all* workflow-command interpretation
  inside the group, including output a trusted child intentionally emitted;
  callers who need child-emitted annotations should run without the sink.
- Workflow commands assume the runner parses the destination stream; a caller
  redirecting `destination` to a non-Actions stream gets literal command text.
- Concurrent runs sharing one presentation destination are **not** protected by
  the lease. The runner holds one stop-commands state per stream, so it tracks
  only the most recent lease: a second session opened while the first is still
  active writes its `::stop-commands::` line into a destination the first lease
  is already suppressing, and that line is therefore never acted on. When the
  first session releases its lease, interpretation resumes for the shared
  destination while the second session is still open, so child output the
  second session mirrors after that point *is* interpreted as workflow
  commands. Framing is additionally misassociated, because the runner closes
  the group that is open when the first session's release lands rather than the
  one that session opened. Attach the sink to concurrent runs only when each
  writes to its own destination. This is a known, unfixed limitation:
  serializing sessions over one destination, or declining to open an
  overlapping one, changes the session lifecycle and is tracked separately.

## Consequences

### Positive

- CI callers get collapsible per-command groups and error annotations without
  changing how they build or run commands.
- The adapter protocol gives future presentation integrations a typed seam the
  execution layer already honours.
- Injection safety and annotation hygiene are enforced once, inside the
  adapter, rather than at every call site.

### Negative

- Every terminal path of the runner and pipeline now owes the session a close,
  which maintainers must preserve when adding exit paths.
- The protocol is an additional public surface to keep stable.
- The stop-commands lease hides genuine workflow commands a child emits while
  framed, which may surprise callers migrating scripts that relied on them.

### Amendment (2026-09-22): `group` and `annotate_failure` as a spelling of Option C

Issue #375 asked for two convenience flags on `RunOutputOptions`, which is
Option B's surface. The decision stands, and the flags ship: the question this
amendment answers is why they are not the Option B this ADR rejected.

Option B was rejected because it *teaches the execution layer about workflow
commands* — it "spreads Actions-specific formatting across every terminal path
of the runner and pipeline implementations." The flags as implemented do not do
that. They are a constructor for the Option C adapter: `__post_init__` builds a
`GitHubActionsSink` with the matching toggles and stores it in the existing
`sink` field, so every run path continues to see an opaque `OutputSink` and
`::group::` appears nowhere outside `cuprum/sinks/github_actions.py`. An
explicit `sink=` wins and makes the flags no-ops, so the flags can never
displace a caller's own adapter.

That is the distinction worth recording. Option B was a *placement* objection —
where the vendor's log format lives — not an interface objection to two
booleans existing. The flags add no workflow-command code to the runner or
pipeline, no branch to a terminal path, and no second place a presentation
change must touch. They inherit the session lifecycle and annotation hygiene
that the adapter already enforces. Group mode also inherits its stop-commands
shield. Annotation-only mode deliberately emits no group or stop-commands
lease, as required by the flag contract, and keeps echoed stdout and stderr on
their usual destinations; child output in that mode can therefore still emit
workflow commands. Had the flags been implemented by writing workflow commands
from the run paths directly, the Option B rejection would have applied
unchanged.

Two consequences the option text did not anticipate are settled here rather
than left to be rediscovered. First, the toggles are validated as `bool` and a
non-`bool` raises `TypeError`: they gate workflow commands, so a merely truthy
value would frame a run on the strength of something the caller never
documented as a flag. Second, the two flags are independent, so
`annotate_failure=True` alone yields an annotation with no group. That is a
supported configuration rather than a degenerate one — a run summary entry
without collapsible logs — and the stop-commands lease is suppressed with the
group, because a lease with no group would silence workflow-command
interpretation for the rest of the step and display nothing for it.
