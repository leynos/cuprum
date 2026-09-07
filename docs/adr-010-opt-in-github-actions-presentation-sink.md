# Architectural decision record (ADR) 010: Opt-in GitHub Actions presentation sink

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
every echo write, and makes local reproduction of CI behaviour implicit. An
environment-triggered presentation change also breaks the contract that the
same call produces the same parent-facing output regardless of where it runs.

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

The execution layer opens the session before the subprocess starts and closes
it on every terminal path — success, non-zero exit, timeout, cancellation, and
spawn failure — through the same shielded finalization that drains observe-hook
tasks. Pipelines map their first failing stage's exit code onto the outcome;
the annotation reports the pipeline, not one stage. `close` is idempotent, so
overlapping terminal paths cannot double-annotate.

Echoed output reaches the adapter only when a run echoes (`echo=True` or a
pipeline's stage streams); capture is unaffected in every configuration.

## Goals and non-goals

### Goals

- One opt-in line opts a command or pipeline into Actions framing.
- Child output cannot forge workflow commands while a group is open.
- Every terminal path closes the session exactly once, cancellation-safely.
- Failure annotations carry bounded, categorical information only.

### Non-goals

- Changing capture, exit codes, or the returned result types.
- Auto-detecting CI environments or adding global configuration.
- Supporting other CI vendors' log formats through the same adapter (new
  adapters implement the protocol instead).
- Making annotations available for commands that do not run (plan-only paths).

## Known risks and limitations

- The stop-commands lease suppresses *all* workflow-command interpretation
  inside the group, including output a trusted child intentionally emitted;
  callers who need child-emitted annotations should run without the sink.
- Workflow commands assume the runner parses the destination stream; a caller
  redirecting `destination` to a non-Actions stream gets literal command text.
- Concurrent runs sharing one process's stderr interleave their framing at
  write granularity; the runner associates each command with the group open
  immediately before it, which is correct for sequential runs and best-effort
  for interleaved ones.

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
