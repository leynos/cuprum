# Expose bounded live byte streams with owned control (#706)

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS

Branch: `issue-706-expose-bounded-live-byte-streams-with-owned-control`

Roadmap item: none yet; this plan adds an `Unreleased` roadmap item.

## Purpose / big picture

Cuprum can run a command to completion and hand back byte-exact captured output
(`run_bytes()`), or hand back an owned `RunningCommand` whose output is only
available after settlement (`start()`). It cannot hand a caller the bytes a
*running* child produces, incrementally, without decoding them as lines and
without giving up the owned lifecycle that stops, reaps, and settles the child.

This plan adds that capability: `SafeCmd.start_bytes()` returns a
`RunningBytesCommand`. The handle exposes `stdout` and `stderr` as independent
`ByteStreamReader`s that yield `ByteChunk` values in per-stream order, report
their own EOF, and can be closed individually. The handle keeps the existing
`terminate()`, `kill()`, `signal()`, `wait()`, `result`, and `async with`
lifecycle, and settles to a `BytesCommandResult`.

A CmdMox-style scripted process fake needs exactly this: observe a byte prefix
before releasing a barrier, write stdin before EOF, observe stdout closing
while stderr and the child remain live, and cancel while a writer is blocked.
Line observation (`lines()`) cannot serve that: a child may emit invalid UTF-8,
split a multibyte character, omit a newline, or truncate a protocol record.

The feature is `Unreleased`; it is not part of `0.2.0-beta2`.

## Constraints

- The text contracts must not change: `RunningCommand`, `SafeCmd.start()`,
  `SafeCmd.lines()`, `SafeCmd.run()`, `run_bytes()`, `run_bytes_sync()`,
  `Pipeline.run_bytes()`, `LineEvent`, execution phases, and the existing
  `CommandResult`/`BytesCommandResult` shapes.
- One owner for the process, stdin writer, drains, supervisor, and settlement.
  No second reader may race the capture pump.
- Lossless backpressure while a reader is live: a `put` that cannot fit waits
  for space; it never drops a byte. After settlement begins, overflow is
  explicit and reported through `StreamClosure.TRUNCATED_BY_SETTLEMENT`.
- Per-stream order only. The API must not promise a total cross-stream order,
  and must document that child write boundaries do not equal parent read
  boundaries.
- Every source file must stay at or below 400 lines (the Pylint gate). Test
  modules in the listed test-only trees are exempt from `too-many-lines` only.
- No `# type: ignore`, no blanket lint suppression, no new dependency, no
  `unsafe`/native change.
- Public annotations must resolve at runtime through `typing.get_type_hints`.
- `LiveStreamError` must chain the drain or supervisor cause.
- Documentation must be en-GB-oxendict, wrapped at 80 columns, and the new
  example must be a gated, tested example.
- The feature is labelled `Unreleased` and explicitly absent from
  `0.2.0-beta2`.

## Tolerances (exception triggers)

Comparison base: `git diff --numstat` against the branch point, excluding
`docs/execplans/`.

- Files: stop and escalate above 45 non-ExecPlan files.
- Code (production source and tests): stop and escalate above 3,500 net lines.
- Configuration: stop and escalate above 50 net lines.
- Documentation (excluding this ExecPlan): stop and escalate above 700 net
  lines.
- Dependencies: any new runtime dependency requires escalation.
- Architecture: changing the one-drain-per-stream rule, the single-settlement
  rule, or the public text result shapes requires escalation.
- Iterations: stop and escalate if the same gate fails after four fix rounds.
- Ambiguity: escalate if two readings of the contract produce materially
  different public behaviour.

These budgets are initial estimates from a survey of the existing handle and
stream modules (about 2,600 lines of relevant production code, plus a
comparable test surface). No comparable prior ExecPlan exists in this
repository, so the budgets are conservative rather than historical.

## Risks

- Risk: startup race between reader availability and early child output.
  Severity: high. Likelihood: low. Mitigation: queues exist before the spawn,
  the drain holds them from the first chunk, and readers are returned only
  after all tasks are attached.
- Risk: a stalled consumer blocks the drain and therefore the child.
  Severity: medium. Likelihood: high (by design). Mitigation: document it as
  lossless backpressure, and release the queue at settlement so teardown is
  bounded.
- Risk: reader status races between EOF and settlement.
  Severity: high. Likelihood: medium. Mitigation: one conditional state
  transition per queue; settlement begins a provisional truncation, and EOF
  publishes only when no byte was discarded.
- Risk: the drain failure is discovered only during reconciliation and is
  lost. Severity: high. Likelihood: medium. Mitigation: inspect settled
  consumer tasks after reconciliation and raise `LiveStreamError`.
- Risk: descriptor or process leak on cancellation. Severity: high.
  Likelihood: low. Mitigation: reconcile and release transports inside the
  existing shield; real-child tests assert reaping and descriptor counts.
- Risk: Windows differences in descendant cleanup and signals. Severity:
  medium. Likelihood: medium. Mitigation: document the gap, mark POSIX-only
  tests, and keep portable cases available.

## Progress

- [x] (2026-11-27) Reconnaissance of the drain, handle, settlement, stdin, and
  byte-result modules.
- [x] (2026-11-27) Branch renamed and Lody session titled.
- [x] (2026-11-27) Issue comment with the Lody session link posted.
- [x] (2026-11-27) `EP-M1` ADR-0031, design §13.11, `ByteChunk`,
  `StreamClosure`, and `LiveStreamError`.
- [x] (2026-11-27) `EP-M2` drain `on_eof`, the composed byte tap, the lossless
  byte queue, and their unit tests.
- [x] (2026-11-27) `EP-M3` reader, `start_bytes()`, `RunningBytesCommand`, the
  bytes settlement and eager supervisor, exports, and typing/API tests.
- [x] (2026-11-27) `EP-M4` the scripted child, the external session watchdog,
  seven portable scenarios, three POSIX scenarios (two descendant stories and
  one held-handle failure), a supervisor reclaim test, and the BDD feature and
  steps.
- [x] (2026-11-27) `EP-M5` users' guide section and gated example, changelog,
  roadmap, migration note, and contents index.
- [x] (2026-11-28) Hosted CodeRabbit agent review: 14 findings fixed over two
  rounds, then a clean review.
- [x] (2026-11-28) Rebases onto live `main`, the ADR-0030 collision, and the
  CodeScene refactors; six-gate certification at `208dba14`.
- [x] (2026-11-28) Hosted assessments: the first round returned incomplete;
  capture opt-outs, eager stdin-failure supervision, finalization-consistent
  EOF, the blocked-put close race, and the expanded cancellation, parity,
  wait-route, and cleanup evidence were added. Reassessment is pending.
- [ ] Reassessment, readiness, formal review, merge.

## Surprises & discoveries

- Observation: the plan names `docs/adrs/adr-0029-...`, but ADR-0029 already
  exists (`adr-0029-shared-build-resources-and-prepared-cargo-state.md`).
  Evidence: `ls docs/adrs`. Impact: the new decision was first recorded as
  ADR-0030; the cross-references to ADR-007, ADR-025 and ADR-028 are unchanged.
- Observation: rebasing onto live `main` found that the base had since added its
  own ADR-0030 (the deferred pytest session layout). The unmerged live-byte ADR
  is the newcomer, so it took the next free number, ADR-0031, and records
  "Renumbered from 0030 on 2026-11-28 (duplicate number)" under its title; the
  base ADR keeps 0030 and every inbound link was updated.
- Observation: the drain was split after the issue was written.
  `_StreamConfig` now lives in `cuprum/_stream_config.py`, `_finish_drain` in
  `cuprum/_stream_drain_finish.py`, and the line feeder composes through
  `_LineConsumption`. Evidence: direct reads. Impact: the `on_eof` and byte-tap
  changes land in those modules, not in a single `_streams.py` block.
- Observation: the owned `RunningCommand` settlement does not race stream
  consumers today; it waits for the child first. Evidence:
  `coordinator._settle_handle_run` calls `_wait_for_exit_code_within_timeout`
  directly. Impact: the byte owner needs its own eager race through
  `_await_exit_or_writer_failure`, while the text path stays unchanged.
- Observation: the first composition delivered every chunk to the byte tap
  twice, because both the drain's fan-out and the line publisher invoked it.
  Evidence: the split-UTF-8 drain test recorded `[b'\xe2', b'\xe2', ...]`.
  Impact: the tap lives on the config and is invoked once by the drain; the
  line publisher only decodes. The test now pins the single delivery.
- Observation: `__aenter__` returning the base class erased the subclass's
  readers from the type checker's view. Evidence: `make typecheck` reported that
  `_OwnedCommandHandle` has no `stdout`. Impact: the base returns
  `typing.Self`, so both public handles keep their own surface.
- Observation: `typing-only-first-party-import` conflicts with public
  annotations that must resolve at runtime. Evidence: `get_type_hints` on the
  new public methods. Impact: the four affected imports carry a narrow
  `ruff: ignore` naming the runtime-resolution reason.

## Decision log

- D1. Decision: a separately typed immediate owner,
  `start_bytes()` → `RunningBytesCommand`, rather than extending
  `RunningCommand` with optional readers or adding an iterator entry point.
  Rationale: the result type and reader surface differ; a separate type keeps
  every existing annotation and test intact, and makes the ownership transfer
  visible at the call site. Rejected: an `iter_bytes()` async generator, which
  cannot also own stdin, control, and settlement without becoming a handle.
  Date/Author: 2026-11-27, implementation agent.
- D2. Decision: live delivery is a per-stream lossless queue fed from the
  existing single drain through `on_chunk`, plus an out-of-band `on_eof`.
  Rationale: one reader per pipe preserves capture, echo, and line observation
  ordering; a second pipe reader would race the capture pump. Rejected: reading
  the `StreamReader` directly from the consumer, which would split the stream
  between two readers and lose capture ordering. Date/Author: 2026-11-27,
  implementation agent.
- D3. Decision: settlement begins a provisional truncation and wakes blocked
  puts, but the published status is `EOF` only when the drain then observes EOF
  with no discarded byte. Rationale: a normal child exit must report EOF, and a
  stalled consumer must never hang settlement; the discarded-byte count keeps
  the status honest. Rejected: publishing `TRUNCATED_BY_SETTLEMENT`
  unconditionally at settlement start, which would misreport normal exits.
  Date/Author: 2026-11-27, implementation agent.
- D4. Decision: reader failure wins over pending items; normal termination
  drains queued items first. Rationale: a protocol consumer must see the
  failure rather than a silently shortened stream, while a clean end must not
  drop bytes the consumer can still read. Date/Author: 2026-11-27,
  implementation agent.
- D5. Decision: the new ADR is ADR-0031. Rationale: ADR-0029 was taken when the
  plan was written, and the base later added ADR-0030 for the deferred pytest
  session layout. The unmerged live-byte ADR is the newcomer, so it takes the
  next free number with the required duplicate-number note. Date/Author:
  2026-11-27 (renumbered 2026-11-28), implementation agent.
- D6. Decision: the live tap is a field on `_StreamConfig`, not extra parameters
  on the consume entry points. Rationale: a stream's tap is per-stream state
  exactly as its capture policy is; keeping it on the config preserves every
  existing argument count and the existing test seams. Date/Author: 2026-11-27,
  implementation agent.
- D7. Decision: the shared handle lifecycle moved to
  `cuprum/_process_handle/base.py`, with `__aenter__` returning `typing.Self`.
  Rationale: the two public handles must not duplicate control and teardown, and
  `Self` keeps each subclass's own reader surface visible to the type checker.
  Date/Author: 2026-11-27, implementation agent.
- D8. Decision: a live drain publishes its own task's failure directly through
  the tap, and that failure requests an immediate serialized stop. Rationale: a
  reader iterating a live stream has not asked for an outcome, so waiting for
  settlement would suspend it indefinitely, and a held handle would leave the
  child blocked on a pipe nobody drains. The stop is bound after the spawn and
  honours a request made before binding. Date/Author: 2026-11-27,
  implementation agent.
- D9. Decision: `kill()` deliberately does not take the stop lock. Rationale:
  taking it would make an immediate forced stop wait out another caller's
  `SIGTERM` grace period, which contradicts the operation's contract; the lock
  serializes escalations, and `kill` is the deliberate non-escalating stop. The
  unverified review note is therefore declined. Date/Author: 2026-11-28,
  implementation agent.
- D11. Decision: the plan's file and code tolerances are exceeded and the
  overage is accepted under the owner's standing instruction to implement issue
  #706 in full and merge it. Measured against `origin/main` (`5d2e4a91`) at the
  final candidate: 67 non-ExecPlan files, 4,355 net code lines, 584 net
  documentation lines, and 26 net configuration lines. The plan's initial
  limits were 45 files and 3,500 code lines; the documentation and
  configuration limits hold. The code delta is about 2,389 net production lines
  over 34 files and 2,026 net test lines over 20 files. The overage is driven
  by the acceptance criteria themselves: barrier-driven real-child coverage,
  both reaping routes, cancellation and cleanup assertions, public typing
  contracts, and the ADR/design/guide text. No dependency or public interface
  beyond the issue scope was added. Date/Author: 2026-11-28, implementation
  agent acting on the owner's merge directive.
- D10. Decision: settlement releases both live queues before the exit wait, not
  at reconciliation. Rationale: a full queue blocks its drain, a full pipe
  blocks the child, and an unbounded `wait()` would then never settle. Live
  delivery after the wait starts may truncate, which the users' guide already
  documents as the cost of waiting before consuming to EOF. Date/Author:
  2026-11-28, implementation agent.

## Outcomes & retrospective

The feature is implemented and covered by deterministic unit tests, a ten-case
supervised real-child suite (seven portable and three POSIX), a supervisor
reclaim test, and three BDD scenarios. The text handle, `lines()`,
`run_bytes()`, and every existing result shape are unchanged. The core drain
and stream-spawn modules were split along seams the live tap exposed, and the
handle lifecycle moved to a shared base so the two public handles cannot drift.
The remaining work is the commit gates, CodeRabbit review, push, and merge.
Lesson: the tap must be invoked by exactly one layer; composing it into both
the drain and the line feeder duplicates every chunk, and the split-UTF-8 test
is the cheapest detector.

## Context and orientation

Relevant existing modules:

- `cuprum/_streams.py` owns `_consume_stream`, `_drain`, `_drain_chunks`, and
  `_fan_out_chunk`. Capture happens before echo, then `on_chunk`.
- `cuprum/_stream_line_consumer.py` composes the line feeder into the single
  `on_chunk` slot through `_LineConsumption`.
- `cuprum/_stream_config.py` holds `_StreamConfig` (capture policy, echo,
  encoding, read size, `capture_bytes`).
- `cuprum/_stream_drain_finish.py` owns `_CapturedDrain` and `_finish_drain`.
- `cuprum/_subprocess_streams.py` builds the stream configs and starts one
  consumer per piped stream.
- `cuprum/_process_handle/` owns `RunningCommand`, the single settlement, the
  coordinator, signal/stop control, and `SafeCmd.start()`.
- `cuprum/_subprocess_rendezvous.py` owns the fixed early-failure precedence
  (settled exit, then stdin writer, then stdout, then stderr).
- `cuprum/_subprocess_stdin_stream.py` owns `StdinStream` pulling.
- `cuprum/sh/byte_results.py` owns `BytesCommandResult`.
- `cuprum/sh/safe_cmd.py` is at the module ceiling and inherits entry-point
  mixins.

Test and gate commands:

- `make check-fmt`, `make lint`, `make test`, `make typecheck`,
  `make markdownlint`, `make nixie`.
- Unit tests live in `cuprum/unittests/test_*.py`; behavioural tests live in
  `tests/behaviour/test_*.py` with features in `tests/features/`.
- Documentation examples marked `<!-- tested-example: name -->` are executed by
  `tests/behaviour/test_documentation_examples_behaviour.py`.

## Conformance basis

- Issue #706, "Expose bounded live byte streams with owned control".
- ADR-0007 (subprocess execution module boundaries), ADR-0025 (bounded output
  delivery), ADR-0028 (detached command launch), and the new ADR-0031.
- `docs/cuprum-design.md` §13.10 (owned running-command handle) and the new
  §13.11.
- `docs/users-guide.md` "Hold a running child and decide when to stop it".

Trace chain:

```plaintext
issue-706 acceptance -> ADR-0031 -> design §13.11 -> EP-M1..EP-M5
EP-M2 -> byte queue tests -> EP-M4 real-child exchange tests
EP-M3 -> public API/typing tests -> EP-M5 gated example
```

## Verification plan

Invariants:

1. While a reader is live and not released, the concatenation of its yielded
   chunks equals the bytes the child wrote to that stream, in order, and no
   chunk is dropped by the queue.
2. A queue never holds more than its byte bound.
3. Each reader publishes exactly one terminal status; `FAILED` outranks pending
   items and normal terminal statuses drain queued items first.
4. Settlement stops, reaps, and reconciles the child and every owned task
   exactly once, including repeated cancellation.
5. Registration of one consumer per stream is unchanged: the byte tap composes
   into the existing single `on_chunk` slot and cannot reorder capture or echo.

Methods:

- Stateful and property tests for the queue (bound, splitting, losslessness,
  release, failure precedence).
- Named unit tests for drain EOF-hook conditions and sink order.
- Barrier-driven real-child tests, under an external watchdog, for exchange,
  EOF, cancellation, and cleanup.
- Public API and `ty` diagnostics tests for the new surface.
- A gated documentation example that reconstructs bytes and observes stdout
  EOF.

Non-vacuity:

- The queue tests exercise both a chunk exactly at the bound and a chunk larger
  than the bound, and assert that consumed bytes equal posted bytes.
- Real-child tests use marker files or stdin tokens rather than only timing,
  and assert reconstructed bytes, not read sizes.
- The negative public-API tests assert specific `ty` diagnostic codes.

## Milestones

### EP-M1 — vocabulary, ownership decision, and design

Create ADR-0031, design §13.11, `cuprum/byte_streams.py` (`ByteChunk`,
`StreamClosure`), `LiveStreamError`, and the `docs/contents.md` link.
Acceptance: `make markdownlint`, `make nixie`, and import smoke checks.

### EP-M2 — drain hook and lossless queue

Add `on_eof` to `_drain`/`_drain_chunks`, compose the byte tap before the line
feeder, add `cuprum/_byte_stream/byte_queue.py`, and test both. Acceptance: the
new unit tests plus the existing stream tests pass.

### EP-M3 — readers and owners

Add `ByteStreamReader`, live wiring through `_subprocess_streams`,
`start_bytes()`, `RunningBytesCommand`, the bytes settlement strategy, the
eager supervisor, and exports. Acceptance: focused tests, `make typecheck`, and
the retained text-handle regression tests pass.

### EP-M4 — real-child proof

Add the scripted child, the supervised-scenario helper, exchange and lifecycle
tests, backend parametrization, POSIX group cases, and the behaviour feature.
Acceptance: the new tests pass under the suite timeout, with no leaked tasks or
processes.

### EP-M5 — documentation and delivery

Add the users' guide section and gated example, the changelog entry, the
`Unreleased` roadmap item, and the post-beta2 migration note. Acceptance:
`make markdownlint`, `make nixie`, and the documentation-example test pass.

## Revision note

2026-11-27: Initial draft created from the issue and the reconnaissance above.
The ADR number was corrected from the issue's stale 0029 to 0030. 2026-11-27:
Implementation milestones EP-M1 to EP-M5 recorded as complete. Decision log D6
and D7 and the surprises above record the final tap placement, the shared
handle base, and the double-delivery defect the drain test caught.
