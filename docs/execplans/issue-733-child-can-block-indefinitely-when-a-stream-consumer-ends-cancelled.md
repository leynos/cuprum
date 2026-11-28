# Stop a cancelled stream consumer from waiting on a blocked child

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS — implementation, documentation, and the first assessment
round are complete. Both CodeRabbit assessments returned `incomplete` on the
shared post-exit wait; the skipped-settled-consumer defect and the
already-settled attribution rule are repaired and covered by deterministic
regressions, and confirmation is pending on the repaired head.

## Purpose / big picture

A single-command run in cuprum waits for two things at once: the child's exit
and the tasks that feed or drain its pipes. Issue #551 repaired one way that
wait could hang forever: a stream consumer that *fails* is now an early
outcome, so the child is terminated and reaped rather than left blocking on a
pipe nobody reads.

A consumer that ends **cancelled** takes a different path. `run()` or `lines()`
keeps waiting for a child exit that cannot arrive, because the child keeps
writing into an undrained pipe. With no `timeout=` configured, the run waits
indefinitely. The same is true of a consumer task that is cancelled externally
while the run is still live, and of a drain that stopped short of end-of-file
but finalized a partial capture.

After this change, an output consumer that ends cancelled — whether a callback
raised `asyncio.CancelledError` or something cancelled the consumer task — ends
the run promptly with a public `ConsumerCancelledError` naming the stream. The
error is deliberately not an `asyncio.CancelledError`, so it cannot be mistaken
for the caller's own cancellation or silently consumed by `asyncio.timeout`.
The child is terminated and reaped on both entry points, and a caller
cancelling the run still receives `CancelledError`.

## Constraints

- The new `ConsumerCancelledError` is an ordinary `Exception` with a `stream`
  attribute of `"stdout"` or `"stderr"`. It must not subclass
  `asyncio.CancelledError` or `TimeoutError`.
- Writer semantics are unchanged. A cancelled stdin writer remains a quiet
  teardown; only stream consumers gain the new classification.
- Precedence is unchanged: a settled exit wins, then the stdin writer, then
  consumers in stdout-then-stderr order. Consumers are not re-read after the
  exit wait is cancelled.
- A caller cancelling the run still receives `CancelledError`, and the exit
  wait is still cancelled so the child is terminated.
- Pipelines and owned process handles keep their existing behaviour. Their
  readers are still reconciled at finalization; only the single-command
  supervisors classify consumer cancellation early.
- The public `ExecPhase` literal is not extended.
- en-GB-oxendict spelling throughout; no file exceeds the repository's
  400-line PyLint ceiling.
- All changes are committed only after the repository's formatting, lint,
  typecheck, test, Markdown, spelling, and Mermaid gates pass.

## Tolerances (exception triggers)

- Stop and record a decision if the change needs more than 14 non-ExecPlan
  files or more than 900 net code lines. The starting estimate is six
  production modules, one new leaf module, two export surfaces, four test
  modules, one new public-API test module, and four documentation files.
- Stop if the public error needs a hierarchy, a base class, or a broader
  taxonomy to be coherent.
- Stop if classifying a cancelled consumer would require changing pipeline or
  process-handle contracts, or extending `ExecPhase`.
- Stop if a real-process test cannot be bounded deterministically by the
  existing watchdogs and cleanup helpers.

## Risks

- **Cancellation is overloaded.** A consumer can end cancelled because a
  callback raised, because the run is being torn down, or because the caller
  cancelled the run. The classification must only fire while the supervisor is
  still live; a cancellation of the supervisor itself must stay a
  `CancelledError`.
- **A successful short drain is easy to miss.** A preserving capture returns a
  `_CapturedDrain` without raising, so task state alone cannot detect every
  short stop. The new `reached_eof` marker is the discriminator.
- **Ordering attribution.** `asyncio.wait` reports a set, so completion order
  must be recorded with done callbacks, as the existing reader wait already
  does.
- **Post-exit waits can hang on a sibling.** Waiting for every reader before
  acting on a cancelled one would spend the whole deadline on a wedged sibling.
  The loop must classify each completion as it arrives.
- **Test flakiness.** Real-process tests use endless-writing children; every
  case needs a watchdog, a started-PID assertion, reaping evidence, and
  `_abandon` cleanup.

## Context and orientation

The single-command supervisors are:

- `cuprum/_subprocess_rendezvous.py` — races the child's exit against the
  stdin writer and the stream consumers. Today `_stdin_writer_failure` returns
  `None` for a cancelled task, and `_consumer_failure` delegates to it.
- `cuprum/_subprocess_stream_run.py` — `run()`'s post-exit collection.
  `_collect_stream_output` gathers the consumers with `asyncio.gather`.
- `cuprum/_line_stream/reader_wait.py` — `lines()`'s post-exit reader wait,
  which uses `FIRST_EXCEPTION` and skips a cancelled reader.
- `cuprum/_line_stream/drain.py` — the deadline scope and publication path
  that consumes the reader wait.

The drain result type is `_CapturedDrain` in `cuprum/_stream_drain_finish.py`;
its cancellation-preserving path currently returns a capture indistinguishable
from an EOF drain. The line consumer's own preserving finalize is in
`cuprum/_stream_line_consumer.py`.

Public exports live in `cuprum/sh/__init__.py` and `cuprum/__init__.py`.
`cuprum/_process_handle_errors.py` is the precedent for a dependency-light leaf
module re-exported by both.

Documentation lives in:

- `docs/adrs/adr-0007-subprocess-execution-module-boundaries.md` (the #551
  addendum, and the later addenda that must stay intact);
- `docs/users-guide.md` (callback-failure behaviour and the troubleshooting
  table);
- `docs/developers-guide.md` (the rendezvous and teardown narrative);
- `docs/cuprum-design.md` (the rendezvous module bullet).

## Conformance basis

- Issue [#733](https://github.com/leynos/cuprum/issues/733), including the
  CodeRabbit finding `cr-comment:v1:c0277a0317b3db330668fe72` on #717.
- [ADR-007](adrs/adr-0007-subprocess-execution-module-boundaries.md) addendum
  of 2026-11-15 (#551), which established the early-failure route and the fixed
  precedence.
- [ADR-007](adrs/adr-0007-subprocess-execution-module-boundaries.md) addendum
  of 2026-11-19, which established the teardown reporting channels.
- `docs/developers-guide.md`, "Subprocess execution module boundaries".
- ExecPlan trace: `#733` → EP-M1 (public error and EOF marker) → EP-M2
  (supervisor classification) → EP-M3 (regression coverage) → EP-M4
  (documentation and gates).

## Milestones

### EP-M1 — Public error and drain EOF marker

Create `cuprum/_stream_consumer_errors.py` as a dependency-light leaf defining
`ConsumerCancelledError(Exception)`. Its message names the stream and says
`consumer ended cancelled before end of stream`. Its `stream` annotation must
resolve at runtime and be structurally equal to `cuprum.lines.LineStreamName`.
Re-export the same class object from `cuprum.sh` and `cuprum`, and add it to
both `__all__` lists.

Add `reached_eof: bool = True` to `_CapturedDrain`, set to `False` by
`_finish_drain`'s preserving cancellation path and by
`_stream_line_consumer._finalize_cancelled_capture`. Pipelines and process
handles ignore the field.

Acceptance: unit tests prove the export identity, the exception inheritance and
non-cancellation contract, the stream attribute and annotation, and the
`reached_eof` marker on both drain paths.

### EP-M2 — Classify consumer cancellation in the supervisors

In the rendezvous, add one shared consumer-outcome query. A cancelled task
yields `ConsumerCancelledError(stream)` with the cancelled task's
`CancelledError` as cause when it can be retrieved; a successful
`_CapturedDrain` whose `reached_eof` is false yields the same error without a
cause; an ordinary exception is returned unchanged; a pending task or an
EOF-success yields nothing. `_consumer_failure` must stop delegating to
`_stdin_writer_failure`, and the writer's own semantics stay unchanged.

In `run()`'s post-exit collection, replace the consumer `gather` with a
`FIRST_COMPLETED` loop inside the existing deadline scope. Record completion
order, classify each completion, raise the first failure through the existing
ordinary-failure handler, and decode results in stdout-then-stderr order after
success.

In `lines()`'s reader wait, replace `FIRST_EXCEPTION` with a `FIRST_COMPLETED`
loop under one remaining-deadline budget, recording and classifying completions
in order. Return the first failure rather than raising it inside the deadline
scope, and never let a final gather raise an unrequested reader
`CancelledError`.

Acceptance: the existing rendezvous, fail-fast, reader-failure, telemetry,
capture-timeout, and paused-lines tests pass, with the new classifications
covered by EP-M3.

### EP-M3 — Regression coverage

Extend the rendezvous unit tests, the precedence property test, the real-child
fail-fast matrix, and the line-stream reader-failure tests. Cover cancelled
consumers on either stream, short-stopped drains, stderr-only cases, cancelled
writers remaining quiet, caller cancellation still raising `CancelledError`,
prompt termination without `timeout=`, exactly one started child, and reaping
before the error reaches the caller. Add a public-API test module.

Acceptance: every new test fails for the intended reason before EP-M2's
production change and passes after it; all real-process cases are bounded by
the existing watchdogs and cleanup helpers.

### EP-M4 — Documentation and gates

Add an ADR-007 addendum dated 2026-11-27 describing the public error, the
early-failure route for independently cancelled and short-stopped consumers,
the shared route for callback-raised and externally requested cancellation,
caller and deadline precedence, unchanged writer cancellation, the exclusion of
pipelines and process handles, and the unchanged `ExecPhase`. Update the users
guide's callback-failure material and troubleshooting table, the developers
guide's rendezvous and cleanup narrative, the design document's rendezvous
bullet, and the changelog's Unreleased section.

Acceptance: `make check-fmt`, `make lint`, `make typecheck`, `make test`,
`make markdownlint`, and `make nixie` pass sequentially on the final head.

## Progress

- [x] Reproduced the hang on the starting tree with a scratch child that
  writes forever and a callback that raises `asyncio.CancelledError`
  (`outcome: HUNG past the watchdog (5.0s)`).
- [x] EP-M1: public error, exports, and `reached_eof` marker. The public
  error is defined in `cuprum/_stream_consumer_errors.py`, re-exported from
  both roots, and the drain marker is set by both preserving cancellation
  paths. The focused tests pass, and the affected drain, pipeline-payload,
  timeout-capture, public-API, and line-stream-exit suites pass.
- [x] EP-M2: supervise cancelled and short-stopped consumers early. The
  rendezvous classifies a cancelled task and a partial `_CapturedDrain` through
  one shared `_consumer_outcome`, and one shared `_await_consumer_outcomes`
  wait serves both post-exit supervisors. The reproducer now ends in 0.0 s with
  `ConsumerCancelledError`.
- [x] EP-M3: regression and public-API coverage. The rendezvous, precedence
  property, real-child fail-fast matrix, reader-failure, telemetry, drain, and
  public-surface tests cover cancelled callbacks, cancelled readers, short
  drains, writer cancellation, caller cancellation, and reaping. The affected
  suites pass.
- [x] EP-M4: documentation, changelog, and full gates. The ADR addendum, users
  guide, developers guide, design document, and changelog entry are written.
  All six gates pass on `65d64c0b4`, hosted CI is green, and CodeScene passes
  locally and hosted.
- [x] Assessment repair: both CodeRabbit assessments found that the post-exit
  wait could drop a settled consumer before classifying it, and the plan
  assessment also asked for a documented rule for consumers already settled at
  wait start. The wait now classifies every settled consumer before removal,
  records stream order as the tie-break for already-settled consumers, and is
  covered by deterministic unit and entry-point regressions in
  `cuprum/unittests/test_post_exit_consumer_classification.py`.
- [x] Gate record on the repaired head `512b20a12`: all six gates pass
  (check-fmt, lint, typecheck, test, markdownlint, nixie). The first test run
  hit three load-induced timeouts in unrelated real-process tests; each passed
  in isolation and the test gate rerun exited 0. Hosted CI run 46083744425 is
  green and CodeScene passes. The later test-strengthening and plan-record
  commit is covered by the final gate run recorded in the delivery ledger.
- [x] First confirmation: both assessments reported the defects resolved on
  `512b20a12`, and the plan assessment asked for the ordering test to settle
  stderr first and for this plan's gate record to name the repaired head. Both
  follow-ups are applied; the final confirmation covers the resulting head.
- [ ] Final assessment confirmation and the formal review.
- [ ] Draft PR opened with `Closes #733` and the Lody session reference.

## Surprises & discoveries

- The reproducer hangs in 5.0 seconds on the starting tree, confirming the
  issue independently of #717.
- `cuprum/lines.py` is a leaf only in the sense that it imports stdlib; a new
  private module cannot import it without the package `__init__`, so the stream
  literal is declared locally and tested for structural equality with
  `LineStreamName`.
- A PEP 695 `type` alias is not equal to another alias over the same literal,
  and `typing.get_type_hints` returns the alias object itself rather than a
  flattened literal. The public-surface test therefore compares the aliases'
  `__value__` attributes.

## Decision log

- **A distinct public exception, not a re-raised `CancelledError`.** Re-raising
  the consumer's own cancellation would be indistinguishable from the caller's
  cancellation and could be swallowed by `except CancelledError` or misread by
  `asyncio.timeout`. `ConsumerCancelledError` is an ordinary `Exception` with a
  `stream` attribute.
- **The same route for callback-raised and externally requested cancellation.**
  The task state cannot distinguish them, and both leave the pipe undrained.
  The supervisor's own cancellation is kept separate because it is observed by
  the rendezvous before the consumer outcomes are consulted.
- **A `reached_eof` marker rather than a second result type.** A preserving
  drain returns a capture even when it stopped short; one boolean on the
  existing record is the smallest discriminator and keeps pipeline callers
  source-compatible.
- **Pipelines and process handles are out of scope.** They reconcile readers
  only after every stage or the process exits; classifying cancellation there
  would change their contract rather than repair this hang.
- **One shared post-exit wait.** The streamed run and the line stream need the
  same wait: completion-ordered classification, stream labels, and prompt
  return on the first failure. Only the budget differs, so
  `_await_consumer_outcomes` takes an optional budget instead of both modules
  carrying a near-identical loop. This also keeps the two supervisors from
  drifting on the cancellation contract.
- **A done-callback barrier.** `asyncio.wait` reports an unordered set, so
  completion order is recorded through done callbacks. The wait yields once
  after `asyncio.wait` returns, which makes the callback ordering explicit
  rather than an implementation detail.
- **A second production module.** The approved coding plan's surface, plus the
  unchanged race, pushed `_subprocess_rendezvous.py` past the 400-line Pylint
  ceiling. The writer publication, consumer classification, and shared
  post-exit wait moved to `cuprum/_subprocess_task_outcomes.py`; the rendezvous
  keeps the race, the writer's second read, and the re-exports its callers
  already use. The initial 14-file tolerance undercounted the plan's own listed
  surface (two new production modules, two export surfaces, five test modules,
  and four documents), so the delivered file count is planned scope rather than
  growth.
- **Two duplication exceptions widened, one family avoided.** The writer-task
  `None` guard moved with its task into the new module, and the
  consumer-cancellation initializer joined the existing exception-constructor
  family. The duplicated pending-task guard inside the new module was
  refactored away rather than allowlisted.

## Verification plan

| Obligation                                                       | Method                                                                 | Artefact                                                        | Evidence                                                                            |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| A consumer that ends cancelled ends `run()` without `timeout=`   | Real-process regression, 2 entry points × 2 streams × 2 callback kinds | `cuprum/unittests/test_consumer_failure_fail_fast.py`           | Run ends under the 10 s watchdog, raises `ConsumerCancelledError`, no pending tasks |
| A consumer that ends cancelled ends `lines()` without `timeout=` | Real-process regression and reader-wait unit cases                     | Same module; `test_line_stream_reader_failure.py`               | Iterator raises `ConsumerCancelledError`; `aclose()` adds no `CancelledError`       |
| The error names the stream and is not a cancellation             | Unit assertions on type, `isinstance`, and `.stream`                   | `test_subprocess_rendezvous.py`, new public-API module          | `isinstance(error, asyncio.CancelledError)` is false; stream matches                |
| A caller's cancellation still raises `CancelledError`            | Gated race test with `ExitProbe`; real-process cancellation case       | `test_rendezvous_precedence_property.py`, fail-fast module      | `CancelledError` propagates; child reaped                                           |
| Precedence is exit, writer, stdout, stderr                       | Property test over generated slot states                               | `test_rendezvous_precedence_property.py`                        | Oracle agrees with race and pure selection                                          |
| A short-stopped preserving drain is not quiet                    | Unit test of `_finish_drain` and the line consumer                     | `test_stream_drain.py`, `test_stream_drain_cancelled_absorb.py` | `reached_eof` false on cancellation, true at EOF                                    |
| Telemetry still reports one settled error                        | Existing logger/event assertions extended to a cancelled callback      | `test_consumer_failure_telemetry.py`                            | One `settled` event with `TerminalOutcome.ERROR`                                    |
| Pipelines and handles are unchanged                              | Existing suites                                                        | `make test`                                                     | No behavioural change in those routes                                               |

Axioms: asyncio delivers a coroutine-raised `CancelledError` as a cancelled
task; `Task.exception()` raises `CancelledError` for a cancelled task;
`asyncio.wait` reports sets without order, so done callbacks supply completion
order. The real-process cases treat CPython's interpreter startup as bounded by
the existing 10 s watchdog.

## Outcomes & retrospective

The original hang is repaired: a callback that ends cancelled, or a consumer
task cancelled while the run is live, now ends `run()` and `lines()` promptly
with a stream-labelled `ConsumerCancelledError`, and a caller's own
cancellation still arrives as `CancelledError`. The public error, the
`reached_eof` marker, the two supervisor paths, and the documentation are in
place, and the full gate suite and hosted CI pass on `65d64c0b4`.

The assessment round exposed a second-order defect worth recording: a done
callback is not a barrier across event-loop turns. A consumer can be settled
while its callback is still queued, so a wait that records completion order
through callbacks and then removes every `done()` task can drop an outcome
unclassified. The wait now classifies every settled consumer before removal,
and it states stream order as the deterministic tie-break when several
consumers were already settled before it began.

## Revision notes

- Initial draft: records the reproduced defect, the four milestones, and the
  verification obligations.
- Assessment repair: records the skipped-settled-consumer defect, the
  already-settled tie-break, the new regression module, and the pending
  confirmation on the repaired head.
