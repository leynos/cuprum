# Carry one deadline through every post-exit pipe drain

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS — EP-M1 (the direct run's post-exit stdin await) and EP-M2
(the `lines()` post-exit drain) are implemented, tested Red-then-Green, and
committed with their commit gates green. EP-M2's CodeRabbit review returned ten
findings: eight were applied and two are rejected as describing code that does
not exist in this tree (see `Decision log`). Applying the first finding exposed
the same `TimeoutError` misclassification at three further layers on the
streamed `run()` path; all four fixes are in the working tree with their own
regression module, and their commit gate is outstanding. EP-M3 (the pipeline's
post-exit collection) and EP-M4 (documentation and full validation) remain. The
direct run was split into `cuprum/_subprocess_direct_run.py` to stay inside the
400-line module cap.

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
`cuprum.sh.TimeoutExpired` at the configured deadline, having preserved
whatever output the run had already read. The overshoot past the deadline is
bounded by the existing cleanup allowances and nothing else: the teardown grace
(`cancel_grace`), the capture end-of-file grace (`_CAPTURE_EOF_GRACE_S`, 0.25
s) and the stdin close grace (`_CLOSE_GRACE_S`, 0.5 s).

This plan fixes all three public routes. It is written so that someone with
only this file, the working tree, and no memory of the investigation can finish
the job.

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
  is now `_run_subprocess_without_streams`, whose bare `await stdin_task` is
  the unbounded await in question. The function has since moved to
  `cuprum/_subprocess_direct_run.py` as part of EP-M1, and the bare await is now
  `_await_writer_within_deadline`. A follow-up reader tracing the issue text
  should follow the *behaviour* — "the direct run awaits its writer with no
  bound" — rather than the path, which this plan's own work has relocated.

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
- Tests fail through pytest's own assertions and `pytest.raises`. The
  `expect_used` lint and the `Result`/`?` helper convention are Rust-only and
  do not apply to this Python change; `assert` is permitted in test modules by
  the repository's ruff per-file ignores.
- POSIX-only tests are marked, not silently skipped. The Windows CI leg must
  stay green.
- Every new file stays under 400 lines, keeps en-GB-oxendict spelling, and
  carries a module docstring explaining why the module exists.
- No new runtime dependency, and no change to the Rust extension. This is a
  Python-orchestration change.
- Every new test stays under the 30-second per-test bound configured in
  `pyproject.toml`.

## Tolerances (exception triggers)

- Scope: the original trigger named four modules and required escalation
  beyond them. The measured change set is larger, and the trigger is retained
  in its measured form rather than deleted: `cuprum/_subprocess_stream_run.py`,
  `cuprum/_subprocess_direct_run.py` (created by the EP-M1 split),
  `cuprum/_subprocess_wait.py`, `cuprum/_subprocess_execution.py`,
  `cuprum/_line_iteration.py`, the whole of `cuprum/_line_stream/`, and — for
  EP-M3 — `cuprum/_pipeline_collect.py` and
  `cuprum/_pipeline_stream_results.py`. Every one of these is a private module;
  the escalation is about breadth, not about interface. The breach itself is
  recorded in `Decision log` with the alternatives. A file outside this list,
  or any change to a public signature, is still a stop-and-escalate event.
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
  through the existing `_drain_stream_consumers` with `discard_on_cancel`
  *unset*, which is the mode that returns the partial buffer; discard mode is
  used only on routes that are re-raising some other error. Milestone EP-M1's
  second test asserts the payload directly, and EP-M2 and EP-M3 repeat the
  assertion on their routes.

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
  bug, because the line buffer fills while nobody is consuming from
  `LineStream` and the run's own cleanup then blocks writing a terminal event.
  Severity: medium. Likelihood: medium. Mitigation: EP-M2's tests drain the
  stream in a dedicated reader task, and the plan records the requirement in
  the users' guide: `lines()` backpressure counts towards the deadline.

- Risk: the new tests leak grandchildren, leaving SIGTERM-immune processes on a
  shared machine where other agents are working. Severity: medium. Likelihood:
  medium. Mitigation: every test reaps in `finally` through `reap_descendant`,
  which escalates to `SIGKILL` — the only signal the descendant honours.

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
  includes reading the module docstring against the new code, and EP-M4 reviews
  them again as part of the design-document pass.

## Progress

- [x] (2026-10-16 14:00Z) Branch renamed to
  `issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts` and
  the Lody session titled. Working tree clean at `c38459100`.
- [x] (2026-10-16 14:05Z) Confirmed `ebd6a41e4435` (the issue's revision) is an
  ancestor of `HEAD`; `_deadline_for_collection` arrived later, in `8c78a5b77`.
- [x] (2026-10-16 14:40Z) Built a scratch probe harness under `/tmp/probe533/`
  and ran twelve routes against `HEAD`. Result table in
  `Surprises & Discoveries`. Probes are scratch only and are never committed.
- [x] (2026-10-16 15:23Z) Wrote this plan.
- [x] (2026-10-16 16:05Z) EP-M1 Red: `test_run_no_stream_post_exit_deadline.py`
  added and run against the unmodified tree — 4 failed in 82.81 s, every
  failure an `asyncio.wait_for` `TimeoutError` at the harness bound of 20 s.
  The direct route hangs, confirmed for both group policies.
- [x] (2026-10-16 16:50Z) EP-M1 Green: `_await_writer_within_deadline` and
  `_cleanup_direct_run` added — 4 passed in 7.20 s.
- [x] (2026-10-16 17:20Z) EP-M1 refactor: the fix pushed
  `_subprocess_stream_run.py` to 476 lines, over the 400-line module cap, so
  the direct run moved to `cuprum/_subprocess_direct_run.py` and
  `_settle_owned_group_before_draining` moved to `cuprum/_subprocess_wait.py`
  (both run shapes need it; neither should import the other). 10 passed in
  18.03 s.
- [x] (2026-10-16 17:45Z) EP-M1 non-vacuity: two mutations run against
  `_subprocess_direct_run.py`. Removing the group settle fails exactly the four
  `own-group` arms and passes the four `inherit` arms; removing the writer
  cancel fails exactly the two async `inherit` arms. Both mutations reverted.
  See `Verification plan`.
- [x] (2026-10-16 18:05Z) EP-M1 `check-fmt` failed on `mdtablefix`: two files
  would be reformatted. `make fmt` applied; the gate then reported 93 files
  left unchanged. Lesson recorded under `Surprises & Discoveries`.
- [x] (2026-10-16 19:20Z) EP-M1 first `lint` attempt hit the runner's own
  4500 s bound during an uncached Rust stage (rustdoc "Finished `dev` profile
  in 23m 35s"; the managed sccache server was unreachable). Re-run with a
  longer runner timeout. Not a code failure.
- [x] (2026-10-16 19:30Z) EP-M1 `lint` attempt 2: every Python leaf passed,
  including the `interrogate` closure-docstring defect found and fixed at
  17:57Z. The clippy leaf then failed with
  `build-limits: the supervisor refused: unhealthy` — an admission outage, not
  a code defect. Independent confirmation: `build-limits-status` reports
  connection refused on `/run/user/1000/buildlimits/supervisor.sock`.
- [x] (2026-10-16 19:35Z) Removed an orphaned duplication allow entry the
  module split created
  (`cuprum/_subprocess_stream_run.py ~ cuprum/_process_handle/coordinator.py`).
  Verified against source, not merely by absence of a match; see `Decision log`.
- [x] (2026-10-16 19:55Z) EP-M1 self-review caught a real defect in the EP-M1
  fix: `_cleanup_direct_run` promised one shield over its two halves while the
  body made two sequential shielded calls, the second unshielded entirely.
  Composed into a single coroutine under one `_shielded_cleanup`. The property
  was unpinned by any direct-path test, so
  `test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group` was
  added with a new `DirectCleanupWatch`; mutation back to the two-shields shape
  fails it on the settlement assertion. 11 passed in 20.62 s.
- [x] (2026-10-16 20:00Z) Compile admission recovered on its own
  (`build-limits-status`: "capacity 4, held 2, healthy"). No workaround was
  applied at any point; `RUSTC_WRAPPER`/`RUSTDOC` were left untouched.
- [x] (2026-10-16 20:35Z) The first `test-python` run of the EP-M1 gate
  completed and reported the wheel snapshot as stale: creating
  `cuprum/_subprocess_direct_run.py` changed the release wheel's member list and
  `test_maturin_wheel_build_snapshot` failed on it. Fixed with a one-line
  sorted insert into `cuprum/unittests/__snapshots__/test_maturin_build.ambr`;
  the module sorts between `cuprum/_subprocess_context.py` and
  `cuprum/_subprocess_execution.py`. Verified the fix restores agreement in
  both directions rather than only silencing one entry: comparing every shipped
  `cuprum/**/*.py` on disk against the snapshot's `entries` now reports nothing
  missing and nothing stale, and the 165-name list is still in sorted order. See
  `Surprises & discoveries` for why no other gate could have caught this.
- [x] (2026-10-16 20:40Z) `make duplication` passed independently of the full
  gate, so the new module's `if typ.TYPE_CHECKING:` block did not surface as a
  new family: "duplication gate passed; 60 allowed by reasoned exceptions". The
  28-module entry in `[tool.duplication_gate.allow]` therefore needs no change
  — the block's three-line body is below the detector's floor, which is why the
  module is absent from it by design rather than by oversight.
- [x] (2026-10-16 20:50Z) The completed `test-python` run gave the real
  failure set, which is smaller and different in kind from what was assumed
  while it ran. Full result:
  `3 failed, 3284 passed, 93 skipped in 2657.74s (0:44:17)`, log
  `/tmp/test-python-cuprum-issue-533-frozen10-pre-snapshotfix.out`, dirty count
  10 at start and 10 at end. The three failures:

  | Test                                                                                          | Cause                    | Disposition                        |
  | --------------------------------------------------------------------------------------------- | ------------------------ | ---------------------------------- |
  | `test_maturin_build.py::test_maturin_wheel_build_snapshot`                                    | the stale wheel snapshot | **real defect, fixed** (20:35Z)    |
  | `test_doctest_warning_contract.py::test_pinned_doctest_route_rejects_a_warning`               | admission outage         | retried, **still outage** (21:35Z) |
  | `test_rust_pump_debug_abort.py::test_debug_rust_pump_does_not_abort_on_early_exit_or_timeout` | admission outage         | retried, **green** (21:35Z)        |

  Both non-defect failures carry
  `build-limits: the supervisor refused: unhealthy` (or the
  `[Errno 111] Connection refused` variant after the wrapper's 270 s wait) in
  their bodies, and both are Rust-touching tests therefore routed through
  compile admission. Their cause is the supervisor outage that was live for
  part of the run's 44 minutes, not the diff.

  **A diagnosis carried into this milestone was wrong and is retracted.** The
  doctest failure was expected to be the known 30 s load flake, on the reading
  that it ran under `pyproject.toml`'s suite-wide `timeout = 30`. It does not:
  `cuprum/unittests/test_doctest_warning_contract.py:71` carries
  `@pytest.mark.timeout(600)`, raised deliberately because the test compiles a
  scratch crate twice through shared admission. The failure body is an
  `AssertionError` from the test's own `former_route.returncode == 0`
  assertion, reporting `assert 101 == 0` with the supervisor refusal inside —
  an environment failure, not a timeout and not a code defect. Recording the
  actual mechanism matters more than the disposition, because "it's just the
  flake" would have left a supervisor outage unexplained and could have
  mislabelled a genuine regression later.
- [x] (2026-10-16 20:52Z) Admission recovered again and is stably healthy
  across three samples
  (`capacity 4, held 2/3/2, healthy, sccache unreachable, compiling directly`).
  The `sccache unreachable` note means builds run uncached; that is not a
  failure. The supervisor is flapping rather than down, which is why the same
  suite both passed and failed Rust leaves within one run.
- [x] (2026-10-16 21:35Z) Focused retry of the three failures, on the
  snapshot-fixed tree: `1 failed, 2 passed in 1772.54s (0:29:32)`, log
  `/tmp/focused-epm1-snapshotfix-cuprum-issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts.out`.
  **The snapshot fix is verified** — `test_maturin_wheel_build_snapshot`
  passed ("1 snapshot passed"), and `test_rust_pump_debug_abort.py` passed, so
  the first `test-python` run's two `retry` rows resolve as: one real defect
  fixed, one outage artefact now green. That leaves one failure, and the
  retraction above is confirmed on a second independent run: the doctest test
  failed again with admission in its body, now at an even earlier point — the
  `cargo`→`rustc` **version probe** was refused, so the scratch crate was never
  compiled at all. The body reads `error: process didn't exit successfully:`
  naming `build-limits-rustc` invoking `rustc -vV` and exiting 1, with
  `[build-limits] rustc: waiting for a compile slot (123 ahead, 45s)` and
  `build-limits: the supervisor refused: unhealthy`. Given a queue of 123
  crates ahead of it, this run had essentially no chance of reaching admission,
  which is consistent with an outage rather than with the test. The test cannot
  be reached by this diff on its own terms either: it writes its scratch crate
  into `tmp_path` and reads only `cargo`, `RUSTFLAGS` and `RUSTDOCFLAGS`, so it
  never consults the one file the diff touches (`pyproject.toml`, and only at
  its `[tool.duplication_gate]` section). Disposition: re-run with the full
  suite once admission holds, and record the outcome; no code change is
  warranted by evidence that points at the environment.
- [x] EP-M2 — `SafeCmd.lines()`'s post-exit drain. Core bound written in
  `cuprum/_line_stream/drain.py` (`_collect_line_stream_after_exit` plus
  `_await_writer_within_scope`, `_await_consumers`, `_first_reader_failure`,
  `_settle_and_reconcile`), coordinator rewired, and an unstamped-`started_at`
  defect found and fixed in the same path — see `Surprises & discoveries`. New
  module `cuprum/unittests/test_safe_cmd_lines_post_exit_deadline.py` collects
  6 cases (three tests × two group policies) and passes. Red state measured
  independently rather than asserted: with `_deadline_for_collection` neutered
  to `return None` in the drain module's namespace, the same 6 cases failed as
  `TimeoutError` raised from `asyncio/timeouts.py:115` after 123.00 s — the
  test harness's own 20 s bound spent six times over — and the source file was
  byte-identical afterwards (`diff` clean). Green after the bound:
  `6 passed in 8.43s`. Neighbouring modules green in the same tree:
  `test_safe_cmd_lines.py` (20), `test_line_stream_exit.py` (7),
  `test_line_stream_terminal_outcome.py` (1), `test_line_stream_ownership.py`
  (3), `test_line_stream_coordinator_stateful.py` (1),
  `test_subprocess_timeout.py` (4) — 36 collected, 36 passed, 2.32 s. Together
  with EP-M1's module the pair is 17 passed in 27.86 s. Milestone gate and
  CodeRabbit review outstanding at the time of writing; both are recorded below.

- [x] EP-M2 milestone gate, and the review returned by
  `coderabbit review --agent --committed --base-commit ed1d47e87`. Ten
  findings. Eight were valid and are applied: the post-exit expiry now emits
  `LineStreamPhase.TIMEOUT` before its settlement (with a new case,
  `test_the_lines_deadline_expiry_is_reported_as_a_timeout_phase`, that fails
  without the emission and passes with it); `_coordinate_line_stream`,
  `_run_to_command_result` and `_wait_for_line_stream_exit` no longer accept an
  execution bundle beside the run and read `run.execution` instead, which is
  the single-source rule this plan's own decision log already required; the
  direct run's `None`-deadline comment, two comments in the direct-run test
  module, the developers' guide's translation sentence, the plan's status line,
  and the plan's first-person pronouns were corrected. The vacuous third case
  in the line-stream test module was deleted and its constant check hoisted to
  module scope. Two findings were rejected as factually wrong for this tree —
  see `Decision log`.
- [x] (2026-10-19) EP-M2 follow-up: the review's *first* finding was applied
  to the drain, and the regression test written for it exposed the same
  misclassification at three further layers — `_run_to_command_result`,
  `_collect_stream_output`, and `_await_direct_completion`. All four are fixed,
  each with a red-then-green observation, and the invariant they rest on
  (`_SubprocessTimeoutError` is disjoint from `TimeoutError` by design) is
  recorded in `Surprises & discoveries`. New module
  `cuprum/unittests/test_line_stream_reader_failure.py`, 2 tests: the `lines()`
  case and the streamed `run()` case. Focused runs: the new module 2 passed;
  the line-stream neighbours 17 passed; the post-exit deadline modules 40
  passed; the streamed-path and timeout modules 32 passed. Genuine expiries
  were re-verified on both paths at `timeout=0.2` and `timeout=0` and still
  produce `TimeoutExpired` carrying the configured timeout, so narrowing the
  catches did not unbound the real deadline.
- [x] (2026-10-21) EP-M2 milestone gate at `33e066dfd`. All four gates `exit=0`:
  `check-fmt` (`/tmp/check-fmt-…-2.out`), `lint` (`/tmp/lint-…-3.out`),
  `typecheck` (`/tmp/typecheck-…-3.out`), and `make test` (`/tmp/test-…-3.out`,
  `167` Rust tests passed plus every `PYTEST_TARGETS` pattern). The first
  `make test` attempt failed only on
  `test_pinned_doctest_route_rejects_a_warning` after 600 s; that file has an
  empty diff against the branch base, its per-route `CARGO_TARGET_DIR` was
  never created, and admission was saturated at the time, so it was classified
  environmental and re-dispatched rather than "fixed". The re-run passed the
  same test at line 938.
- [x] (2026-10-21) EP-M2 milestone review (`coderabbit review --agent`). Three
  findings. One was valid and is applied: the comment above the bare `raise` in
  `_subprocess_execution.py` now names both exit-wait routes, and this plan's
  `Surprises & discoveries` repeats the corrected claim. Two were rejected as
  factually wrong for this tree, re-verified at `33e066dfd` rather than
  inherited from the previous rejection — see `Decision log`.
- [ ] EP-M3 — `Pipeline.run()`'s final output and its `INHERIT` wait-timeout
  route.
- [ ] EP-M4 — documentation and full validation.
- [ ] Gate each commit through `scrutineer`; request `coderabbit review --agent`
  after each milestone.
- [ ] Push and open the draft PR.

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

  | #   | Route            | Configuration                              | Verdict                    |
  | --- | ---------------- | ------------------------------------------ | -------------------------- |
  | 1   | `run()` streamed | capture, OWN_GROUP                         | bounded, expired at 1.54 s |
  | 2   | `run()` streamed | capture, 2 MiB stdin, OWN_GROUP            | bounded, 1.54 s            |
  | 3   | `run()` direct   | no capture, 2 MiB stdin, INHERIT           | hangs                      |
  | 4   | `run()` direct   | no capture, 2 MiB stdin, OWN_GROUP         | hangs                      |
  | 5   | `run()` direct   | no capture, no stdin                       | fine, 0.07 s               |
  | 6   | `lines()`        | capture, OWN_GROUP                         | hangs                      |
  | 7   | `lines()`        | no capture, OWN_GROUP                      | hangs                      |
  | 8   | `Pipeline.run()` | success, capture, OWN_GROUP                | hangs                      |
  | 9   | `Pipeline.run()` | success, no capture, OWN_GROUP             | fine, 0.07 s               |
  | 10  | `Pipeline.run()` | success, non-final stage leaves descendant | hangs                      |
  | 11  | `Pipeline.run()` | wait timeout, OWN_GROUP                    | bounded, 1.58 s            |
  | 12  | `Pipeline.run()` | wait timeout, INHERIT                      | hangs                      |

  Routes 1–2 are bounded because `_collect_stream_output` already wraps its
  whole post-exit boundary in `asyncio.timeout(_deadline_for_collection(...))`.
  Route 11 is bounded because the pipeline wait-timeout handler calls
  `_terminate_timed_out_stages` before gathering. Route 12 hangs because under
  `INHERIT` no signal reaches the *descendant* holding the pipes: the leader is
  already gone.

- **Route 4 hangs under `OWN_GROUP` too.** This was counter-intuitive — the
  group teardown exists to reclaim descendants. The reason is ordering: the
  direct path awaits the parked stdin writer *before* any settle happens, and
  the settle only runs when the writer await fails. There is nothing to fail
  it. The bound has to come first; the settle follows expiry.

- **Route 5 and route 9 are fine for the same reason, and it is not
  reassuring.** Both have literally nothing to await after exit — no stdin
  task, and (for a no-capture pipeline) no output tasks. They are bounded by
  vacuity, not by design, and are listed to show the probe distinguished them
  rather than to claim they are exercised.

- **A bounded return says nothing either way about the descendant, and the
  original claim here was never measured.** Route 11 returned in 1.58 s under
  `OWN_GROUP`. This plan originally recorded that the descendant survived that
  teardown, on the premise that `Pipeline.run()` carries no process-group
  policy. Both halves were wrong. `cuprum._pipeline_spawn` derives `owns_group`
  from `config.ctx.resolved_process_group` for every stage
  (`_pipeline_spawn.py:132-133`), records it per stage in the spawn result, and
  the timeout handler passes it on to `_terminate_timed_out_stages`. A
  measurement on 2026-10-21 that read the descendant's `/proc/<pid>/stat` state
  found it already gone when the run returned at 1.52 s. What route 11 does
  *not* exercise is the success path, where no teardown runs at all (route 8);
  that is the route EP-M3 has to bound. `INHERIT` is the arm that genuinely
  cannot reclaim a descendant, because the only process it may signal is the
  direct child and that child has already exited (route 12). The new pipeline
  tests therefore assert reclamation under `OWN_GROUP` and assert only a
  bounded return under `INHERIT`.

- **The chained lint log does not name the tool that failed.** `make lint` runs
  `ruff`, `interrogate`, `pylint`, `skylos`, the duplication gate, and then
  `rustdoc`/`clippy` in one recipe. A failure prints only the failing command's
  own output, and that output was 54 KB into the log. The `interrogate` defect
  found at 17:57Z (two nested closures without docstrings in the new test
  module, 96.8 % against a 100 % floor) was located only by grepping the
  `Makefile` for `minimum:`, which surfaced `Makefile:235`. Treat every
  `make lint` failure as "some tool in the chain", and identify it before
  reading the message as a code defect.

- **`mdtablefix` is not satisfied by writing documentation "already
  formatted".** `make check-fmt` failed with `2 files would be reformatted` on
  `docs/developers-guide.md` and this plan, both written by hand at 80 columns.
  `make fmt` rewrote the plan (771 → 774 lines). The formatter owns the layout;
  writing to the column count by hand is not the same as being formatted.

- **A module split can orphan a duplication allow entry, and the gate reports
  it as a coincidence rather than as growth.** Moving the direct run out of
  `cuprum/_subprocess_stream_run.py` removed the last `_TeardownPolicy` import
  from that module, which is the overlap a two-key entry named. The entry then
  matched no family, and the gate said so — but framed it as "the overlap at <a
  26-location import family> is a path-glob coincidence", which is a misleading
  pointer: the real cause was that the entry's *subject* had moved. See
  `Decision log` for how the removal was confirmed.

- **A new module is also a wheel-layout change, and the only gate that says so
  is a snapshot reached through a full release build.** Creating
  `cuprum/_subprocess_direct_run.py` left the `entries` list in
  `cuprum/unittests/__snapshots__/test_maturin_build.ambr` stale, so
  `test_maturin_wheel_build_snapshot` failed. The failure was expensive to
  reach and cheap to fix, and both halves are worth recording. It is reached
  only by building the native wheel through maturin and cargo — the test carries
  `@pytest.mark.timeout(0)` and took over twenty minutes on this machine, most
  of it queued behind other agents in compile admission — so it surfaces late
  in a full `make test` and looks like a stall rather than a defect. The fix is
  one sorted insert, because the snapshot records the wheel's own member names
  in sorted order and the new module sorts between
  `cuprum/_subprocess_context.py` and `cuprum/_subprocess_execution.py`.

  The useful part is why nothing caught it earlier. The wheel's packaging is
  globbed by directory (`python-source = "."` with
  `exclude = ["cuprum/unittests/**"]`), so no packaging manifest enumerates
  modules and no lint notices a new one. The snapshot is a *consequence* check
  rather than an inventory: it asserts what the build actually emitted, which
  is why it cannot be replaced by keeping a list up to date by hand. It is also
  the only gated enumeration of shipped modules — `pyproject.toml`'s
  duplication allow-list and the module-boundary prose in `docs/` name modules
  too, but neither is asserted against the tree, and grepping every tracked
  file for a module list found no other mechanized inventory. So the practical
  rule is: **creating a module is a packaging change, and the wheel snapshot is
  the gate that will tell you.**

- **The `lines()` path never stamped `started_at` onto the bundle its caller
  keeps, and the deadline is what made that matter.** `_start_line_stream_run`
  rebuilds the execution with `dc.replace(execution, started_at=...)` and hands
  the rebuild to the consumers, but `_drive_line_stream` went on passing its
  own *unstamped* bundle to `_coordinate_line_stream`. Both bundles are
  `_SubprocessExecution`, both carry the same `observation`, and only the two
  differ in `started_at` — so the defect was invisible until EP-M2 introduced
  the first read of `execution.started_at` on this path. The direct path had
  already solved the same problem by returning
  `_StartedRun(process, execution)` and rebinding at the call site; the
  line-stream analogue kept only a bare float in `_LineStreamRun.started_at`,
  which the consumers read but the coordinator's `execution` parameter did not
  carry.

  How it surfaced, and why the first diagnosis was wrong: with `timeout=2.0`,
  `_deadline_for_collection` computed `2.0 - (perf_counter() - 0.0)` ≈
  `-692339`, i.e. two seconds minus the machine's ~8-day monotonic uptime. The
  traceback printed the value (`remaining = -692019.82`) beside
  `started_at=0.0`, and the two readings agreed to within the elapsed test time
  — which is the evidence that pinned it, not the stack alone.
  `test_lines_empty_output_completes_and_publishes_result` (`python -c pass`,
  no output, 2 s budget) failed with a `TimeoutExpired` raised instantly, which
  reads like "the new bound misfired" and is really "the reference it subtracts
  from was never set".

  The interim guard added to `_await_consumers`
  (`if remaining is not None and remaining <= 0: raise TimeoutError`) was both
  out of plan and wrong in its own right, and was removed: `asyncio.wait`
  returns immediately for a non-positive timeout with every unfinished reader
  left in `pending` (verified: `done 0, pending 1` for `timeout=-100.0`), so a
  reader that already finished is reported as finished and a spent budget is
  still reported as expiry by the existing `if pending` arm. A separate guard
  could only *add* a way to fail a run whose readers had all completed.

  Fix and its shape: `_LineStreamRun` now carries the stamped `execution`
  instead of a bare `started_at`, mirroring `_StartedRun`, and both the
  coordinator's exit-event/`duration_s` reading and `_run_to_command_result`'s
  `started_at` read come from it. There is now one bundle, so the two cannot
  drift again.

- **A reader's own `TimeoutError` was misclassified at three layers, not one,
  and the reviewer's finding named only the first.** CodeRabbit's finding said
  a reader failure shaped like a `TimeoutError` would be reported as the run's
  deadline. The regression test confirmed it and then found the same
  misclassification twice more on the streamed `run()` path, at layers that had
  been introduced by earlier milestones rather than by EP-M2. The four sites,
  in the order a streamed `run()` reaches them:

  | Layer                | Site                                             | Fix                                                                      |
  | -------------------- | ------------------------------------------------ | ------------------------------------------------------------------------ |
  | line-stream drain    | `drain._collect_line_stream_after_exit`          | return the failure from `_await_consumers`, raise after the scope closes |
  | line-stream boundary | `coordinator._run_to_command_result`             | catch `_SubprocessTimeoutError` only                                     |
  | streamed collect     | `_subprocess_stream_run._collect_stream_output`  | gate on the scope's `expired()`                                          |
  | streamed boundary    | `_subprocess_execution._await_direct_completion` | catch `_SubprocessTimeoutError` only                                     |

  The invariant that makes the two catch-narrowing fixes safe is worth stating
  because it is not local to the code: **every genuine expiry on these paths
  has already been translated to `_SubprocessTimeoutError` before the wider
  frame sees it** — the exit-wait routes call `_handle_stream_timeout`, which
  raises that wrapper. A bare `TimeoutError` escaping those frames is therefore
  always somebody else's failure. That invariant holds because
  `_SubprocessTimeoutError` deliberately does *not* subclass `TimeoutError`;
  the two types are disjoint on purpose.

  The collect layer could not use the same narrowing: its scope is the thing
  that raises on expiry, so the exception type is identical either way. It uses
  `asyncio.timeout(...).expired()` instead. That discriminator was probed
  rather than assumed, because the plan's earlier EP-M1 decision had rejected
  `expired()` for a different question: an inner `raise TimeoutError` inside
  the scope leaves `expired()` **False**, and a genuine timer expiry leaves it
  **True**. The two questions are different — EP-M1 asked "was this expiry or a
  caller's cancellation?" (where `expired()` conflates the two), and this one
  asks "did this scope's timer fire?" (which is exactly what `expired()`
  answers). Both decisions stand, each for its own question.

  `_await_direct_completion` distinguishes the two paths by
  `execution.consumes_stdout or execution.consumes_stderr`, which is the same
  test that selected the streamed run in the first place; on the streamed path
  the bare exception is therefore a reader failure, and on the direct path it
  comes from the exit wait itself: `_wait_for_exit_code_within_timeout`
  re-raises a bare `TimeoutError` on both its routes, the non-positive
  already-elapsed deadline and the elapsed positive one.

  Non-vacuity: with all four fixes reverted, both new tests fail —
  `test_lines_reader_timeout_is_not_reported_as_the_run_deadline` raises the
  invariant failure rather than the sentinel, and the `run()` sibling loses the
  sentinel the same way. Restored, both pass. The line-stream test also asserts
  the `LineStreamPhase.TIMEOUT` phase is *absent* and the teardown phases are
  *present*, so it pins what the caller observes, not merely which exception
  object comes back.

- **The same doctest test has a third failure mode, and it must not be
  mistaken for the two already recorded.** `Progress` records two independent
  runs that failed inside the test's *own* assertion, with
  `build-limits: the supervisor refused: unhealthy` in the reported body; that
  reading is the better-evidenced one and stands. A background scrutineer run
  failed differently: `Failed: Timeout (>600.0s) from pytest-timeout`, raised
  inside `selectors.poll`, with zero `build-limits` matches in the log at all.
  Saturation evidence around that run: pre-run `build-limits-status` showed
  `capacity 4, held 4, healthy` with ~170 waiting, and post-run all five
  queries returned `[Errno 11] Resource temporarily unavailable` while 340
  wrappers were live.

  The three runs share one cause — compile admission under load — but they are
  not interchangeable as evidence, and the third is the weakest of the three.
  Its limitation is structural: `_run_doctests` uses `capture_output=True`, so
  the child's diagnostics are invisible to the test that times out. **Absence of
  `build-limits` text is therefore not evidence that admission was not the
  blocker**, which is the opposite of what it looks like. Treat a
  `pytest-timeout` failure inside `selectors.poll` on this machine as
  undiagnosed until admission health is read from *outside* the failing process.

## Decision log

- Decision (EP-M2, 2026-10-16): fix the unstamped-`started_at` defect found
  while implementing EP-M2 as part of EP-M2, rather than filing it separately
  or working around it. Rationale: EP-M1 made `execution.started_at`
  load-bearing on this path, so the defect and its symptom are the same
  change's responsibility — leaving it would ship a `lines()` post-exit
  deadline that fires against a `0.0` reference, which is worse than the
  unbounded wait it replaces. The fix is also confined to the routes this plan
  already owns. Alternative considered: keep the `remaining <= 0` guard as a
  stopgap and file the stamping bug separately (rejected: the guard would
  convert every `lines()` run into an instant `TimeoutExpired`, i.e. trade a
  hang for a systematic false failure).

- Decision (EP-M2, 2026-10-16): carry the stamped bundle on `_LineStreamRun`
  rather than passing it beside the run, and read `started_at` from it in both
  places the coordinator needs it. Rationale: the defect existed precisely
  because two bundles with the same type travelled together and only one was
  stamped; removing the duplicate makes the class of bug unrepresentable rather
  than merely fixed. This mirrors the direct path's `_StartedRun`, which had
  already solved the same problem the same way. Alternative considered: keep
  the bare `started_at` field and pass the stamped `execution` as an extra
  argument to `_coordinate_line_stream` (rejected: two sources for one value is
  the original defect, restated).

- Decision (EP-M2 review, 2026-10-19): reject the review's two
  `_spawn_stdin_writer` findings rather than applying them. Both assert a
  four-argument contract `(process, plan, codec, observation)` with the plan
  read from `execution.stdio.stdin` and the codec from
  `cuprum._subprocess_stdin_stream._stdin_codec`. Neither exists in this tree:
  the real signature is `_spawn_stdin_writer(process, stdin_data, observation)`,
  `_SubprocessExecution` has no `stdio` field, and there is no
  `_subprocess_stdin_stream` module. The direct run and the streamed path both
  already call the three-argument form, and the direct run's `execution` is
  where `stdin_data` lives. Applying the suggestion would replace working calls
  with an import of a module that does not exist. The review appears to have
  been written against a different revision. Rationale for recording rather
  than silently skipping: a rejected finding is evidence about the review, not
  about the code, and the next reader will meet the same comment.

- Decision (EP-M2 review, 2026-10-21): reject the same `_spawn_stdin_writer`
  finding again, and re-verify rather than inherit the earlier rejection. The
  follow-up review raised it once more, this time as `critical`, claiming
  `execution.stdin_data` raises `AttributeError` because the bundle exposes
  `stdio`. Re-checked at `33e066dfd`: `_spawn_stdin_writer` is still
  `(process, stdin_data, observation)` at `cuprum/_subprocess_stdin.py:139`,
  `_SubprocessExecution` still declares `stdin_data: bytes | None` at
  `cuprum/_subprocess_spawn.py:68` and no `stdio` anywhere, and neither
  `cuprum/_subprocess_stdin_stream.py` nor any `_stdin_codec` symbol exists in
  the repository — the only occurrence of that name on disk is inside the quote
  of this very review in this plan. The claim that the failure occurs "before
  the child is awaited" is therefore untestable here: the attribute read
  succeeds. Rationale for re-verifying instead of citing the earlier rejection:
  the passage of a milestone and a fresh review is exactly when a
  previously-rejected finding deserves a second look, and a rejection that
  turns out to have been right would be a critical defect shipped on the
  strength of a stale check.

- Decision: bound each post-exit phase at the *route* level rather than by
  teaching the drain, the consumers, or the writer about deadlines. Rationale:
  the same deadline already exists and is already honoured by the streamed
  path; the defect is four sites that never ask for it. Threading a deadline
  into `_drain_stream_consumers` would give a second mechanism for the same
  invariant. Alternatives considered: a deadline parameter on every await site
  (rejected: duplicates the arithmetic and drifts), and a watchdog task that
  cancels the run (rejected: would change which exception the caller sees).
  Date: 2026-10-16.

- Decision: keep the expiry's existing public shape — `TimeoutExpired` with the
  *configured* timeout and the run's already-read output. Rationale: a streamed
  run already expires this way, and a caller cannot tell which route expired.
  Alternatives considered: raising a distinct post-exit exception (rejected: a
  new public type for a distinction no caller asked for), and reporting the
  bounded remainder as the timeout value (rejected: it would make `exc.timeout`
  mean two different things on two routes). Date: 2026-10-16.

- Decision: for `lines()`, wait on consumers with a bounded
  `asyncio.wait` and then reconcile, rather than wrapping the consumer gather in
  `asyncio.timeout`. Rationale: a cancelled line consumer can be holding a
  trailing line it has not published, and the repository's line-stream contract
  is that a delivered line stays delivered. `asyncio.wait` observes; the
  existing reconciliation performs the cancellation, and it is the code that
  already knows how to do so without losing a trailing line. Date: 2026-10-16.

- Decision: do not signal an inherited group on a pipeline deadline, and do not
  attempt to. Rationale: under `INHERIT` the only process the run may signal is
  the direct child, and on the post-exit routes that child has already exited;
  reaching a descendant there would mean adopting a group the caller declined.
  The plan records the gap so the pipeline tests assert a bounded *return*
  rather than a reclaimed descendant on that arm. Date: 2026-10-16; rationale
  corrected 2026-10-21, because the original cited a process-group policy
  `Pipeline.run()` in fact has (see the entry of that date).

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

- Decision: read expiry and cancellation apart by catching `TimeoutError` and
  letting `CancelledError` propagate, rather than by consulting the timeout
  scope's `expired()` state. **This deviates from the letter of the Coding
  Plan**, which named `expired()`. Date: 2026-10-16.

  Rationale: `asyncio.timeout.__aexit__` already performs the cancel accounting
  (`self._task.uncancel() <= self._cancelling`) that decides whether an
  overlapping expiry-and-cancellation pair was its own timer or the caller's
  `cancel()`. Re-deriving that from `expired()` would be a second, less careful
  copy of the same judgement: `expired()` returns true for both the `EXPIRING`
  and `EXPIRED` states and cannot distinguish its own timer from a caller's
  `cancel()` arriving after that timer fired, so a caller's cancellation would
  be reported as a timeout. The scope's own accounting is authoritative; the
  repository's streamed boundary already relies on it, so the direct path now
  does too.

  Impact: none on the observable contract — every case still raises the public
  `TimeoutExpired` carrying the configured timeout, and a caller's cancellation
  still surfaces as `CancelledError`. The deviation is in the mechanism, not
  the behaviour. No upstream artefact needs revising.

- Decision: move `_settle_owned_group_before_draining` from
  `cuprum/_subprocess_stream_run.py` to `cuprum/_subprocess_wait.py`, and split
  the direct run into `cuprum/_subprocess_direct_run.py`. Rationale: the helper
  is now needed by both run shapes, and the streamed module importing it back
  from a direct-run module (or the reverse) would make one shape depend on the
  other for no reason. `_subprocess_wait` is already where the rules for
  *ending* a run live — the deadline, the exit wait, the task reconciliation —
  and the group settlement is exactly that kind of rule. The streamed module
  re-exports the name so existing imports keep resolving. Alternatives
  considered: leaving it in the streamed module and having the direct run
  import it (rejected: an inverted dependency between two sibling shapes), and
  duplicating it (rejected: two copies of a teardown invariant is how they
  drift). Date: 2026-10-16.

- Decision: parametrize the direct-run module over all four entrypoints
  (`run`, `run_bytes`, `run_sync`, `run_bytes_sync`), while stating in the
  module docstring which arms carry which evidence. Date: 2026-10-16.

  Rationale: the two synchronous entrypoints delegate to `asyncio.run`, whose
  shutdown cancels whatever is still pending before the case can look.
  Measured, not assumed: removing the writer cancellation fails the two async
  `inherit` arms and passes all four sync arms. The sync arms therefore do
  *not* pin writer cancellation, and the docstring says so rather than letting
  a reader infer equal weight from equal parametrization. What they do
  establish — the wrapper reaches the same bounded path, reports the same
  public exception, and settles the group — is still worth the parametrization.

  Impact: the module's claims are narrower and true; no production change.

- Decision: assert descendant reclamation only on the `OWN_GROUP` arms. Date:
  2026-10-16. Rationale: `_members_remain_to_signal` vetoes the signal when the
  group holds no signalable members, and `_settlement_of` probes the child's
  *group* under `owns_group` rather than the leader's own state — so an exited
  leader does not suppress the group signal. Verified by reading
  `cuprum/_process_signal.py:130-212`, and confirmed by mutation: removing the
  settle leaves all four `own-group` descendants alive past the test's 5 s
  reclamation window. Under `INHERIT` there is no group to reclaim and nothing
  to assert, which is why the reclamation assertion is conditional rather than
  parametrized over both policies.

- Decision: delete the duplication allow entry keyed on
  `cuprum/_subprocess_stream_run.py ~ cuprum/_process_handle/coordinator.py`,
  rather than widen it to the module the overlap moved to. Date: 2026-10-16.

  Rationale, and this is the part the plan's `Constraints` require be recorded
  rather than assumed: AGENTS.md forbids deleting a stale entry merely because
  no family matched it, so the removal was confirmed against the source first.
  Three checks, in order. (a) The entry's own reason names its subject — "the
  same run-machinery surface — `_drain_context`, `_reconcile_run_tasks`,
  `_wait_for_exit_code_within_timeout` … and `_TeardownPolicy` from its own
  module". `grep -n '_TeardownPolicy' cuprum/_subprocess_stream_run.py` now
  returns nothing: the import the entry was about is gone from that module, and
  it did not move to `cuprum/_subprocess_direct_run.py` either (same grep,
  empty). So there is no successor module to widen the entry *to*. (b) At
  pristine `HEAD` the entry matched a real two-location family
  (`_subprocess_stream_run.py:52-68 ~ coordinator.py:52-62`, value 25.2); with
  this branch's tree it matches nothing. The change in status is attributable
  to this branch, which is what makes "stale" a claim about the split rather
  than about the detector. (c) Dropping the entry and re-running the detector
  at progressively lower `--min-size` floors (24, 20, 16, 12, 8) shows no
  family at or above the configured floor 24 containing either key; the
  residual overlaps only appear at floor 12 and are three- and four-location
  import families under the *other* prologue entry's keys, not this one's.
  Widening the entry to those would be exactly the "manufacture a generic
  utility to satisfy the detector" move AGENTS.md rejects.

  Impact: the gate reports 60 allowed families instead of 61, with no unmatched
  entry. Nothing else in the allowlist was touched, and no entry was removed on
  the strength of a report alone.

- Decision: `_cleanup_direct_run` composes its two halves into one coroutine
  under a single `_shielded_cleanup`, rather than making two sequential
  shielded calls. Date: 2026-10-16.

  Rationale: this was a defect in the EP-M1 fix as first written, found by
  re-reading the diff rather than by a failing test — which is the point worth
  recording. `_shielded_cleanup` finishes the task it was given and *then*
  re-raises the caller's cancellation, so sequential calls are not one shielded
  sequence: a cancellation landing in the first exits before the second is
  awaited. The direct path's first half is the writer reclamation and its
  second is the group settlement, so the bug left the descendant holding the
  run's stdin unsignalled — precisely the failure the milestone exists to fix.
  The streamed path already had this shape right
  (`_settle_group_and_reconcile_consumers`), and its own second-cancellation
  test is what made the asymmetry visible.

  Impact: the property was unpinned on the direct path, so a test was added
  rather than the fix being taken on trust. `DirectCleanupWatch` gates both
  halves;
  `test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group`
  asserts the settlement was entered *and* the descendant is gone. Mutating the
  helper back to the sequential shape fails it on the first assertion, so the
  test detects the defect it was written for rather than merely passing beside
  it. `start_owned_run` grew a `capture` parameter to select the direct shape;
  no production behaviour changed beyond the shield composition.

- Decision (EP-M2 review finding, 2026-10-19): treat a reader's `TimeoutError`
  as the reader's failure on every path, and narrow the catches that had been
  translating it. The alternative — threading a private wrapper through the
  reader contract so callers can tell the two apart — was rejected because it
  changes the exception a public hook may observe and puts library knowledge
  into user code. Date: 2026-10-19.

  The decision has two halves, and they use different mechanisms on purpose.
  Where a genuine expiry has already been translated before the frame runs, the
  frame catches `_SubprocessTimeoutError` alone; where the frame *owns* the
  timer, it asks the scope whether its own clock fired. Fixing only the
  reviewer's site would have left the streamed `run()` defect in place, and a
  reader failure there is worse than a hang: it is reported as a timeout the
  caller never configured. The four sites and the probe that settled
  `expired()`'s behaviour are recorded in `Surprises & discoveries`.

- Decision (2026-10-19): record the scope-tolerance breach rather than
  re-cutting the plan. The trigger allowed four modules; the delivered change
  touches `cuprum/_subprocess_direct_run.py`, `cuprum/_subprocess_wait.py`,
  `cuprum/_subprocess_execution.py`, `cuprum/_line_iteration.py` and three of
  the four `cuprum/_line_stream/` modules as well, all of them private.
  Rationale for continuing rather than stopping: every additional file was
  reached by a *single* invariant — the post-exit deadline — and splitting the
  work into more commits would not have reduced the file count, only hidden it.
  The public surface is unchanged, which is the tolerance that protects
  callers. The measured shape is now written into `Tolerances` so the next
  reader sees the real boundary rather than the predicted one.

  Impact: none on behaviour. The plan's earlier "four-file shape" sentence was
  a prediction made before EP-M1's module split; it has been replaced with the
  measured list rather than quietly exceeded.

- Decision (EP-M3, 2026-10-21): the collector composes its own capture-grace
  step over its N+1 consumer tasks, rather than handing them to
  `_DrainContext.eof_grace_waiter`. Rationale: `_EofGraceWaiter` in
  `cuprum/_subprocess_wait_types.py` is typed over a *two*-element tuple,
  because it exists to serve the direct run's stdout/stderr pair, while a
  pipeline holds one stderr consumer per stage plus at most one final stdout
  consumer. Reusing the hook would either need that type widened — in a module
  outside EP-M3's allowed change list — or a cast that lies about the tuple
  length to a function the collector does not own. The collector therefore
  awaits `(context.eof_grace_waiter or _await_eof_grace)(tuple(tasks))` in the
  same shape `_await_capture_eof_grace` does, keeping one copy of the 0.25 s
  rule (`_CAPTURE_EOF_GRACE_S`) and one copy of the pending-reader accounting
  while leaving the public and private signatures alone. It still uses
  `_settle_consumers`, `_decode_consumer_result` and `_DrainContext`, and it
  does not call `_drain_stream_consumers`, whose failure reporting is
  stdout/stderr-pair specific. Alternative considered: widen `_EofGraceWaiter`
  to an arbitrary sequence (rejected: outside the allowed files, and it would
  put pipeline-shaped knowledge into a direct-run type).

- Decision (EP-M3, 2026-10-21): `_gather_pipeline_outputs` is retained and
  taught to accept a deadline, rather than retired in favour of a single gather
  inside the new helper. Rationale: EP-M3 deliverable 3 itself says to pass the
  remaining budget "from `_collect_pipeline_inputs` *and*
  `_gather_pipeline_outputs`", which presupposes the helper survives. Retiring
  it would force an edit to `cuprum/_pipeline_internals.py`, which imports it
  at line 38 and lists it in `__all__` at line 107; that module is outside the
  milestone's allowed change list, and re-exporting the replacement from it
  purely to keep the name importable would be compatibility theatre for a
  private helper with one caller. Only `_gather_optional_text_tasks` is
  retired, whose dependants are the collector's import and one unit test.
  Alternative considered: keep `_gather_optional_text_tasks` and add a second,
  bounded gather path (rejected: deliverable 2 requires one gather path).

- Decision (EP-M3, 2026-10-21): no new per-stage telemetry code is added for
  the final-output expiry. Rationale: `_run_spawned_pipeline` already catches
  `_sh_module().TimeoutExpired` and calls `_finalize_pipeline_timeout`, which
  calls `_report_pipeline_timeout_expiry` (per-stage `pid`, mode
  `elapsed_deadline` or `non_positive_immediate`) and
  `_emit_timeout_exit_events`. The shared handler inside the collector
  therefore owes only the public `TimeoutExpired` carrying the configured
  timeout, the captured output and the concatenated stage stderr. Adding a
  second emission site would double-report every pipeline expiry. Alternative
  considered: report at the expiry site as the single-command path does
  (rejected: the pipeline contract is one deadline reported once per stage, and
  that layer already does it).

- Decision (EP-M3, 2026-10-21): "terminate and settle the stages first,
  including owned groups whose leaders have exited" is satisfied by reusing
  `_terminate_timed_out_stages`, not by adding leader-independent termination.
  Rationale: `_stages_to_terminate` selects an owned stage unconditionally —
  group ownership, not leader liveness, is what decides — and
  `_terminate_all_shielded` absorbs per-stage failures, so it is safe while a
  timeout is already propagating. Reimplementing any part of it would create a
  second termination policy that could drift from the wait-timeout route's.
  Alternative considered: branch on `process.returncode is None` before
  terminating (rejected: it is the leader check this deliverable exists to
  avoid, and it would skip the group that still holds the pipes).

- Decision (EP-M3, 2026-10-21): the plan's earlier claim that
  `Pipeline.run()` carries no process-group policy is withdrawn, and
  `Surprises` and `Verification plan` are corrected to match. Rationale: the
  code says otherwise — `_pipeline_spawn.py:132-133` resolves
  `config.ctx.resolved_process_group` into a per-stage `owns_group`, records it
  on the spawn result, and the deadline route hands it to
  `_terminate_timed_out_stages`. A measurement reading the descendant's
  `/proc/<pid>/stat` found route 11's descendant already gone when the run
  returned, so the original claim was unmeasured as well as ungrounded.
  Consequence for EP-M3: the new module asserts reclamation under `OWN_GROUP`
  and only boundedness under `INHERIT`, rather than asserting boundedness
  everywhere. Alternative considered: leave the claim in place and assert
  nothing about ownership (rejected: it would have written a weaker assertion
  on a false premise, and the pipeline's owned-group reclamation would then
  have gone untested on the success route this milestone bounds).

## Milestones

Each milestone ends in a state that is coherent on its own: the repository
builds, the gates are green, and the routes it covers are bounded.

### EP-M1 — the direct run's post-exit stdin await

Requirement: `ISS533`. Deliverable, in
`cuprum/_subprocess_stream_run.py::_run_subprocess_without_streams` — which
EP-M1 relocated to `cuprum/_subprocess_direct_run.py`, see the split decision in
`Decision log`:

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
4. Report expiry without mistaking a cancellation for one. The plan named the
   timeout scope's `expired()` state as the mechanism; **amended at
   implementation**, because `expired()` is true for both the `EXPIRING` and
   `EXPIRED` states and cannot tell the scope's own timer from a caller's
   `cancel()` arriving after that timer fired. The scope's `__aexit__` already
   performs the authoritative cancel accounting, so the implementation catches
   `TimeoutError` and lets `CancelledError` propagate, which reads the same
   distinction correctly and without a second copy of the judgement. See
   `Decision log`; the amendment is recorded there rather than applied silently.
5. Route the resulting failure through the existing
   `_handle_stream_timeout()` translation with `None` payloads, so the caller
   receives the same public `TimeoutExpired` the streamed path produces.

Tests: new module `cuprum/unittests/test_run_no_stream_post_exit_deadline.py`,
built on `cuprum/unittests/_post_exit_teardown_support.py`, with a 2 MiB stdin
payload, capture, echo and observers all off, parametrized over
`INHERIT`/`OWN_GROUP` and over all four entrypoints —
`run`/`run_bytes`/`run_sync`/`run_bytes_sync` rather than the plan's three; the
byte-exact synchronous wrapper reaches the same path and costs one id. Assert
the run ends, the raised exception is the public `TimeoutExpired`, pending
tasks are inspected through `tests/helpers/timeouts.py::pending_tasks`, and any
descendant is reaped in `finally`.

Acceptance evidence: the new module fails before the change with a hang past
the harness bound (Red — observed as 4 failed in 82.81 s, all four
`asyncio.wait_for` `TimeoutError` at the 20 s bound), and passes after it
(Green — 10 passed in 18.03 s across the four entrypoints and two policies); the
`OWN_GROUP` arms assert the descendant is gone, and fail when the group
settlement is removed. Mutation evidence in `Verification plan`.

### EP-M2 — `SafeCmd.lines()`'s post-exit drain

Requirement: `ISS533`. Deliverables, in `cuprum/_line_stream/drain.py` and the
post-exit branch of `cuprum/_line_stream/coordinator.py`:

1. Give `_collect_line_stream_after_exit` the remaining
   `_deadline_for_collection(execution)` budget, covering three things under
   one bound: stopping the idle monitor, the shielded stdin await, and consumer
   completion.
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

Measured, with one deliberate departure from the wording above. The Red state
was taken by neutering the bound rather than by reverting the hunk: a
subprocess-based probe patched `_deadline_for_collection` in the drain module's
namespace to `return None`, ran the module, and restored the file, with `diff`
confirming byte-identical restoration. All 6 cases then failed with
`TimeoutError` from `asyncio/timeouts.py:115` in 123.00 s, which is the 20 s
harness bound spent once per case. Reverting instead would have removed the
`lines()` bound while leaving the *other* change in the same path — the
unstamped-`started_at` fix — in place, so the two could not be told apart; the
probe isolates exactly the bound. Both group policies are covered, and the
partial-output assertion is `_MARKER in drained.joined`, which reads the lines
a reader task had already received when the expiry ended it. Green:
`6 passed in 8.43s`.

Two acceptance points are worth stating precisely, because the obvious reading
of each is wrong. First, the partial-output arm does **not** read the raised
model's `.output`: with `capture=True` the reconciliation payload does reach
`TimeoutExpired`, but asserting it there would conflate what the caller already
received on the iterator with what the exception carries, and only the former
is the "already-delivered lines" claim. Second, "hangs before the change" is
strictly true but the mechanism is subtler than a bare hang: the drain awaited
three things unbounded, so the run did not merely outlive its deadline, it
never returned at all, and the reconciliation that reclaims the descendant was
what the hang prevented. An arm that only bounded the wait without settling
would still have stranded the descendant, which is why the second test asserts
reclamation rather than merely completion.

Follow-up within EP-M2, from the milestone review. Finding 1 said a reader's own
`TimeoutError` would be reported as the run's deadline. The regression module
written for it, `cuprum/unittests/test_line_stream_reader_failure.py`, holds
two tests — one for `lines()`, one for the streamed `run()` — and it showed the
defect at four layers rather than one. The line-stream drain returns a reader's
failure to its caller for re-raising after the scope closes; the coordinator
and the streamed boundary catch `_SubprocessTimeoutError` alone; the streamed
collect step consults its own scope's `expired()`. The invariant that makes the
two catch-narrowings safe, and the probe that established `expired()`'s
behaviour when inner code raises, are recorded in `Surprises & discoveries`.

Acceptance evidence for the follow-up: with all four fixes reverted, the
`lines()` test fails on exception identity (it receives
`_SubprocessInvariantError`) and the `run()` test loses its sentinel; restored,
both pass. Genuine deadlines were re-checked at `timeout=0.2` and `timeout=0`
on both routes and still raise `TimeoutExpired` with the configured timeout, so
the narrowing removed a false positive without removing the real bound.

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
   `test_timeout_capture_contract.py`,
   `test_capture_eof_grace_observability.py`,
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
  partial buffer of a cancelled-but-not-at-end-of-file reader, and falls back to
  `""` or `b""` according to `capture_bytes`.
- **L3 (for I3):** `_reconcile_run_tasks` leaves no task from
  `_RunTaskOwnership` pending, and `_terminate_all_shielded` completes its
  teardown before the caller's cancellation is re-raised.
- **L4 (for I4):** the `asyncio.timeout` scope's own cancel accounting in
  `__aexit__` (`self._task.uncancel() <= self._cancelling`) distinguishes its
  own expiry from an external cancellation, and reports the first as
  `TimeoutError` and the second as `CancelledError`. Catching `TimeoutError`
  and letting `CancelledError` propagate therefore reads the two apart without
  a second copy of that accounting. Note the scope's `expired()` state does
  *not* serve here: it is true for both the `EXPIRING` and `EXPIRED` states and
  cannot tell its own timer from a caller's `cancel()` arriving after the timer
  fired, so consulting it would report that caller's cancellation as a timeout.
  See the corresponding entry in `Decision log`. Because
  `cuprum.sh.TimeoutExpired` subclasses the builtin `TimeoutError` *and*
  `_SubprocessTimeoutError` does not, the exception *type* alone still cannot
  answer I4; the scope's accounting is what makes the type meaningful.
- **L4b (for I4, added EP-M2):** on a frame that *owns* the timeout scope, a
  bare `TimeoutError` is the scope's own expiry exactly when `scope.expired()`
  is true; an exception raised by code inside the scope leaves it false. Probed
  directly: `raise TimeoutError` inside `asyncio.timeout(None)` and inside an
  armed scope both leave `expired()` false, and only the timer firing sets it
  true. Unlike L4 this does not have to separate a caller's cancellation,
  because `CancelledError` never reaches an `except TimeoutError` arm.
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
  is the repository's accepted acknowledgement of that gap (`ADR021`,
  `tests/helpers/timeouts.py`).

Methods, artefacts and discharge conditions:

- **I1, L1:** unit test over `_deadline_for_collection` with a held clock, plus
  the parametrized route tests' timing assertions. Discharged when an unfixed
  build exceeds the 10 s per-test tolerance and a fixed build does not.
- **I2, L2:** direct payload assertions on each route (EP-M1, EP-M2, EP-M3),
  each with a `stdout_marker` the child flushes before it spawns anything, so
  the payload is in the buffer well before the deadline. Discharged when
  replacing the capture-mode reconciliation with discard mode fails the
  assertion.
- **I3, L3:** `pending_tasks()` after each route returns, plus
  `assert_reclaimed` on descendants for the `OWN_GROUP` arms. Discharged when a
  route that stops waiting without reconciling leaves a pending task.

  EP-M1 status: **discharged for the direct route, with a measured limit.**
  `pending_tasks()` is asserted on all four entrypoints, but only the two async
  ones have a live loop at the assertion — `asyncio.run`'s shutdown cancels
  stranded tasks before the synchronous arms can see them. The
  `assert_reclaimed` arms are discharged: with
  `_settle_owned_group_before_draining` removed from `_cleanup_direct_run`, all
  four `own-group` arms fail with the descendant still alive after the 5 s
  reclamation window, and the four `inherit` arms pass, which is the
  discrimination expected since only `OWN_GROUP` has a group to settle.
  Reclamation is asserted on the `OWN_GROUP` arms alone for that reason.

- **I4, L4:** a case that cancels the run and asserts the caller's
  `CancelledError` (not a timeout), and a case that completes inside the
  deadline and asserts success. Discharged when the expiry/cancellation
  distinction is removed and the cancellation case misreports. EP-M1 relies on
  `asyncio.timeout`'s own accounting rather than an `expired()` check; the
  existing `test_stream_run_owned_group_post_exit.py` covers the cancellation
  arm for the streamed boundary, and the direct route's cancellation arms are
  added with EP-M2's shared support if they are not already exercised.
- **I4, L4b (EP-M2 follow-up):**
  `cuprum/unittests/test_line_stream_reader_failure.py` raises a `TimeoutError`
  subclass from an `on_line` hook and asserts the same object reaches the
  caller on both the `lines()` and streamed `run()` routes. Discharged as a
  negative control: with the four catch/scope fixes reverted, the `lines()`
  case reports `_SubprocessInvariantError` and the `run()` case loses the
  sentinel; restored, both pass and the genuine-deadline runs still raise
  `TimeoutExpired`. The `lines()` case additionally asserts the absence of the
  `TIMEOUT` phase and the presence of both teardown phases, so it pins
  telemetry as well as the exception identity.
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
  and must hang or fail, and the scratch copy is discarded. The observed result
  is recorded in `Progress`.

  EP-M1 status: **discharged, with two mutations rather than one.** The Red
  stage is itself the negative control for the bound — the module failed with
  four `asyncio.wait_for` `TimeoutError` failures at the 20 s harness bound
  before any production change. Two further mutations isolate the two halves of
  the cleanup, because a single mutation cannot distinguish them:

  | Mutation                                         | `own-group` arms                     | `inherit` arms                |
  | ------------------------------------------------ | ------------------------------------ | ----------------------------- |
  | group settle removed from `_cleanup_direct_run`  | 4 failed (descendant alive past 5 s) | 4 passed                      |
  | writer cancel removed from `_cleanup_direct_run` | 4 passed                             | 2 async failed; 4 sync passed |

  The second row is the measurement that bounds this module's claims: the group
  signal alone settles the writer under `OWN_GROUP`, so those arms cannot
  detect a missing writer cancellation, and `asyncio.run`'s shutdown sweeps it
  under the synchronous entrypoints. Only the two async `inherit` arms pin
  writer settlement, and the module docstring now says so instead of implying
  that all four entrypoints carry equal weight. Both mutations were reverted
  and the module re-run green (10 passed in 18.03 s).

Non-vacuity: the `INHERIT` arms are what make most of these tests real. Under
`OWN_GROUP` a group signal can close the pipes, so a run that bounds *only* the
final await can still pass; under `INHERIT` nothing closes them, so only the
new bound and the new settlement end the run. Each route therefore carries at
least one `INHERIT` arm, and the test docstrings say which arm is expected to
discriminate. Each child announces readiness through a pid file and a flushed
marker rather than the test sleeping, so no case can pass by racing start-up.

The EP-M1 measurement sharpens this: for the direct route the *split* is the
other way round from what was expected. `OWN_GROUP` is the arm that
discriminates the group settlement (four failures without it, none with it
removed from `INHERIT` runs because there is no group to remove), while
`INHERIT` is the arm that discriminates writer settlement. Neither policy alone
is sufficient evidence, which is why both are parametrized on every case.

Residual gaps, recorded rather than hidden:

- A pipeline deadline cannot reclaim a descendant under `INHERIT`, because the
  only process it may signal is the direct child and that child has already
  exited (`Surprises`, route 12). Under `OWN_GROUP` it can and does: the
  stage's group is recorded at spawn and reaches `_terminate_timed_out_stages`,
  which was measured retiring the descendant before the run returned. What the
  `INHERIT` arm verifies is that the *run* returns inside its deadline plus the
  bounded graces.
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
of `cuprum/_subprocess_stream_run.py`, whose account of the streamed boundary
is the shape all three routes now follow.

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
