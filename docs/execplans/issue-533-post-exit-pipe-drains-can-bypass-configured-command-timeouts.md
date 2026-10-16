# Carry one deadline through every post-exit pipe drain

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT — reconnaissance and defect localisation complete; no production
code has been changed yet.

## Purpose / big picture

A subprocess run in cuprum has two halves. The first waits for the child to
exit. The second tidies up: it stops the idle heartbeat, reconciles the stdin
writer, and collects whatever the stdout and stderr readers produced. The
configured `timeout=` has always bounded the first half. It bounds only part of
the second.

That gap is observable. A child may exit while a *descendant* it left behind
still holds the run's inherited pipes — an ordinary shell wrapper, a daemonized
helper, anything that starts a background job without redirecting its output.
End-of-file then never arrives on those pipes, and the run's readers never
finish. Because the cleanup that would signal the descendant is itself what the
run is waiting on, the run hangs indefinitely: the timeout does not fire, and
neither does the group teardown.

After this change a caller of `SafeCmd.run()`, `SafeCmd.lines()`, or
`Pipeline.run()` gets a run that ends. Either it completes, or it raises
`cuprum.sh.TimeoutExpired` at the configured deadline, having preserved whatever
output the run had already read. The overshoot past the deadline is bounded by
the existing cleanup allowances and nothing else: the teardown grace
(`cancel_grace`), the capture end-of-file grace (`_CAPTURE_EOF_GRACE_S`, 0.25 s)
and the stdin close grace (`_CLOSE_GRACE_S`, 0.5 s).

This plan fixes all three public routes. It is written so that someone with only
this file, the working tree, and no memory of the investigation can finish the
job.

## Conformance basis

Upstream artefacts and their revisions, as read on branch
`issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts` at the
time of writing:

- Issue: `leynos/cuprum#533`, "Post-exit pipe drains can bypass configured
  command timeouts". Filed against revision `ebd6a41e4435`; severity medium,
  confidence high. `ebd6a41e4435` is an ancestor of the current `main`
  (`c38459100`), with 51 commits between.
- `docs/adr-021-process-group-ownership.md` — process group ownership and
  descendant cleanup. This plan is the deadline half of the same story and
  reuses the reconciliation points ADR 021 established.
- `docs/cuprum-design.md` — the streamed-run and pipeline designs. Sections
  cited for update in milestone EP-M4 are at lines 1770–1782 (post-exit
  boundary), 1937–1973 (timeout translation) and 3043–3053 (pipeline
  collection).
- `docs/users-guide.md` — the caller-visible timeout contract, at lines
  806–810, 1270–1299 and 1349–1355.
- `AGENTS.md` — repository conventions: en-GB-oxendict prose, 400-line module
  cap, NumPy-style public docstrings, Red-Green-Refactor for behaviour changes.

Trace links used below: `ISS533` (the issue), `ADR021`, `EP-M1`…`EP-M4` (the
milestones), and the acceptance evidence named in each milestone.

Two source references from the issue no longer match the working tree, because
neither `_deadline_for_collection` nor the streamed-run boundary existed when
the issue was filed:

- The issue's root-control site, `cuprum/_subprocess_wait.py:324-326` at
  `ebd6a41e4435`, is now `_wait_for_exit_code_within_timeout` plus the
  `_deadline_for_collection` helper at `cuprum/_subprocess_wait.py:209-243`.
- The issue's sink, `cuprum/_subprocess_stream_run.py:187` at `ebd6a41e4435`,
  is now `_run_subprocess_without_streams` at
  `cuprum/_subprocess_stream_run.py:161-194`, whose bare `await stdin_task` is
  the unbounded await in question.

## Constraints

- One absolute deadline, not one duration per step. The remaining budget is
  derived from `execution.started_at + execution.timeout` by
  `_deadline_for_collection(execution)` and read immediately before the scope
  it bounds. A duration computed earlier would let the first await spend part
  of the budget and still hand the whole of it to the next.
- `_deadline_for_collection` keeps its current behaviour: `None` for an
  unconfigured timeout (so a run without `timeout=` stays unbounded), a
  possibly negative remainder when the exit wait already overran, and its clock
  read through `execution.observation.monotonic_clock()` rather than
  `loop.time()`, so a test can hold the deadline still.
- Expiry continues to raise the public `cuprum.sh.TimeoutExpired` with the
  *configured* timeout and with the run's already-read output preserved. No new
  exception type, no new keyword argument, no change to the existing public
  signatures.
- An inherited process group is never signalled. Under
  `ProcessGroupPolicy.INHERIT` only a leader that has not yet exited may be
  signalled, exactly as `cuprum/_teardown_policy.py` already states.
- Cleanup that must not be abandoned runs under `_shielded_cleanup`, and
  sequential cleanup steps compose into *one* shielded coroutine — two
  sequential shielded calls are not one shielded sequence.
- Consume fallible fixtures; keep `expect_used` strict. New tests raise through
  `Result`/`?`-style helpers rather than `assert`-and-continue where the
  repository's lint policy requires it.
- POSIX-only tests are marked, not silently skipped. The Windows CI leg must
  stay green.
- Every new file stays under 400 lines, keeps en-GB-oxendict spelling, and
  carries a module docstring explaining why the module exists.
- No new runtime dependency, and no change to the Rust extension. This is a
  Python-orchestration change.
- Every new test stays under the 30-second per-test bound configured in
  `pyproject.toml`.

## Tolerances (exception triggers)

- Scope: stop and escalate if a fourth production module must change beyond
  `cuprum/_subprocess_stream_run.py`, `cuprum/_line_stream/`,
  `cuprum/_pipeline_collect.py` and `cuprum/_pipeline_stream_results.py`. The
  four-file shape is what the defect map supports; a fifth site means the
  defect is broader than measured and the plan needs re-cutting.
- Interface: stop and escalate if any public signature, exception type, or
  result field must change. In particular, if a route can only be bounded by
  changing `RunOutputOptions`, `ExecutionContext`, or `PipelineResult`, that is
  a design change and not a fix.
- Flakiness: stop and escalate if a new test fails intermittently. A timing
  test that flakes is worse than no test, because it trains the reader to
  re-run. Three consecutive clean local runs of each new module is the bar
  before the module is committed.
- Duration: stop and escalate if any single new test exceeds 10 seconds on this
  machine, or if a module of new tests exceeds 90 seconds in total. The suite
  bound is 30 seconds per test; a test that needs half of it is hiding a wait
  that should be structural.
- Iterations: stop and escalate if a route still hangs past its deadline after
  three distinct fix attempts. The three routes fail for different reasons, and
  a third guess at one of them is evidence the mechanism is not yet understood.
- Environment: stop and escalate if `/tmp` or the workspace filesystem runs
  short, rather than deleting anything to make room.
- Deviation: stop and escalate if the approved design cannot be followed — for
  example if bounding a consumer gather turns out to require cancelling a
  consumer whose partial output the public contract promises to preserve.

## Risks

- Risk: bounding a consumer gather by cancelling it *discards* the partial
  output the deadline exists to preserve, turning a hang into silent data loss.
  Severity: high. Likelihood: medium. Mitigation: every expiry path reconciles
  through the existing `_drain_stream_consumers` with
  `discard_on_cancel` *unset*, which is the mode that returns the partial
  buffer; discard mode is used only on routes that are re-raising some other
  error. Milestone EP-M1's second test asserts the payload directly, and EP-M2
  and EP-M3 repeat the assertion on their routes.

- Risk: the new tests pass vacuously. A `TimeoutExpired`-shaped test can be
  satisfied by a run that expired during the *exit wait* rather than during the
  post-exit phase, proving nothing about the code under change. Severity: high.
  Likelihood: high (this is the default outcome of a carelessly written test).
  Mitigation: every test uses `descendant_then_exit_argv`, whose child exits
  promptly and successfully, so the exit wait cannot be what expired; and the
  discriminating arms are the `INHERIT` ones, where no group signal can close
  the pipes and only the new bound can end the run. Each test is checked
  against a mutation that removes the bound and must fail.

- Risk: `cuprum.sh.TimeoutExpired` subclasses the builtin `TimeoutError`. An
  outer harness arm written as `except asyncio.TimeoutError` placed before
  `except TimeoutExpired` silently swallows a *successful* deadline expiry and
  reports it as a hang. Severity: high. Likelihood: high — this was observed
  during reconnaissance and produced a wholly false result table. Mitigation:
  the project's own helpers `run_to_its_own_deadline` and `settle` assert on
  `TimeoutExpired` directly; tests use those rather than hand-rolling an outer
  bound, and any hand-rolled arm orders `TimeoutExpired` first with a comment
  saying why.

- Risk: a `lines()` case deadlocks on **caller** backpressure rather than on the
  bug, because the line buffer fills while nobody is consuming from `LineStream`
  and the run's own cleanup then blocks writing a terminal event. Severity:
  medium. Likelihood: medium. Mitigation: EP-M2's tests drain the stream in a
  dedicated reader task, and the plan records the requirement in the users'
  guide: `lines()` backpressure counts towards the deadline.

- Risk: the new tests leak grandchildren, leaving SIGTERM-immune processes on a
  shared machine where other agents are working. Severity: medium. Likelihood:
  medium. Mitigation: every test reaps in `finally` through
  `reap_descendant`, which escalates to `SIGKILL` — the only signal the
  descendant honours.

- Risk: bounding the pipeline's final gather makes an unrelated failure
  *reported* as a timeout, because `TimeoutError` is also what a bounded wait
  raises for reasons other than the run's own deadline. Severity: medium.
  Likelihood: medium. Mitigation: each bounded wait reports expiry through an
  explicit flag on its result rather than by catching `TimeoutError`, and the
  existing `except TimeoutExpired:`-first ordering is preserved. EP-M3's
  handler is shared with the wait-timeout route precisely so the two cannot
  drift.

- Risk: the module docstrings under `cuprum/_subprocess_stream_run.py` and
  `cuprum/_line_stream/` are long and precise, and an edit that keeps the code
  correct can still leave the prose describing the old behaviour. Severity:
  low. Likelihood: high. Mitigation: each milestone's Definition of Done
  includes reading the module docstring against the new code, and EP-M4
  reviews them again as part of the design-document pass.

## Progress

- [x] (2026-10-16 14:00Z) Branch renamed to
  `issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts` and
  the Lody session titled. Working tree clean at `c38459100`.
- [x] (2026-10-16 14:05Z) Confirmed `ebd6a41e4435` (the issue's revision) is an
  ancestor of `HEAD`; `_deadline_for_collection` arrived later, in `8c78a5b77`.
- [x] (2026-10-16 14:40Z) Built a scratch probe harness under `/tmp/probe533/`
  and ran twelve routes against `HEAD`. Result table in `Surprises &
  Discoveries`. Probes are scratch only and are never committed.
- [x] (2026-10-16 15:23Z) Wrote this plan.
- [ ] (2026-10-16 15:23Z) Submit this plan for approval before implementation.

## Surprises & discoveries

- **The defect is wider than the issue text, and narrower, than it first
  looked.** The issue names three routes. Probing all three plus their variants
  showed that two streamed `run()` variants and one pipeline variant were fixed
  by earlier work; the live failures are the *direct* `run()` stdin await, both
  `lines()` modes, and the pipeline's final output gather. Six of twelve probed
  routes hang.

  Measurement conditions: `HEAD` = `c38459100`; a child that spawns a
  SIGTERM-immune grandchild inheriting the run's pipes, waits for its pid file,
  prints a marker, and exits 0; run `timeout` 1.0 s; an outer harness bound of
  12 s, counted as a hang if the run had not ended.

  | # | Route | Configuration | Verdict |
  | - | ----- | ------------- | ------- |
  | 1 | `run()` streamed | capture, OWN_GROUP | bounded, expired at 1.54 s |
  | 2 | `run()` streamed | capture, 2 MiB stdin, OWN_GROUP | bounded, 1.54 s |
  | 3 | `run()` direct | no capture, 2 MiB stdin, INHERIT | hangs |
  | 4 | `run()` direct | no capture, 2 MiB stdin, OWN_GROUP | hangs |
  | 5 | `run()` direct | no capture, no stdin | fine, 0.07 s |
  | 6 | `lines()` | capture, OWN_GROUP | hangs |
  | 7 | `lines()` | no capture, OWN_GROUP | hangs |
  | 8 | `Pipeline.run()` | success, capture, OWN_GROUP | hangs |
  | 9 | `Pipeline.run()` | success, no capture, OWN_GROUP | fine, 0.07 s |
  | 10 | `Pipeline.run()` | success, non-final stage leaves descendant | hangs |
  | 11 | `Pipeline.run()` | wait timeout, OWN_GROUP | bounded, 1.58 s |
  | 12 | `Pipeline.run()` | wait timeout, INHERIT | hangs |

  Routes 1–2 are bounded because `_collect_stream_output` already wraps its
  whole post-exit boundary in `asyncio.timeout(_deadline_for_collection(...))`.
  Route 11 is bounded because the pipeline wait-timeout handler calls
  `_terminate_timed_out_stages` before gathering. Route 12 hangs because under
  `INHERIT` no signal reaches the *descendant* holding the pipes: the leader is
  already gone.

- **Route 4 hangs under `OWN_GROUP` too.** This was counter-intuitive — the
  group teardown exists to reclaim descendants. The reason is ordering: the
  direct path awaits the parked stdin writer *before* any settle happens, and
  the settle only runs when the writer await fails. There is nothing to fail it.
  The bound has to come first; the settle follows expiry.

- **Route 5 and route 9 are fine for the same reason, and it is not reassuring.**
  Both have literally nothing to await after exit — no stdin task, and (for a
  no-capture pipeline) no output tasks. They are bounded by vacuity, not by
  design, and are listed to show the probe distinguished them rather than to
  claim they are exercised.

- **A timeout route can appear bounded while the descendant survives.** Route 11
  returned in 1.58 s under `OWN_GROUP`, which looks like success until the pid
  file is read: the descendant is still alive because the group teardown did not
  reach it either. `Pipeline.run()` has no process-group policy of its own, so
  this is a structural limitation rather than a regression, but the plan records
  it so the new pipeline tests do not assert a reclamation that cannot happen.

## Decision log

- Decision: bound each post-exit phase at the *route* level rather than by
  teaching the drain, the consumers, or the writer about deadlines. Rationale:
  the same deadline already exists and is already honoured by the streamed path;
  the defect is four sites that never ask for it. Threading a deadline into
  `_drain_stream_consumers` would give a second mechanism for the same
  invariant. Alternatives considered: a deadline parameter on every await site
  (rejected: duplicates the arithmetic and drifts), and a watchdog task that
  cancels the run (rejected: would change which exception the caller sees).
  Date: 2026-10-16.

- Decision: keep the expiry's existing public shape — `TimeoutExpired` with the
  *configured* timeout and the run's already-read output. Rationale: a streamed
  run already expires this way, and a caller cannot tell which route expired.
  Alternatives considered: raising a distinct post-exit exception (rejected: a
  new public type for a distinction no caller asked for), and reporting the
  bounded remainder as the timeout value (rejected: it would make
  `exc.timeout` mean two different things on two routes). Date: 2026-10-16.

- Decision: for `lines()`, wait on consumers with a bounded
  `asyncio.wait` and then reconcile, rather than wrapping the consumer gather in
  `asyncio.timeout`. Rationale: a cancelled line consumer can be holding a
  trailing line it has not published, and the repository's line-stream contract
  is that a delivered line stays delivered. `asyncio.wait` observes; the
  existing reconciliation performs the cancellation, and it is the code that
  already knows how to do so without losing a trailing line. Date: 2026-10-16.

- Decision: do not signal an inherited group on a pipeline deadline, and do not
  attempt to. Rationale: `Pipeline.run()` has no process-group policy; inventing
  one here would be a design change (see `Tolerances`). The plan records the
  gap so the pipeline tests assert a bounded *return* rather than a reclaimed
  descendant. Date: 2026-10-16.

- Decision: one new test module per route —
  `test_run_no_stream_post_exit_deadline.py` for the direct run,
  `test_safe_cmd_lines_post_exit_deadline.py` for line iteration, and
  `test_pipeline_post_exit_deadline.py` for the pipeline. Rationale: the three
  routes fail for different reasons and are fixed in three separate commits, so
  a failure names the route. The `OWN_GROUP` streamed path keeps its existing
  module, extended with the direct-path analogue. Date: 2026-10-16.

- Decision: use `execution.observation.monotonic_clock()` throughout, never
  `loop.time()`. Rationale: the remainder is only meaningful against the same
  clock `started_at` was sampled from, and the seam lets a test hold the
  deadline still instead of racing a wall clock. Date: 2026-10-16.

## Milestones

Each milestone ends in a state that is coherent on its own: the repository
builds, the gates are green, and the routes it covers are bounded.

### EP-M1 — the direct run's post-exit stdin await

Requirement: `ISS533`. Deliverable, in
`cuprum/_subprocess_stream_run.py::_run_subprocess_without_streams`:

1. Replace the bare `await stdin_task` with a shielded `_await_stdin_writer`
   inside `asyncio.timeout(_deadline_for_collection(execution))`. The shield
   matters for latency, not correctness: awaiting the writer task makes it the
   await's `_fut_waiter`, and `Task.cancel()` forwards into it, so without the
   shield the boundary wakes only once the writer has finished dying —
   including the 0.5 s close grace.
2. On expiry, cancel the writer through
   `_shielded_cleanup(_cancel_stdin_writer(stdin_task))`, then settle the owned
   group and reconcile with an *empty* consumer bundle, so the group teardown
   that releases the descendant runs before the exception propagates.
3. Preserve the existing behaviour on the cancellation and error routes: the
   same cleanup, then re-raise unchanged.
4. Report expiry by distinguishing the timeout scope's own `expired()` state
   from a cancellation, not by catching the exception type alone:
   `cuprum.sh.TimeoutExpired` subclasses the builtin `TimeoutError`, so a
   `TimeoutError` arm sees cancellations and expiries alike.
5. Route the resulting failure through the existing
   `_handle_stream_timeout()` translation with `None` payloads, so the caller
   receives the same public `TimeoutExpired` the streamed path produces.

Tests: new module `cuprum/unittests/test_run_no_stream_post_exit_deadline.py`,
built on `cuprum/unittests/_post_exit_teardown_support.py`, with a 2 MiB stdin
payload, capture, echo and observers all off, parametrized over
`INHERIT`/`OWN_GROUP` and over `run`/`run_bytes`/`run_sync`. Extend
`cuprum/unittests/test_stream_run_owned_group_deadline.py` with the direct-path
analogue of its stdin case. Assert the run ends, the raised exception is the
public `TimeoutExpired`, pending tasks are inspected through
`tests/helpers/timeouts.py::pending_tasks`, and any descendant is reaped in
`finally`.

Acceptance evidence: the new module fails before the change on the `INHERIT`
arm with a hang past the harness bound (Red), and passes after it (Green); the
`OWN_GROUP` arm asserts the descendant is gone.

### EP-M2 — `SafeCmd.lines()`'s post-exit drain

Requirement: `ISS533`. Deliverables, in `cuprum/_line_stream/drain.py` and the
post-exit branch of `cuprum/_line_stream/coordinator.py`:

1. Give `_drain_after_exit` the remaining `_deadline_for_collection(execution)`
   budget, covering three things under one bound: stopping the idle monitor,
   the shielded stdin await, and consumer completion.
2. Use `asyncio.wait(consumers, timeout=remaining)` for consumer completion
   rather than cancelling through `gather()` inside `asyncio.timeout`. A
   trailing line already sitting in a full queue must still be published, and
   `asyncio.wait` lets the existing reconciliation — which knows that rule —
   perform the cancellation.
3. On expiry: settle the owned group first, then reconcile the bundle in
   *capture* mode with `run.tasks.discard_on_cancel` set, and raise into the
   existing translation that publishes `TimeoutExpired` on `result_future`.
4. On cancellation, or any other post-exit error including `aclose()` after the
   child has exited: discard-mode reconciliation plus the same group
   settlement, shielded with `_shielded_cleanup`, cancelling stdin before the
   consumers.
5. Never signal an inherited group (per `cuprum/_teardown_policy.py` and
   `ADR021`).
6. Preserve already-delivered lines, and carry the reconciliation's output into
   the raised `TimeoutExpired.output` / `.stderr`, with `""` / `b""` fallbacks
   in the run's mode.
7. Keep the successful `queue.put(result)` sentinel outside the deadline and
   after task settlement, so a successful run is not reported as expired.

Tests: new module `cuprum/unittests/test_safe_cmd_lines_post_exit_deadline.py`,
draining the stream in a dedicated reader task so the case measures the run
rather than caller backpressure.

Acceptance evidence: the module hangs before the change on both `OWN_GROUP`
capturing and non-capturing modes and passes after it; the partial-output arm
asserts a line the child emitted before the deadline is present in the raised
exception.

### EP-M3 — `Pipeline.run()`'s final output and its `INHERIT` wait-timeout route

Requirement: `ISS533`. Deliverables:

1. In `cuprum/_pipeline_stream_results.py`, add a deadline-aware
   `asyncio.wait` over every stage-stderr task and the final stdout task. It
   takes an optional remaining budget, reports expiry through an explicit flag,
   preserves `None` positions so stage order survives, and **does not cancel**
   the tasks it observes.
2. Add bounded settlement through `_drain_stream_consumers`: capture mode gives
   the 0.25 s end-of-file grace and then cancels what is still unfinished,
   preserving prefixes with `""`/`b""` fallbacks; without capture the grace is
   skipped. Delegate `_gather_optional_text_tasks()` to the new helper, or
   retire it, so there is one gather path.
3. In `cuprum/_pipeline_collect.py`, pass the remaining `timeout_deadline`
   budget from `_collect_pipeline_inputs` and `_gather_pipeline_outputs`, and
   use the explicit expired flag rather than catching `TimeoutError`. Do not
   relabel unrelated errors. With no configured timeout, behaviour stays
   unbounded as it is today.
4. Share one timeout handler between the wait-timeout route and the
   final-output expiry so the two cannot drift: terminate and settle the stages
   first, including owned groups whose leaders have exited; reconcile the
   relays; run the bounded settlement; then build the `TimeoutExpired` with the
   existing builder and per-stage telemetry, including final stdout and stderr
   concatenated in stage order.
5. Shield the settlement with `_shielded_cleanup` and preserve the
   collector-level relay `finally` and the run-failure cleanup in
   `cuprum/_pipeline_finalize.py`.

The `INHERIT` wait-timeout route (probe route 12) is covered by the same
handler: the stage leader is terminated, but the descendant holding the pipes
is not reachable, so it is the bounded *settlement* that ends the run.

Tests: new module `cuprum/unittests/test_pipeline_post_exit_deadline.py`,
parametrized over async and sync runners, capture on and off, and
`OWN_GROUP`/`INHERIT`, across the available stream backends. It follows the
backend-selection pattern in
`tests/behaviour/test_stream_backend_pipeline.py:40-59`: skip a backend that is
unavailable, set `CUPRUM_STREAM_BACKEND` with `monkeypatch`, and reset the
caches around it.

Acceptance evidence: the module hangs before the change on the capturing
success route and on the `INHERIT` wait-timeout route, and passes after it;
assertions cover the raised exception's `timeout`, `output` and the stage
telemetry, plus a pending-task check.

### EP-M4 — documentation and full validation

Requirement: `ISS533`, repository documentation policy. Deliverables:

1. `docs/users-guide.md`: state, at the timeout contract (lines 806–810),
   the `lines()` guidance (1270–1299) and the pipeline section (1349–1355),
   that the deadline now covers post-exit stdin, stdout and stderr collection
   for `run()`, `lines()` and `Pipeline.run()`; that a run can exceed the
   deadline by at most `cancel_grace` plus the end-of-file and stdin close
   graces (0.25 s and 0.5 s); that a `lines()` caller whose consumption lags
   counts that backpressure towards the deadline; and that lines already
   delivered stay delivered.
2. `docs/cuprum-design.md`: the post-exit boundary (1770–1782), the timeout
   translation (1937–1973) and pipeline collection (3043–3053).
3. `docs/adr-021-process-group-ownership.md`: append (never rewrite) a note
   that the deadline is what makes the group settlement reachable when a leader
   exits early, or record in the `Decision log` why no amendment is needed.
4. Full validation: every new module, plus
   `test_timeout_capture_contract.py`, `test_capture_eof_grace_observability.py`,
   `test_stream_run_owned_group_deadline.py`,
   `test_stream_run_owned_group_post_exit.py`, `test_pipeline_timeouts.py`,
   `test_safe_cmd_lines.py` and `test_line_stream_terminal_outcome.py`; then
   `make test` and the remaining gates.

Acceptance evidence: `make test` green at the branch head, the named modules
green individually, and the documentation reviewed against the shipped
behaviour rather than the intended behaviour.

## Verification plan

Invariants the change introduces or preserves:

1. **I1 — the deadline is absolute.** For any run with `timeout = T`, the
   post-exit phase begins with at most `T - (now - started_at)` seconds of
   budget, measured at the moment the phase starts.
2. **I2 — expiry preserves payload.** A capturing route that expires during a
   post-exit await reports, in the raised `TimeoutExpired`, every byte or
   character its readers had already produced.
3. **I3 — expiry settles ownership.** A deadline expiry on any of the three
   routes leaves no task the run owns still pending, and settles an owned group
   whose leader has exited.
4. **I4 — cancellation is not expiry.** A caller cancelling the run is not
   reported as a timeout, and a run that completes successfully is never
   reported as expired.
5. **I5 — no timeout configured means no bound.** A run without `timeout=`
   behaves exactly as before.
6. **I6 — inherited groups are never signalled.** Under `INHERIT` the change
   introduces no signal directed at any process the run does not lead.
7. **I7 — delivered lines stay delivered.** A line already published to a
   `lines()` consumer is not withdrawn by a later expiry.
8. **I8 — non-vacuity.** Every discriminating test fails when the bound it
   exercises is removed.

Lemmas:

- **L1 (for I1):** `_deadline_for_collection` returns `None` exactly when
  `execution.timeout is None`, and otherwise returns
  `timeout - (monotonic_clock() - started_at)`, which the caller reads no
  earlier than the scope it bounds.
- **L2 (for I2):** `_drain_stream_consumers` with `capture=True` returns the
  partial buffer of a cancelled-but-not-at-end-of-file reader, and falls back
  to `""` or `b""` according to `capture_bytes`.
- **L3 (for I3):** `_reconcile_run_tasks` leaves no task from
  `_RunTaskOwnership` pending, and `_terminate_all_shielded` completes its
  teardown before the caller's cancellation is re-raised.
- **L4 (for I4):** the `asyncio.timeout` scope's `expired()` distinguishes its
  own expiry from an external cancellation, which the exception type alone
  cannot, since `cuprum.sh.TimeoutExpired` is a `TimeoutError`.
- **L5 (for I6):** `_teardown_policy` yields `owns_group=False` for every stage
  under `INHERIT`, and the new call sites pass the policy through unchanged.

Axioms (assumptions treated as given, with the evidence that exercises them):

- **A1:** `asyncio.timeout(None)` disables the bound rather than expiring
  immediately; `asyncio.wait(..., timeout=None)` waits forever.
- **A2:** the POSIX `fork`/`exec` semantics of `asyncio.create_subprocess_exec`
  give a grandchild the parent's inherited pipe file descriptors.
- **A3:** `SIGKILL` to a pid the test recorded is sufficient to reclaim that
  descendant on this host, and `_terminate_process` escalates to it within
  `cancel_grace`.
- **A4:** the Windows leg cannot exercise process groups; the POSIX-only marker
  is the repository's accepted acknowledgement of that gap
  (`ADR021`, `tests/helpers/timeouts.py`).

Methods, artefacts and discharge conditions:

- **I1, L1:** unit test over `_deadline_for_collection` with a held clock, plus
  the parametrized route tests' timing assertions. Discharged when an
  unfixed build exceeds the 10 s per-test tolerance and a fixed build does not.
- **I2, L2:** direct payload assertions on each route (EP-M1, EP-M2, EP-M3),
  each with a `stdout_marker` the child flushes before it spawns anything, so
  the payload is in the buffer well before the deadline. Discharged when
  replacing the capture-mode reconciliation with discard mode fails the
  assertion.
- **I3, L3:** `pending_tasks()` after each route returns, plus
  `assert_reclaimed` on descendants for the `OWN_GROUP` arms. Discharged when
  a route that stops waiting without reconciling leaves a pending task.
- **I4, L4:** a case that cancels the run and asserts the caller's
  `CancelledError` (not a timeout), and a case that completes inside the
  deadline and asserts success. Discharged when the `expired()` check is
  removed and the cancellation case misreports.
- **I5:** a case with no `timeout=` that completes normally against a
  descendant that does eventually exit.
- **I6, L5:** a unit assertion on `_teardown_policy`'s ownership for each
  policy, plus the `INHERIT` route arms, which would fail if an inherited
  process were signalled — the test's own descendant reaper is the only thing
  allowed to kill it.
- **I7:** the `lines()` module asserts a specific line delivered before the
  deadline is present after it.
- **I8:** a *negative control* per route: the bound is removed (or the
  remainder forced to `None`) on a scratch copy, the discriminating test is run
  and must hang or fail, and the scratch copy is discarded. The observed
  result is recorded in `Progress`.

Non-vacuity: the `INHERIT` arms are what make most of these tests real. Under
`OWN_GROUP` a group signal can close the pipes, so a run that bounds *only* the
final await can still pass; under `INHERIT` nothing closes them, so only the
new bound and the new settlement end the run. Each route therefore carries at
least one `INHERIT` arm, and the test docstrings say which arm is expected to
discriminate. Each child announces readiness through a pid file and a flushed
marker rather than the test sleeping, so no case can pass by racing start-up.

Residual gaps, recorded rather than hidden:

- A pipeline deadline cannot reclaim a descendant under `INHERIT`, and cannot
  reclaim one under `OWN_GROUP` either, because `Pipeline.run()` carries no
  process-group policy (`Surprises`, route 11). What is verified is that the
  *run* returns inside its deadline plus the bounded graces.
- The scenario suite cannot be exercised on a non-POSIX host; the POSIX-only
  marks are the same acknowledgement the existing post-exit tests make.

## Validation and acceptance

The exact commands, all from the repository root, each with the log file
convention `/tmp/<action>-cuprum-<branch>.out`:

```sh
uv run pytest cuprum/unittests/test_run_no_stream_post_exit_deadline.py -v
uv run pytest cuprum/unittests/test_safe_cmd_lines_post_exit_deadline.py -v
uv run pytest cuprum/unittests/test_pipeline_post_exit_deadline.py -v
make test
make lint
make check-fmt
```

Expected: each focused module reports a non-zero collected count, no skips on
this POSIX host, and no test over the 10 s tolerance; `make test` reports no
failures and no warnings; `make lint` and `make check-fmt` exit zero.

Gate runs are delegated to the `scrutineer` subagent, which runs them
sequentially and writes one log per gate under `/tmp`. A gate log certifies the
revision it recorded at its own head; after any further commit, the affected
gates must be re-run rather than the stale log cited.

## Outcomes & retrospective

To be completed as milestones land. At minimum it will record: which routes
were bounded, the measured overshoot past the deadline on each, whether the
`INHERIT` arms discriminated as predicted, whether any route needed a design
change, and what a future contributor should read first — the module docstring
of `cuprum/_subprocess_stream_run.py`, whose account of the streamed boundary is
the shape all three routes now follow.

## Appendix — how to reproduce the hang by hand

Scratch only; nothing here is committed. With the repository's own virtualenv
active and `cuprum` importable from the working tree:

1. Write a child that starts a grandchild which installs `SIG_IGN` for
   `SIGTERM`, writes its pid to a file, and sleeps for five minutes; the child
   then waits for that file, prints, and exits 0 without waiting for the
   grandchild. `tests/helpers/timeouts.py::descendant_then_exit_argv` builds
   exactly this and is the version to reuse.
2. Under `scoped(ScopeConfig(allowlist=...))`, run that command with
   `timeout=1.0` and a 2 MiB `StdinInput`, with
   `RunOutputOptions(capture=False, echo=False)`, under both group policies.
3. Bound the call from outside at 12 s and treat a bound that fires as a hang.
   **Order the exception arms with the project's `TimeoutExpired` first**: it
   subclasses the builtin `TimeoutError`, so an `except asyncio.TimeoutError`
   arm placed first reports a successful expiry as a hang.
4. Reap the grandchild by pid with `SIGKILL` in a `finally`. `SIGTERM` is
   ignored by design.
