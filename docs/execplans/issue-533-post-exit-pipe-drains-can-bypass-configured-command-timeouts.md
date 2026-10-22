# Carry one deadline through every post-exit pipe drain

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS — EP-M1 (the direct run's post-exit stdin await), EP-M2 (the
`lines()` post-exit drain and the four-layer `TimeoutError` misclassification
it exposed), EP-M3 (the pipeline's post-exit collection) and EP-M4
(documentation and full validation) are all implemented, tested Red-then-Green,
and committed. The branch has been rebased three times. The first replay, onto
`origin/main` = `5da6d68fb`, is described below; the second, onto `origin/main`
= `9c9008351` ("Add bounded and file-backed capture policies per stream"),
replayed the same 34 commits against `main`'s per-stream capture policy API,
and its seven conflicts were resolved by hand (see `Progress` and
`Decision log`). The third, onto `origin/main` = `e2c7592b5` ("Reject remote
rsync operands in the core builder"), replayed 38 commits with no conflict:
that commit touches the builder library and only three documentation files this
branch also edits, and `range-diff` reports all 38 as patch-identical. The
final target-versus-head audit is clean: 52 commits ahead of `origin/main` =
`e2c7592b5` and none behind, no merge commits in the range, no deleted files,
and `git diff --check` reports nothing. That base is still the remote `main`,
so the comparison is against the current target rather than a stale local copy.
The candidate carries the split that brought `cuprum/_subprocess_execution.py`
back under the 400-line module cap. The PR is open as
[#652](https://github.com/leynos/cuprum/pull/652). The one review thread
CodeScene raised, a Bumpy Road Ahead on `_pipeline_payloads`, a function this
branch introduces, was actioned by flattening the function rather than
suppressing the marker (see `Decision log`).

Every gate round is recorded in `Progress` beside the head it ran at, and the
`Progress` record — not this paragraph — is the source of truth for which round
is the latest. Each round runs all six gates to exit zero with the tree clean
and HEAD unchanged before and after each one, and each log at
`/tmp/<gate>-cuprum-<branch>-<short-sha>.out` records the head, the tree-change
count, the exit code, and the elapsed time. The PR body cites the most recent
complete round recorded there rather than a pending table. Naming a log for its
head, and having the log record that head, is what keeps a filename and its
header from disagreeing: a plan-only edit therefore changes the head, so it is
certified by the round that follows it and not by the round that preceded it.
An earlier draft of this paragraph named a fixed head and had to be corrected
each time the plan advanced; pointing at the record avoids that.

CodeRabbit has reviewed this branch repeatedly, and each round's finding count
is recorded beside its adjudication in `Progress` rather than totalled here:
the EP-M2 milestone review returned ten, of which eight were applied and two
rejected; a second EP-M2 review returned three, one applied and two rejected;
the post-rebase review of `720bd430a` returned three, all applied; the
EP-M3/EP-M4 milestone review returned six, five applied and one rejected; the
PR #652 round returned three, two applied and one falsified; the review of
`80ab1f4fe` returned two, both applied; review `5609732903` returned seven, all
applied; the review of `deb83403a` returned ten, which fold into eight distinct
concerns, all applied, though one sub-proposal within them — flipping
`clock_origin` to a negative value — was rejected as wrong for this tree; and
the review of `a2e63ffbb` returned seven over 53 reviewed files, all still
valid and fixed in the commit that follows it; the review of `7459e3caf`
returned one over the same 53 files, also valid and fixed in the commit that
follows it; the review of `d9db359cf` returned one, valid and fixed in
`c09ae8e1e`, which is documentation only; and the review of `9d6a8428d`
returned three, all valid and fixed in `78e4d2aed`, with the round's drain-only
premise extended to the exit wait whose arm an ordinary run actually reaches.
Each rejection was re-verified against the tree at the head that carried it
rather than inherited from the round before. Each earlier head, including
`c7fe5341a` and `d95388a59`, remains recorded in the dated `Progress` entries;
none of them is the current candidate.

Two splits keep the source inside the module cap: the direct run in
`cuprum/_subprocess_direct_run.py`, and the direct run's result assembly in
`cuprum/_subprocess_outcome.py`.

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
s), the stdin close grace (`_CLOSE_GRACE_S`, 0.5 s), and — for a run that owns
its process group — the bounded one-second `_POST_KILL_SETTLEMENT_S` wait after
the group is forcibly killed.

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
  `_deadline_for_collection` helper. That helper was defined in
  `cuprum/_subprocess_wait.py` when this plan was written and now lives in
  `cuprum/_subprocess_deadline.py`, where `main` put it beside the exit wait it
  is measured against; `cuprum._subprocess_wait` still re-exports it from its
  `__all__` for the callers that reach it that way. This branch adopts that
  module as the single owner rather than keeping a copy — the remainder is only
  meaningful against the reference the exit wait was scheduled from, which is
  the reason `main` gives for the move.
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
  unconfigured timeout, a possibly negative remainder when the exit wait
  already overran, and its clock read through
  `execution.observation.monotonic_clock()` rather than `loop.time()`, so a
  test can hold the deadline still. It is `main`'s definition in
  `cuprum/_subprocess_deadline.py`, adopted unchanged; this branch adds callers
  and no behaviour. Its `None` means *no deadline configured*, which is not the
  same as *no bound owed*: a caller that receives `None` must still choose a
  bound of its own rather than passing `None` on to `asyncio.timeout`, which
  would mean no bound at all. That distinction is what the review of the
  rebased head found this branch had collapsed; see `Surprises & discoveries`.
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
  EP-M3 — `cuprum/_pipeline_collect.py`, `cuprum/_pipeline_stream_results.py`,
  and the split-out `cuprum/_pipeline_settlement.py`, together with the
  `cuprum/_pipeline_records.py` record they exchange. Every one of these is a
  private module; the escalation is about breadth, not about interface. The
  breach itself is recorded in `Decision log` with the alternatives. Four
  further files were needed by gates rather than by the design —
  `cuprum/_stream_drain.py`, `Makefile`, the maturin wheel snapshot, and
  `pyproject.toml` — and they are recorded in the same place. A file outside
  this list, or any change to a public signature, is still a stop-and-escalate
  event.
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

- Risk: replacing an unbounded await with `asyncio.timeout(remaining)` *removes*
  a bound elsewhere, because `remaining` is `None` for a run that configured no
  deadline and `asyncio.timeout(None)` arms nothing. The branch then hangs a
  caller that `main` returned for. Severity: high. Likelihood: high — this is
  not hypothetical: it happened on this branch, was caught by the review of the
  rebased head rather than by any test, and survived a full green gate round.
  Mitigation: the `None` case is handled explicitly and pinned by
  `test_a_run_without_a_timeout_still_bounds_its_post_exit_writer_wait`, whose
  absence from the suite was the actual cause — every deadline test configured
  a deadline, so no case could observe the deletion. The general rule for any
  later narrowing of a shared helper: enumerate the helper's existing callers
  and ask what each one becomes when the new parameter is absent.

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
- [x] (2026-10-21) EP-M3 production code and its regression module.
  `_bounded_payloads` in `cuprum/_pipeline_stream_results.py`,
  `cuprum/_pipeline_settlement.py` (new), `cuprum/_pipeline_records.py`
  (`_PipelineOutputGather`), and the shared expiry handler in
  `cuprum/_pipeline_collect.py`. `test_pipeline_post_exit_deadline.py`: 16
  passed, 8 skipped (the Rust arms) in 14.24 s; the four modules run together —
  the new one plus `test_pipeline_collect`, `test_pipeline_stream_results` and
  `test_pipeline_timeouts` — report 40 passed, 8 skipped in 14.56 s.
- [x] (2026-10-21) EP-M3 — three consecutive clean local runs of
  `test_pipeline_post_exit_deadline.py` at `25a776a5c` with the EP-M3 tree in
  place: `16 passed, 8 skipped` in 14.26 s, 14.24 s and 14.24 s
  (`/tmp/test-cuprum-533-repeats.out`). `make check-fmt` then passed at the
  same HEAD and tree after one `make fmt` pass re-wrapped the plan's
  Decision-log edits; the log is
  `/tmp/check-fmt-cuprum-issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts.out`
  and records its own HEAD and tree state. The gate and the milestone review
  follow.
- [x] (2026-10-21) EP-M3 gate round 1, red on the nose duplication gate inside
  `make lint` (`/tmp/lint-…-epm3.out`, exit 2 at `Makefile:528`) with two
  unsuppressed families. Both are cleared rather than suppressed wholesale. The
  copy-paste pair — the pending-reader accounting and
  `capture_eof_grace_expired` reporting, restated in
  `_await_pipeline_eof_grace` — is now the shared
  `_stream_drain._report_eof_grace_expiry`, which takes an arbitrary-length
  reader tuple, so the two drains report one rule from one place while each
  keeps its own wait. The TYPE_CHECKING-block family gained this change's two
  modules and its reasoned allow entry was widened from thirty to thirty-two
  members. A standalone `make duplication` then passed:
  `59 allowed by reasoned exceptions`. Focused re-run of the four affected
  modules: 34 passed, 8 skipped in 14.53 s
  (`/tmp/pytest-cuprum-533-drain-shared.out`).
- [x] (2026-10-21) EP-M4 documentation. `docs/users-guide.md`: the three-routes
  sentence and the overshoot list (cancel grace, 0.25 s EOF window, the 0.5 s
  stdin close for a run given `stdin` data, and the one-second owned-group
  settlement), the `lines()` backpressure and delivered-lines-stay-delivered
  paragraphs, the pipeline-timeouts paragraph, and pointers from "Handle a slow
  command" and "Stream lines as they arrive". `docs/cuprum-design.md`: the
  post-exit-boundary bullet, a `_pipeline_settlement.py` entry in the §8.1.5
  module-boundary list, and the bounded pipeline collection passage. Reading
  the shipped code to check that last passage corrected a drafted claim: the
  direct run measures its remainder from `execution.started_at`, but
  `_collect_pipeline_inputs` stamps `time.monotonic() + timeout` at the start
  of collection, so spawn time is not deducted for a pipeline. Both documents
  now say so instead of implying one shared reference. ADR-021 gained an
  appended 2026-10-21 addendum rather than a Decision-log note: the settlement
  it introduced is *unreachable* without the bound, which is a substantive
  addition to that ADR's reasoning rather than a restatement. `make fmt`
  re-wrapped both edited docs, and `make check-fmt` then exited 0.
- [x] (2026-10-21) EP-M4 named focused modules: 85 passed, 8 skipped in 39.31 s
  (`/tmp/test-cuprum-533-epm4-focused.out`) across
  `test_timeout_capture_contract`, `test_capture_eof_grace_observability`,
  `test_stream_run_owned_group_deadline`,
  `test_stream_run_owned_group_post_exit`, `test_pipeline_timeouts`,
  `test_safe_cmd_lines`, `test_line_stream_terminal_outcome`,
  `test_pipeline_post_exit_deadline`, `test_pipeline_collect` and
  `test_pipeline_stream_results`. The 8 skips are the Rust-parametrized arms,
  which this host cannot run because the extension is not built.
- [x] (2026-10-21) Full gate round on the EP-M3/EP-M4 tree
  (`/tmp/{check-fmt,lint,typecheck,test}-…-epm3c.out`): `check-fmt` passed in 1
  s, `lint` in 116 s with the duplication gate green at 59 reasoned exceptions,
  `typecheck` in 1 s. `make test` then failed `test-python` with
  `2 failed, 3310 passed, 101 skipped` in 244.97 s, and `test-rust` therefore
  never ran. Both failures are consequences of the change, not of the
  environment, and both name the same omission from a declared inventory.
  `test_extension_build_contract.py:246` derives extension-gated modules from
  the suite and found the new `test_pipeline_post_exit_deadline.py` was absent
  from `EXTENSION_TEST_TARGETS`, so its eight Rust-backend arms would never run
  in the one job that builds the extension; `test_maturin_build.py:185` found
  the wheel snapshot one entry short of the shipped file list. Fixed by adding
  the module to `Makefile`'s `EXTENSION_TEST_TARGETS` beside the other gated
  modules and by adding the single `cuprum/_pipeline_settlement.py` line to
  `cuprum/unittests/__snapshots__/test_maturin_build.ambr` in sorted position,
  rather than by regenerating the snapshot wholesale. Focused re-run: 16 passed
  in 4.97 s (`/tmp/focused-fixes-cuprum-….out`), with the snapshot report
  showing `1 snapshot passed`.
- [x] (2026-10-21) Full gate round 2 at `25a776a5c` with the inventory fixes
  (`/tmp/{check-fmt,lint,typecheck,test}-…-epm3d.out`): `check-fmt` passed in 1
  s, `typecheck` in 1 s, and **`make test` passed in 357 s — python 3312 passed
  / 101 skipped in 233.57 s, nextest 167/167, doctests ok — so `test-rust` ran
  for the first time at this HEAD.** Both previously-red tests are green, and
  the new `test_pipeline_post_exit_deadline.py` ran 16 passed. `make lint` was
  the only red gate, and it failed at `spelling` on prose in this plan: the
  British-`-ise` form of "recognize", written in this plan (`…epm3d.out`, exit
  2 at `Makefile:632`), where en-GB-oxendict requires `-ize`. The word was in
  the unstaged docs, so the staged code tree was never affected, but the gate
  sees the whole working tree. Recording the mistake reproduces it: the entry
  first quoted the misspelling verbatim, in backticks, and `make markdownlint`
  failed on this plan a second time. The `typos` gate reads fenced and inline
  code as well as prose, so a misspelling cannot be cited as an example — it
  has to be described. Fixed, then `make lint` re-run clean
  (`/tmp/lint-…-epm3e.out`, exit 0) — this second run is what certifies
  `yamllint` and `actionlint`, since the first aborted before reaching them.
- [x] (2026-10-21) EP-M3/EP-M4 milestone review
      (`coderabbit review --agent --committed --base-commit ed1d47e87`). Six
      findings. Five were valid and are
      applied. Two `OWN_GROUP` tests in `test_pipeline_post_exit_deadline.py`
      now reap their descendant in `finally` through the shared
      `_post_exit_teardown_support.reap_descendant`, with the whole body inside
      the `try` so a run that raises still reaches the reaper; the module's
      four unused parametrization constants are gone and its `_context`
      docstring no longer mentions a timeout the function does not take; the
      module docstring now describes the four sibling cases it actually holds
      rather than a parametrization it does not perform; the plan's two
      first-person passages and one second-person passage are impersonal; the
      plan's status line and its EP-M3 `_await_pipeline_eof_grace` decision now
      match the shipped mechanism. The sixth finding is the
      `_spawn_stdin_writer` claim for the third time — see `Decision log`.
      Focused re-run: `test_pipeline_post_exit_deadline.py` 16 passed, 8
      skipped in 14.26 s (`/tmp/test-cuprum-issue-533-post-exit-pipe-drains-can-bypass-configured-command-timeouts-epm3-review2.out`).
- [ ] Gate each commit through `scrutineer`; request `coderabbit review --agent`
  after each milestone.
- [x] (2026-10-21) Push and open the draft PR:
      [#652](https://github.com/leynos/cuprum/pull/652),
  draft, head `8936c9bfa`, with the Lody session link and `Fixes #533`.
- [x] (2026-10-21) Rebase onto `origin/main` = `5da6d68fb`. Old head
  `bf465eb5e`; old base `c38459100`; all 14 commits replayed, none dropped or
  emptied. `range-diff` accounts for every commit: 9 identical, 5 rewritten
  only where `main` relocated the module a function lived in. Recovery refs
  `refs/recovery/533-pre-rebase-{old-head,old-base,target}` retained.
- [x] (2026-10-21) Post-rebase adjudication of the two contract collisions the
  rebase exposed. `main` had, since the branch's base, landed the stdin-plan
  split and simultaneously given the *direct* run a fixed-window settle. Both
  are recorded in `Decision log`: the four-argument
  `_spawn_stdin_writer(process, plan, codec, observation)` is now the real
  contract (superseding three recorded rejections), and the direct path keeps
  this branch's remaining-deadline bound rather than adopting `main`'s fixed
  window, because reporting the child's exit code over undelivered input is the
  defect #533 exists to fix.
- [x] (2026-10-21) Tests re-expressed for the adjudicated contract.
  `test_stdin_settle_cancellation::test_a_child_that_never_reads_does_not_wedge_the_run`
  asserted `main`'s contract (exit code 0 on a parked 2 MiB writer under a 10
  s timeout); it is now
  `test_a_child_that_never_reads_ends_the_run_on_its_deadline` and asserts the
  public `TimeoutExpired` carrying the configured timeout. The shared harness
  `_install_post_exit_probe` patched only
  `cuprum._subprocess_stream_run._settle_stdin_writer`, which the rebased
  direct path no longer calls; it now patches
  `cuprum._subprocess_direct_run._await_writer_within_deadline` as well, and
  its docstring no longer claims both paths live in one module. Both modules
  green: `test_stdin_cooperative_cleanup.py` 2 passed in 1.07 s,
  `test_stdin_settle_cancellation.py` 2 passed in 10.91 s.
- [x] (2026-10-21) `cuprum/_subprocess_stdin_reclaim.py`'s module docstring now
  names the direct run as the one caller that awaits its writer without
  settling it, and states why that exception is considered rather than an
  omission.
- [x] (2026-10-21) First post-rebase gate round at `2ae38af08`, red on
  `make lint`: `ruff`'s `implicit-return-value` at
  `cuprum/_line_stream/drain.py:260`, a bare `return` in `_await_readers`,
  whose contract is `BaseException | None`. The rule is part of the 0.16 rule
  set the rebase brought in; the branch's own pre-rebase lint rounds predate
  it. Fixed by writing the early exit as `return None`, matching every other
  completing exit in the same function (`905a49569`). The rest of that round
  mattered too: `make lint` aborts its recipe on the first failing stage, so
  interrogate, DF12 pylint, ambrleaks, Skylos, the nose duplication gate,
  Clippy, Whitaker, spelling, yamllint and actionlint were all left
  **unobserved** rather than passing. Only the re-run below certifies them.
- [x] (2026-10-21) Full gate round at `905a49569`, all six green, each log
  written for this head: `make check-fmt`, `make test`, `make typecheck`,
  `make lint`, `make markdownlint`, `make nixie`. `make test`: 22 pytest
  sessions totalling 5219 passed, 122 skipped, 0 failed; nextest 167/167;
  doctests clean. `make lint` exited 0 with all eleven stages observed — ruff,
  interrogate (100.0%), classic pylint (10.00/10, both passes), DF12 pylint
  (10.00/10 on CPython 3.14.7), ambrleaks, Skylos, the nose duplication gate,
  Clippy, Whitaker, spelling, yamllint, actionlint.
- [x] (2026-10-21) Post-rebase review of `720bd430a`
  (`coderabbit review --agent --base main`). Three findings, all applied. The
  major one is real and was reproduced before it was believed:
  `_await_writer_within_deadline` took its bound from
  `_deadline_for_collection(execution)`, which is `None` for a run with no
  `timeout=`, and `asyncio.timeout(None)` is not a bound at all — so the branch
  had *removed* the settle window `main` still applies there, and a direct run
  with no timeout hung where `main` returned. A standalone probe against the
  unfixed tree reported `HANG: still running after 20.60s`; against the fixed
  tree, `RESULT: run returned after 0.80s`. The `None` branch now calls the
  shared `_settle_stdin_writer`, and
  `test_a_run_without_a_timeout_still_bounds_its_post_exit_writer_wait` pins
  it; deleting the branch back out fails that test in 20.75 s with the intended
  message, and restoring it passes in 0.87 s. The two test findings were also
  valid: the `uncaptured` pipeline case now reaps inside the `finally` from the
  first statement, and the settle-cancellation cases reap their grandchild and
  run under a 1.0 s deadline, which brings that module from ~10 s to 1.86 s for
  two tests. Module docstrings in `_subprocess_direct_run.py` and
  `_subprocess_stdin_reclaim.py` and item 6 of the developers' guide
  walkthrough now state the two bounds and which run each belongs to. Focused
  re-runs: `test_stdin_settle_cancellation.py` 2 passed in 1.86 s,
  `test_run_no_stream_post_exit_deadline.py` 12 passed in 19.62 s, and the five
  touched suites 40 passed / 8 skipped in 44.12 s.
- [x] (2026-10-21) Committed the three fixes as `61856ebd7`. A post-`fmt`
  re-run of the three touched modules — `test_stdin_settle_cancellation.py`,
  `test_run_no_stream_post_exit_deadline.py` and
  `test_pipeline_post_exit_deadline.py` — reported 30 passed, 8 skipped in
  35.68 s at `720bd430a` with the fixed tree
  (`/tmp/focused-postfmt-e82aaf4b-38bb-456f-9cd2-e0b8abf09619.out`). That run
  predates the commit, so it establishes the content and not the commit; the
  gate round for `61856ebd7` and its documentation commit is what the PR
  certifies.
- [x] (2026-10-21) The failing
      `Extension-gated tests (Windows Python/Rust boundary)` job at `720bd430a`
      read: 24 of the 24 collected cases in
      `cuprum/unittests/test_pipeline_post_exit_deadline.py` failed, 18 with
      `ValueError: ProcessGroupPolicy.OWN_GROUP requires POSIX process groups`
      and six with `AttributeError: module 'signal' has no attribute 'SIGKILL'`
      (`/tmp/gh533-windows-ext-job.log`, run `39559707092`, job
      `119087730814`). The module is declared in `EXTENSION_TEST_TARGETS` —
      this branch's own `669e44529` put it there — so the Windows arm collects
      it, and it had no platform skip. The skip was added to the module
      (`pytestmark`) and the `EXTENSION_TEST_TARGETS` comment now says a listed
      module must carry its own. Verified at `7729c0c34` with the change in the
      tree: 16 passed, 8 skipped in 14.25 s, and `--collect-only` still reports
      24 cases, so the mark hides no parametrization. Committed as `6bb6b778b`.
      The required-check list for `main` names only
      `Extension-gated tests (Python/Rust boundary)`, so this job is advisory —
      but a red advisory check is a claim a reviewer has to discount, and the
      failure was a real gap in the branch's own CI declaration rather than a
      flake.
- [x] (2026-10-21) The `CodeScene Code Health Review (main)` check failed at
      `50c4536a4` with three gates red and four files flagged. Each finding was
      repaired rather than adjudicated, because a newly introduced violation
      fails the gate regardless of the check being advisory (PR #466's
      correction), and three of the four were honest simplifications:
      `_await_direct_completion` read `execution.consumes_stdout or
      execution.consumes_stderr` twice and the second *BoolOp* pushed it to
      cc = 9; `_collect_stream_output` and `_collect_line_stream_after_exit`
      each carried docstring prose duplicating their module docstrings; the
      deadline test module read five arguments at once and held a 70-line test.
      The two prose trims were paired with one extraction each — the expiry arm
      of `_collect_line_stream_after_exit` became `_expire_line_stream`, and
      the two-cancellation choreography became
      `cancel_twice_across_direct_cleanup` — so the shorter bodies are a
      consequence of a named responsibility rather than of deleted explanation.
      Local `CS_DISABLE_VERSION_CHECK=1 cs check` now reports 10.00 on all five
      touched files, and it reproduced CI's wordings exactly on the earlier
      round, so it is the iteration oracle; the check-run at the pushed head
      remains the verdict to cite.
- [x] (2026-10-21) The extracted test helper was proved non-vacuous before
      committing. Replacing the production `_shielded_cleanup(settle_writer_
      then_group())` with two sequential awaits made
      `test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group`
      fail with the intended message — "a second cancellation arriving during
      the writer's reclamation must not cost the run its group settlement" —
      so moving the choreography out of the test body did not cost the case its
      teeth. The mutation was reverted and `git diff --stat` on
      `cuprum/_subprocess_direct_run.py` is empty. `--collect-only` still
      reports 12 cases, with the eight entrypoint×policy cases now carrying
      joined ids such as `[run-own-group]`.
- [x] (2026-10-21) The full gate round was run against `4d1b262db` and
      certified, with every log stamped `HEAD before`/`HEAD after` at that
      commit and zero tracked-tree changes on both stamps
      (`/tmp/gh533-gates/*-4d1b262db.log`): `make check-fmt`, `make typecheck`,
      `make test`, `make lint`, `make markdownlint` and `make nixie`, all exit
      0. `make test` reported 22 pytest sessions, 5220 passed, 122 skipped,
      0 failed, then nextest 167/167 with no failures and the Rust doctests
      clean. The first attempt at `make lint` (`b033e8cab`) failed
      `python-lint`'s `docstring-extraneous-exception` check: the
      `_expire_line_stream` docstring had a `Raises` section naming
      `_SubprocessTimeoutError`, which the helper does not raise itself — the
      delegate `_handle_stream_timeout` does. The fix states the same fact in
      prose and keeps the `NoReturn` rationale; it was committed as
      `4d1b262db` and the whole round re-run there, because a gate cannot
      certify a tree that changes under it.
- [x] (2026-10-21) Force-pushed `4d1b262db` over SSH with the lease bound to
      the remote head recorded immediately beforehand (`50c4536a4`), refreshed
      the tracking ref with a leading `+`, and confirmed the remote head with
      `git ls-remote` against `git rev-parse HEAD`; the branch's upstream now
      names `origin/`. The PR's CI is running at that head.
- [x] (2026-10-22) A CodeRabbit review round on PR #652 raised three
      findings, each adjudicated against the current tree rather than against
      the wording of the finding.
- [x] (2026-10-22) Finding 1 (start reference): applied the half that was
      real. `_start_line_stream_run` now samples `started_at` from
      `execution.observation.monotonic_clock()` instead of the module-level
      `perf_counter`, matching `_sample_run_start` on the direct path, so the
      post-exit deadline subtracts a reading taken from the clock it subtracts
      from. Committed as `192d162a3` with a guard test that injects both
      clocks; the guard was mutation-verified in both collection orders (see
      the Surprises section for why the order matters).
- [x] (2026-10-22) Finding 1 (line stamps): applied, after a first-round
      refusal that was wrong. The finding asked that the observation clock be
      threaded through `_LineEmissionContext` into `_stamp_line`.
      `_stamp_line` is the *only* site that constructs a `LineEvent` in
      production code, so the earlier answer — that the queue sink stamps it —
      was false; the queue relays the already-stamped event. Corrected in the
      same change that threads the clock: all four routes now read
      `observation.monotonic_clock` for the start reference, the per-line
      stamp, and the deadline, and each route has a mutation-verified guard.
- [x] (2026-10-22) Finding 2: falsified as stated. It proposed calling
      `_settle_owned_group_before_draining` above the direct run's writer
      await, on the reading that the expiry route does not settle the group.
      It does: `_cleanup_direct_run` cancels the writer and *then* settles the
      group under one `_shielded_cleanup`. The claim came from the helper's
      own docstring, which was wrong about both its routes and the direct
      path's ordering; corrected in `70cf3564e`, with no production change.
- [x] (2026-10-22) Finding 3: applied. The users' guide, the design document,
      and ADR-021 all attributed the one-second `_POST_KILL_SETTLEMENT_S` wait
      to pipelines alone; it is owed by any run that owns its group.
      Committed as `0dabd1dc7`, with a new ADR addendum rather than an edit to
      the accepted one.
- [x] (2026-10-22) The duplication gate red at `ac8744d1c` is resolved without
      widening any exception. First moved the stage path's context
      construction into a local helper; that fixed the reported family but
      pulled the module's `if typ.TYPE_CHECKING:` block into the
      repository-wide import family. A probe that stripped only the three new
      type-only imports passed (`66 allowed by reasoned exceptions`), proving
      the block was outside the family before this branch. The construction
      then moved to `cuprum/_line_callbacks.py` as
      `_compose_observation_line_callback`, sharing it at the layer that owns
      the stamping seam. Committed as `4f45f7f37`; the gate reports
      `66 allowed by reasoned exceptions`.
- [x] (2026-10-22) The prep-cost spy in
      `test_line_event_emission_prep_cost.py` was retargeted from the stage
      module's now-absent `_compose_line_callbacks` binding to
      `_line_callbacks._compose_line_callbacks`, where the shared seam lives.
      Mutating the helper to compose a local closure makes the two pipeline
      cases fail, so the pin still bites.
- [x] (2026-10-22) Rebased onto a second, newer `origin/main` at
      `9c9008351` ("Add bounded and file-backed capture policies per stream
      (#443) (#635)"), because `main` had advanced past the `5da6d68fb` this
      branch was first replayed onto. Old head `3a17dc4fa`; old base
      `5da6d68fb`; all 34 commits replayed, none dropped or emptied.
      Recovery refs `refs/recovery/20261022-gh533/{old-head,old-base,target,
      remote-branch-head}` retained. New head `2468a3cc5`.
- [x] (2026-10-22) Seven replay conflicts resolved by hand, each by reading
      the replayed commit, its parent and the accumulated result rather than
      by choosing a side: replays 12 (`_pipeline_settlement.py`,
      `_pipeline_stream_results.py`, `pyproject.toml`,
      `test_pipeline_stream_results.py`), 16 (`_subprocess_stream_run.py`,
      `_stdin_run_cancellation_support.py`), 22 (`_line_stream/drain.py`,
      `_subprocess_execution.py`), 23 and 28 and 30
      (`_line_stream/drain.py`, `_subprocess_wait.py`). See `Decision log`
      for what each resolution had to preserve.
- [x] (2026-10-22) The rebase was run with
      `-c core.attributesFile=/dev/null`, repeated on every
      `git rebase --continue`, because the host's global Git attributes select
      a `weave` merge driver for `*.py`. See `Surprises & discoveries`.
- [x] (2026-10-22) `Decision log` reconciled with the new base: the pre-rebase
      helper-home decision marked **superseded** (the split stands, the owner
      does not — `main`'s ADR-007 moved the helper to
      `cuprum/_subprocess_stream_post_exit.py`), and four new entries recorded
      for the capture-policy vocabulary, the `_PipelineOutputs` per-stream
      correction, and the run module's lost `_settle_stdin_writer` binding.
- [x] (2026-10-22) The gate round on `4952dd5dd` came back red on one genuine
      finding and nothing else: `make lint` stopped at `pylint-classic` with
      `C0302: Too many lines in module (425/400)` for
      `cuprum/_subprocess_execution.py`. The module was exactly at 400 lines
      on the target head, and this branch's post-exit deadline work is what
      took it over. Because `python-lint`'s remaining prerequisites abort
      before the recipe body runs, ruff, interrogate, both pylint passes'
      second halves, ambrleaks,
      Skylos, the duplication gate, Clippy, Whitaker, spelling, yamllint and
      actionlint were all unobserved by that round — the finding is the only
      thing it certified, and the rest had to be re-run rather than assumed.
- [x] (2026-10-22) Resolved by splitting the *result* half of the module out,
      not by suppressing the finding: `_DirectCompletion`,
      `_build_command_result`, `_shared_result_fields` and the relay-fallback
      flattening moved to `cuprum/_subprocess_outcome.py`, on the boundary the
      module docstring already drew (orchestration owns *when* a run settles,
      the new module owns *what* it reports). `_subprocess_execution` is 273
      lines and re-exports the two names its own importers reach for.
      `_process_handle/coordinator.py` and `_line_stream/coordinator.py` are
      untouched. See `Decision log`.
- [x] (2026-10-22) The duplication gate then paired the new module's
      `TYPE_CHECKING` prologue with the 45-module import family. The existing
      reasoned exception gained the member and a record of the widening: same
      idiom, one more declarer, not copy-paste to extract.
- [x] (2026-10-22) `docs/developers-guide.md` corrected in two places: the
      stale claim that `_settle_owned_group_before_draining` lives in
      `cuprum/_subprocess_wait.py` (ADR-007 moved it to
      `cuprum/_subprocess_stream_post_exit.py`, and three shapes share it), and
      a new paragraph placing `cuprum/_subprocess_outcome.py` beside the other
      two module splits.
- [x] (2026-10-22) The three open CodeRabbit threads on `da5cbeaf` answered
      and resolved. The clock finding was applied earlier on this branch
      (`spawn.py:103`, `_line_callbacks.py:103` and `:302`); the
      settle-before-writer finding was rejected with the success-path contract
      from the direct-run module and the ADR-021 addendum; the settlement
      allowance had already been widened by `15c63a51a`, and the one
      actionable part left — the comma before an essential `because` — was
      fixed in `e884bdf0b`.
- [x] (2026-10-22) Rebased onto `origin/main` = `e2c7592b5`, which advanced
      while the round above was running. All 38 commits replayed with no
      conflict and `range-diff` reports every one patch-identical; the new
      base touches the builder library plus three documentation files this
      branch also edits, and none of its hunks neighbours this branch's. The
      in-flight round was stopped rather than allowed to certify a tree the
      rebase replaced.
- [x] (2026-10-22) Full gate round on `7caf413e3`, five of six gates green:
      `check-fmt` (1082 ruff files, 100 Markdown files unchanged), `typecheck`
      (both `ty` runs), `lint` (every sub-gate reached: pylint 10.00/10 twice,
      interrogate 100%, DF12 10.00/10, ambrleaks, Skylos, duplication with 68
      reasoned exceptions, rustdoc + Clippy, Whitaker, yamllint, actionlint),
      `markdownlint` (0 issues, spelling included), and `nixie`. `make test`
      failed on `test_maturin_wheel_build_snapshot`: the new
      `cuprum/_subprocess_outcome.py` is packaged but was missing from the
      wheel manifest. The Rust suite and the doctest step never ran, because
      pytest is a prerequisite of `test-rust` and the recipe aborted.
- [x] (2026-10-22) Adjudicated the snapshot's second diff line before
      touching it. The elided diff also showed `- cuprum/sh/pipeline_output.py`,
      which reads as a dropped module — and `cuprum/sh/pipeline.py` imports
      three names from it, so a wheel without it would fail on import. A fresh
      `maturin build` of this tree contains that file; comparing the built
      wheel's entry set against the snapshot's shows exactly one difference in
      either direction, `_subprocess_outcome.py`, and nothing missing from the
      wheel. So that line was a display shift in the elided middle of the
      diff, the same artefact the maturin memory records. The snapshot got a
      one-line sorted insert (223 entries, still sorted and unique), and
      `test_maturin_build.py` passes 6/6 with "1 snapshot passed".
- [x] (2026-10-22) Full gate round on `d95388a59`, all six green, HEAD and the
      tracked-tree change count identical before and after every gate. `make
      test` finally reached what the previous round could not: pytest 4233
      passed and 105 skipped across the collected suite, `test_maturin_build`
      passing at last with 29 snapshots passed, nextest 167/167 from 7
      binaries, and the doctest step, whose three entries are deliberate
      `.rs`-line ignores. `make lint` reached every chained sub-gate with none
      aborting early, and the duplication gate still reports the same 68
      allowed findings. The round recorded the two things the previous one had
      to leave open, so the evidence for the candidate is now complete.
- [x] (2026-10-22) Re-ran the whole round on `c7fe5341a`, the head the push
      would carry, rather than citing the `d95388a59` logs for it. Only a
      plan document had changed since the previous certified head, but a gate
      log names exactly one revision and the previous set named a different
      one. All six green, HEAD and the tracked-tree change count identical
      before and after every gate.
- [x] (2026-10-22) Audited the final target-versus-head diff before publishing:
      43 commits ahead of `origin/main` = `e2c7592b5` and none behind, no
      merge commits in `origin/main..HEAD`, no deleted files under any path
      filter, `git diff --check` silent. Pushed over SSH with
      `--force-with-lease` bound to the previously recorded remote head
      `da5cbeaf7`, refreshed the remote-tracking ref through an explicit
      leading-`+` refspec, confirmed `ls-remote` and `rev-parse HEAD` agree on
      `c7fe5341a`, and set the branch's upstream to `origin/`. The PR body's
      gate table now carries this round's results instead of a pending row per
      gate.
- [x] (2026-10-22) Adjudicated the PR's one unresolved review thread before
      requesting another review. It is CodeScene's Bumpy Road Ahead on
      `_pipeline_payloads` in `cuprum/_pipeline_stream_results.py`, which the
      thread calls a *new* issue — confirmed by checking that
      `origin/main`'s copy of the module has no such function. Two nested
      conditional blocks per function is the threshold, and the slot loop
      nested one inside another. Flattened into `_slot_payload` plus a frozen
      `_SlotContext` and a `_slot_stream` predicate; ruff clean, and the three
      deadline test modules pass 34 passed / 8 skipped. The decision and its
      head are recorded together so the gate round can run on one head with no
      plan edit after it.
- [x] (2026-10-22) Full gate round on `58971f7ee`, the head carrying the
      `_pipeline_payloads` fix. All six gates green, the log suffix is
      `-58971f7ee.out`, and each log records its own `HEAD before`/`HEAD
      after` with no tracked-tree change.
- [x] (2026-10-22) Adjudicated the second CodeRabbit review, the
      `CHANGES_REQUESTED` at `2026-10-22T04:32:30Z` (review `5609291319`).
      CodeRabbit accepted all three verdicts, withdrew the group-before-writer
      finding as based on a wrong premise, and confirmed no further changes
      were needed. The reply is `pull/652#issuecomment-6332850782`; a
      correction at `#issuecomment-6332863278` fixes a mistake in it — the
      review was attached to `58971f7ee`, the current head, and the
      `5da6d68fb..da5cbeaf7` range quoted in the reply belonged to the
      earlier review.
- [x] (2026-10-22) Requested a full review of `80ab1f4fe`; CodeRabbit returned
      two fresh, test-only findings, both genuine. Fixed in `3c438cd6f`. The
      cleanup race it caught is the more valuable of the two: the child in
      `test_stdin_settle_cancellation.py` spawns its grandchild and exits
      without waiting, so a single `recorded_descendant` read could return
      `None` and leave `reap_descendant` with nothing to kill, parking a
      sleeper on the host for its whole lifetime. The read now waits via
      `asyncio.to_thread(wait_for_pid_file, ...)`, the pattern the sibling
      owned-group tests already use. The other finding added a failure message
      to the payload-alignment assertion. Reply at `#issuecomment-6333121153`.
- [x] (2026-10-22) Adjudicated the third CodeRabbit review, review
      `5609732903` at `2026-10-22T05:41:30Z`, which left seven unresolved
      threads. All seven were still valid against the code and are fixed in
      `c5501c33e`; none was disputed or skipped. Two were test-quality
      findings that had let the tests pass without witnessing the behaviour
      they name. In `test_pipeline_post_exit_deadline.py`, the success route
      asserted a bounded return but never asserted the descendant was
      reclaimed, and its `RUN_BUDGET_S` was a harness bound of 12 s rather
      than a measurement of settlement — a regression back to the old
      unbounded drain could take 11 s and still pass. Replaced with a real
      elapsed assertion against `DEADLINE_S + SETTLE_ALLOWANCE_S` (3 s) plus
      `wait_for_pid_file` / `process_is_running` / `reap_descendant`; the
      measured settlement is about 1.5 s, and the two module-level asserts
      that encoded the old bound were deleted rather than retuned. In
      `test_stdin_settle_cancellation.py`, the pid file was read once after
      the child exited. The child spawns its grandchild and exits without
      waiting, so that read could see the file before the write and hand
      `reap_descendant` a `None`; it now awaits `wait_for_pid_file` through
      `asyncio.to_thread`, as its siblings do. The three code findings were
      all genuine and none was cosmetic.
      `_await_consumers` attributed simultaneous reader failures by set
      iteration, so the error a caller read depended on task hashing; the
      fix records completion order through done callbacks registered before
      the wait. Red-then-green was proven twice: with the old shape the new
      test reports `['stderr', 'stdout']` across 25 trials, and with the fix
      it reports one answer. The pipeline expiry route re-observed readers
      under an already-expired deadline, which could replace the expiry the
      caller is owed with a reader's own failure; `_unobserved_pipeline_outputs`
      now reports `finished=False` so the settlement supplies its own EOF
      grace, and `deadline` is gone from both `_expired_pipeline_outputs` and
      `_PipelineExpiry` rather than left there as an unused figure the next
      reader would treat as a bound. `_create_stream_callback` rebuilt the
      emission context that `_compose_observation_line_callback` exists to
      bind, so it could pair the start reference with a different clock; it
      now routes through the helper. The doc finding was a real overclaim:
      `docs/users-guide.md` said a pipeline's deadline starts at spawn. It
      starts when collection begins, so spawn time is *not* deducted, and the
      page now says so. The design
      document, Figure 15's node label, and the developers' guide were
      brought in line with the same three code changes.
- [x] (2026-10-22) Full gate round on `c5501c33e`'s parent tree, `46dbb51e`
      with the ten changes dirty: check-fmt, test (5822 passed / 125 skipped
      across 22 sessions, Rust doctests green), typecheck, and lint all exit
      0. The dirty-tree round is the one that certifies the commit, since the
      commit is exactly that tree.
- [x] (2026-10-22) Adjudicated the fourth CodeRabbit review, of `deb83403a`.
      It returned ten findings over 53 reviewed files, which reduce to eight
      distinct concerns once the two restatements are folded in; all eight
      were still valid against the code and are fixed, and none was disputed.
      Five were stale prose: the plan quoted the overshoot allowances without
      the owned-group `_POST_KILL_SETTLEMENT_S`, the users' guide said no
      bound applies at `timeout=None` when the direct run's stdin wait is
      still bounded by the shared settle window, two test docstrings named
      `cuprum._subprocess_stream_run._await_stdin_writer` after the split
      moved it to `_subprocess_stream_post_exit`, the ADR addendum said a
      pipeline "stamps" its stages' output when the gathering is what starts
      late, and one Progress entry used the first person. The other three
      replaced the two clock tests' patch target. Patching `perf_counter` on
      the standard-library `time` module reached every caller in the process,
      so a start or stamp read directly from `time.perf_counter()` received
      the injected value and passed the very assertion meant to catch it;
      both tests now replace the `time` binding in `_command_internals`. The
      reviewer also proposed flipping `clock_origin` to a negative value, and
      that part was wrong: `_line_callbacks.py` takes
      `max(0.0, monotonic_clock() - started_at)`, so a negative origin makes
      a stray real reading appear as a large *positive* stamp rather than
      flooring to `0.0`. Both origins stay at `1e12`, with the comments
      rewritten to say why a value above the machine's uptime is what makes
      the test discriminating.
- [x] (2026-10-22) Adjudicated the fifth CodeRabbit review of `a2e63ffbb`,
      which returned seven findings over 53 reviewed files. All seven were
      still valid against the tree and all seven are fixed; none was disputed.
      Four of the seven restate two concerns: the plan's opening still named
      `c7fe5341a` as the branch head and left gate/commit/push/PR-readiness
      open, and the acceptance criteria said "no skips" while this plan records
      eight Rust arms, and "five gate targets" while the command list has six.
      Both are corrected here, and the finding counts are now enumerated per
      round rather than totalled, because a hand-summed total had already
      drifted from the per-round records. The users' guide gained the one
      thing the `lines()` timeout section did not say: an idle consumer is a
      suspended coroutine, so the expiry is recorded at the deadline and
      observed on the next `async for` step rather than raised into a caller
      that is not asking. EP-M1's Step 2 now names `_cleanup_direct_run`, which
      composes the writer reclaim and the group settlement under a single
      `_shielded_cleanup`; the step previously named the writer cancellation
      alone, which would have prescribed the exact two-shield shape the
      `Decision log` forbids. The lifecycle test's clock patch moved from
      `_pipeline_observation.time`'s attributes to the module's `time` binding,
      matching `test_pipeline_stdio_policy.py`.
- [x] (2026-10-22) Committed the fifth round's fixes as `8a80bccbe`. The
      round that certified that commit's tree ran while the three files were
      still uncommitted: all six gates exit 0 with `head=a2e63ffbb` and
      `tree=3` recorded at both ends of every log, 5655 passed / 125 skipped /
      0 failed across 22 pytest invocations, nextest 167/167, doctests 0
      failed with 3 ignored. That log set certifies the *tree*; a comment
      claiming a head for it would be the stale-filename mistake this plan
      already records, so the filenames carry no SHA.
- [x] (2026-10-22) Committed one further plan correction, `4b3d8c787`, after
      noticing that the opening claimed the certifying round's logs would be
      SHA-suffixed "as every earlier round's are" when only the two most
      recent rounds had done that.
- [x] (2026-10-22) Re-ran all six gates at the clean head `4b3d8c787`, with
      the tracked-tree change count 0 before and after each one
      (`/tmp/<gate>-cuprum-<branch>-4b3d8c787.out`): `check-fmt` 0 s, `test`
      426 s, `typecheck` 1 s, `lint` 110 s, `markdownlint` 18 s, `nixie` 1 s,
      every one exit 0. `make test` again reported 5655 passed / 125 skipped /
      0 failed over 22 sessions, nextest 167/167, and doctests 0 failed with
      3 ignored. `make lint` regenerated `typos.toml` and left it
      byte-identical, so the tree stayed clean.
- [x] (2026-10-22) Pushed `8a80bccbe` over SSH with the lease bound to the
      previously recorded remote head `a2e63ffbb`, then pushed `4b3d8c787` on
      top. Refreshed the tracking ref through an explicit leading-`+` refspec,
      confirmed `ls-remote` and `rev-parse HEAD` agree, and set the branch's
      upstream to `origin/`.
- [x] (2026-10-22) Committed the sixth round's fix as `82baebde0`, ran all six
      gates at that clean head, and pushed it. Every log records
      `head=head_after=82baebde0` with `tree=tree_after=0` and `exit=0`
      (`/tmp/<gate>-cuprum-<branch>.out`): `check-fmt` 0 s, `test` 447 s,
      `typecheck` 0 s, `lint` 114 s, `markdownlint` 18 s, `nixie` 1 s. The
      `test` figures were re-derived here rather than taken from the runner:
      22 pytest invocations, 5655 passed / 125 skipped / 0 failed; nextest
      167 run / 167 passed / 0 skipped over 7 binaries; doctests 3 ignored,
      0 failed. `make lint` regenerated `typos.toml` and left it
      byte-identical, so the tree stayed clean. The PR body now cites the
      `-82baebde0` round and the sixth-round reply is comment `6336964023`.
- [x] (2026-10-22) Ran all six gates at `fd6c27926`, the commit that recorded
      the round above, and closed the record-this-round item with this entry
      rather than with another cycle. Every log records
      `head=head_after=fd6c27926` with `tree=tree_after=0` and `exit=0`:
      `check-fmt` 0 s, `test` 430 s, `typecheck` 0 s, `lint` 113 s,
      `markdownlint` 17 s, `nixie` 1 s. The `test` figures were again
      re-derived from the log: 22 pytest invocations, 5655 passed / 125
      skipped / 0 failed; nextest 167/167 over 7 binaries; doctests 3
      ignored, 0 failed. This entry is part of the commit it describes, so
      the `fd6c27926` logs certify the commit *before* it; the PR body cites
      the round that follows this one. Repeating the record-then-regate cycle
      cannot terminate, because each record is a commit and each commit needs
      a round; the plan stops at the first round whose head is a commit that
      does not itself carry a gate claim.
- [x] (2026-10-22) Ran all six gates at `eab5a6671`, the commit that closed the
      cycle above, and pushed it. Every log records
      `head=head_after=eab5a6671` with `tree=tree_after=0` and `exit=0`:
      `check-fmt` 0.82 s, `test` 441.54 s, `typecheck` 0.56 s, `lint`
      113.85 s, `markdownlint` 17.77 s, `nixie` 0.47 s. The `test` figures
      were re-derived here from the log rather than inherited: 22 pytest
      invocations, 5655 passed / 125 skipped / 0 failed, zero `FAILED` lines;
      nextest 167 run / 167 passed / 0 skipped over 7 binaries; doctests 3
      ignored, 0 failed. `make lint` regenerated `typos.toml` and left it
      byte-identical. The head was pushed over SSH, `ls-remote` and
      `rev-parse HEAD` agree, the tracking ref was refreshed through a
      leading-`+` refspec, and the branch's upstream was set to `origin/`.
      The PR body cites the `-eab5a6671` logs, and GitHub Actions passed on
      that head: 16 of 16 jobs in the main run succeeded, and the Rust
      boundary run's four jobs succeeded. This is the terminal record: the
      work is complete, so there is no further round to name.

- [x] (2026-10-22) Requested `coderabbit review --agent` on the pushed head
      `7459e3caf` and answered the fifth round on PR #652 (comment
      `6336469259`). The sixth round returned one finding over 53 reviewed
      files, and it was right: this plan's opening still named `a2e63ffbb` as
      the last certified round, when `7459e3caf` is later and its six logs all
      exit zero at that head with `tree=0` before and after. The paragraph now
      points at the `Progress` record instead of naming a head, and records why:
      a plan edit moves the head, so a fixed name in the prose goes stale on the
      edit that follows it.

- [x] (2026-10-22) Requested `coderabbit review --agent` on the pushed head
      `d9db359cf` and answered the seventh round on PR #652 (comment
      `6338816985`). The round returned one finding over 51 reviewed files, at
      `docs/users-guide.md:2353-2354`, and it was right: the pipeline-timeout
      passage said an expiring `OWN_GROUP` run reclaims a descendant "exactly
      as it is for a single command", which is true only of a descendant still
      in the group. Teardown is `os.killpg(process.pid, group_signal)`
      (`cuprum/_group_teardown.py:349`), and `cuprum/_process_group.py`
      records why that cannot be extended: a `killpg` names a bare
      process-group number, and the kernel offers no way to ask whether the
      group holding it is this run's. A descendant that calls `setsid()` or
      `setpgid()` has left the group and is out of reach. The sentence now
      states the bound and defers to `docs/users-guide.md:2425`, which already
      says containment of an actively escaping process needs a supervisor, a
      cgroup, or a container. A first draft restated that bound at the sentence
      instead; it was replaced once the existing passage was found, because two
      copies of one guarantee drift apart. Fixed in `c09ae8e1e`, which is
      documentation only and is certified by `make markdownlint` (0 issues),
      `make nixie` and `make check-fmt` at that head with `tree=0` before and
      after each. That head was pushed over SSH with `ls-remote` and
      `rev-parse HEAD` agreeing, and its GitHub Actions runs are
      `39765094520` (CI) and `39765094495` (Rust boundary).

- [x] (2026-10-22) Requested `coderabbit review --agent` on the pushed head
  `9d6a8428d` and answered the eighth round. The round returned three findings
  over the same surface, and all three were right. Findings 1 and 3 are one
  concern stated twice — both post-exit deadline arms caught a `TimeoutError`
  raised by a stdin writer, because `TimeoutError` is an `OSError` subclass.
  They are fixed once, at the two classification arms. A third concern in the
  same round was separate and is described below.

  The third concern is genuinely about a helper, but a test one: the cleanup
  path most post-exit tests reach read the descendant's pid file once, so a
  test that failed before its child wrote that file reaped nothing and parked
  a sleeper on the shared host for its whole lifetime. Fixed in `78e4d2aed` by
  giving the read a short grace. A first draft of this entry credited the
  classification fix to that helper, which is a test-cleanup utility and
  cannot host a production arm; the two concerns were one paragraph apart and
  a sentence was merged across them. It also counted the helper's reach as 25
  call sites across eight modules, which was wrong twice: it is **21 call
  sites across seven modules**, and an eighth module held a *local copy*
  rather than reaching the shared helper. That copy is fixed separately in
  `24c78a8ea` and recorded in its own `Progress` bullet.

  Finding 2 was the substantive one, and it was checked against the tree rather
  than accepted from the review text. Its premise — that
  `_collect_line_stream_after_exit` classifies the expiry by exception type,
  while `_collect_stream_output` classifies it by its scope's `expired()` — is
  exactly right. What the review could not see is that the drain is not where
  an ordinary run first meets the failure. Instrumenting both arms of a writer
  that fails with a `TimeoutError` showed the exit wait's arm firing and the
  drain's never firing: the exit rendezvous re-raises a failed writer
  unchanged, one level above the drain. So the finding is valid *and* there is
  a sibling defect the round did not name, at
  `cuprum/_line_stream/coordinator.py`. Fixing only the drain would have left
  the reported shape broken and the new regression test passing for the wrong
  reason.

  Both arms now classify by identity rather than type, which is exact: the
  rendezvous raises the very object `_stdin_writer_failure` reads back off the
  task. The drain additionally consults its scope's `expired()` first and lets
  that win, because an expiry that fired was the run's deadline whether or not
  the writer was also failing — and that precedence keeps `_await_consumers`'
  spent-budget report distinct, since it leaves no writer failure to point at.
  A first draft of the drain used
  `not expired() or writer_failure is not None`, gated on
  `execution.timeout is None`; it was replaced because that disjunct still
  reported a writer failure as the deadline whenever a `timeout=` *was*
  configured.

  The two regression cases are kept separate so each names its arm, and each
  was proven to bite only its own fix: removing the drain arm fails only the
  after-exit case, and removing the exit-wait arm fails only the before-exit
  case. Reaching the drain took a third attempt worth recording. A writer that
  waits for `process.returncode` still loses, because that attribute is set
  before the exit wait resolves, so the case collapsed silently into the
  exit-wait one and passed with the drain fix reverted. The drain case now
  wraps the rendezvous through the module's documented patch seam and fails the
  writer only after the real rendezvous returns, with an assertion that the
  flag was set so the case cannot silently collapse again. Fixed in
  `78e4d2aed`. That commit is **not** gate-certified: the round run over it
  failed `make lint` at `pylint-classic`, because the added reasoning pushed
  `drain.py` and `coordinator.py` from 393 and 392 lines to 415 each, past the
  400-line module cap. The fix is the split recorded below, and the gate
  evidence for this arm is the round run over that split's commit.

- [x] (2026-10-22) Split the two modules back under the cap rather than
  suppressing `C0302`, which the repository forbids. Two cohesive units moved
  out, each into a new module beside its peers, and no behaviour changed.
  `_await_consumers` and `_first_reader_failure` became
  `cuprum/_line_stream/reader_wait.py`, leaving `drain.py` to own *when* the
  run stops waiting and the new module to own *what* it learned while waiting;
  `drain.py` is 290 lines. `_finalize_line_stream_captures` became
  `cuprum/_line_stream/captures.py`, which is the one cleanup a completed
  `lines()` result owes; `coordinator.py` is 387 lines.

  The seam was chosen by the patch surfaces, not by line count. Three names
  could not move, and each is pinned for a different reason:
  `_wait_for_line_stream_exit` by `test_line_stream_reader_failure.py`'s dotted
  string `cuprum._line_stream.coordinator._await_exit_or_writer_failure`;
  `_run_line_stream_teardown` and `_discard_drain` by
  `test_line_stream_exit.py`, which patches `_shielded_cleanup` *in
  coordinator's globals* and calls `_discard_drain` as a coordinator attribute;
  and `_run_to_command_result` by `test_line_stream_coordinator_stateful.py`,
  which assigns it onto the module object. `_coordinate_line_stream` therefore
  had to stay with the last one, because it is the caller that resolves that
  name from coordinator's globals — moving it would have silently un-patched
  the stateful machine while its own import from the package still resolved.
  That was verified directly (`.__globals__ is coordinator.__dict__`) before
  the cut.

  `test_line_stream_reader_failure.py`'s import was updated to the owning
  module. It previously read
  `from cuprum._line_stream.drain import _await_consumers`, which would have
  kept passing only because `drain` happens to import the name for its own use
  — a re-export patch seam that resolves to a name the consumer no longer
  defines.

  Verified at the tree: `make pylint-classic` 10.00/10 on both passes,
  `make duplication` 68 allowed by reasoned exceptions, `make typecheck` clean,
  the Skylos dead-code scan exit 0 with no findings, `make check-fmt` clean
  over 1084 files, and the focused line-stream suites 677 passed / 35 skipped.

- [x] (2026-10-22) Unified the one module that kept a local reaper instead of
  reaching the shared one. `test_pipeline_process_group_run.py` defined
  `_reap_descendant` and `_recorded_descendant` privately, and the pid file was
  read once; the shared helper's grace was therefore absent from it. Its three
  call sites all pass a pid that is `None` when the test failed before the
  pid-file wait returned, and two of them resolve that pid through
  `wait_for_pid_file`, which can raise before assigning — so the leaking branch
  is reachable, not theoretical. Its own docstring already promised to resolve
  the pid "when it failed before the wait returned", which is how the gap
  survived review: the prose described the shared behaviour the code did not
  implement. The module now imports the shared helper, `os` and `signal` are
  gone, and the three tests pass. Fixed in `24c78a8ea`, with
  `make pylint-classic` at 10.00/10 on both passes and `make duplication`
  still 68 allowed. This is what makes the shared helper's reach uniform; the
  figures are 21 call sites across seven modules plus these three.

## Surprises & discoveries

- **A task-list item's content column is 2, not the width of its marker.** A
  `- [x]` item renders its `[x]` as a checkbox, so the item's content begins at
  the `-` indent rather than after the whole marker. Continuation lines
  indented to the marker's visual width — 6 spaces — are therefore 4 spaces
  beyond the content column, which CommonMark reads as an *indented code
  block*. The eighth round's `Progress` entry did exactly that for its second
  and later paragraphs, and `make markdownlint` then reported the file's one
  legitimate fenced block as an MD046 violation: MD046's `consistent` mode
  adopts the *first* block token it sees, so one accidental indented block
  retroactively makes every fenced block in the file wrong. The reported line
  is a victim, not the cause — the offending block sits in `Progress` and the
  line named is in the validation section more than a thousand lines later.
  Continuing a list paragraph after a blank line needs only the item's own
  2-space content indent, which is what the rest of this file already uses.

- **A passing regression test can be reaching the wrong arm.** The first
  version of the writer-`TimeoutError` case passed with *and* without the drain
  fix, because the exception was consumed one level up at the exit wait. A test
  that passes for the wrong reason is worse than no test: it certifies a fix
  that is not on the path it names. Instrumenting both arms is what showed the
  route, and proving each case bites only its own fix is what showed the
  rebuild had landed on the right one. The same trap applies to any test whose
  failure is an exception identity the codebase raises in more than one place.

- **The host's Git attributes replace Python's merge driver.** The rebase
  initially ran through a `weave` merge driver selected by the host's global
  attributes file, which silently dropped re-imports this branch's split
  depends on: replays 2 and 5 both produced a tree in which
  `cuprum._subprocess_stream_run` no longer bound `_settle_stdin_writer` or
  `_settle_owned_group_before_draining`, without reporting a conflict, so the
  tests patching those names would have patched dead bindings. The replay was
  restarted against the built-in merge backend by passing
  `-c core.attributesFile=/dev/null` on the initial `git rebase` *and* on every
  `git rebase --continue`, because each continuation starts a fresh Git process
  that reads the attributes again. The lesson is that a clean replay exit is
  not evidence the merge backend was the documented one; confirm which driver
  ran before trusting a conflict-free result.

- **A docstring correction can be the only thing a later commit carries, and
  the helper it describes may have moved.** Replay 28 was twenty lines of prose
  about `_settle_owned_group_before_draining`: which routes call it, and in
  what order the direct path uses it. On the rebased tree the helper lives in
  `cuprum._subprocess_stream_post_exit` — `main`'s ADR-007 split put it there,
  and this branch's plan had decided the opposite before the rebase — so the
  commit applied to a location that no longer exists, and the correct
  resolution was to rewrite the docstring at the helper's new home rather than
  retain a second copy in `_subprocess_wait`. Replay 30 then corrected the
  caller count from four to three, and again the edit belonged at the new home.
  Prose commits are as exposed to a relocation as code commits are.

- **Declaring a module in `EXTENSION_TEST_TARGETS` also declares it for
  Windows.** The list reads like "the modules that need the compiled
  extension", and its stated purpose is to keep the Rust arms out of the main
  suite. It is consumed by two jobs, and one of them runs on `windows-2022`;
  nothing in the list's shape says so. A module whose cases need POSIX process
  groups is therefore a Windows job failure the moment it joins the list, and
  the failure looks like a product defect
  (`OWN_GROUP requires POSIX process groups`) rather than a missing marker. The
  lesson is to read the consumer list, not the comment, before adding an entry:
  the comment described *why* the list exists, and the CI matrix described
  *where* it runs.

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
  the gate that reports it.**

  The same lesson repeated for `cuprum/_pipeline_settlement.py` on 2026-10-21,
  which is why it was predicted rather than discovered the second time — but a
  *second* inventory failed alongside it that the first module did not trip.
  `test_extension_build_contract.py` derives the extension-gated test modules
  from the suite textually and fails until each is declared in
  `EXTENSION_TEST_TARGETS`, so the new
  `cuprum/unittests/test_pipeline_post_exit_deadline.py` — which skips its Rust
  arms with the shared reason — broke it. A new *test* module is therefore also
  a wiring change, and the two inventories are independent: the wheel snapshot
  enumerates shipped production modules, the extension contract enumerates
  gated test modules, and one can be satisfied while the other is not. Both
  failures shared a signature that is worth recognizing: they are the
  repository's own *derivations* of a set that this change grew, and each one
  failed with a message naming the missing member rather than with a diff.

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

- **The deadline fix had removed a bound that predated it.** The bug in #533 is
  an unbounded wait; the branch's own first fix replaced the direct run's
  unbounded await with `asyncio.timeout(_deadline_for_collection(execution))`
  and read no further. That expression is `None` when the caller configured no
  `timeout=`, and `asyncio.timeout(None)` is not a short deadline — it is no
  deadline. So on the no-timeout route the branch turned a hang that `main`
  already prevented into a hang it caused, and no test in the module noticed,
  because every existing case configured a deadline (the no-timeout case that
  did exist,
  `test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group`,
  cancels rather than waits, so the settle path is never what ends it).

  The general lesson is about the shape of the mistake rather than this
  instance: a fix that *narrows* a shared helper to "the caller's configured
  budget" must ask what that helper did for a caller that configured nothing.
  `main`'s direct path had called `_settle_stdin_writer` unconditionally; the
  rebase preserved the call for the streamed path only, and the diff read as a
  simplification because the deleted call and the added `asyncio.timeout` sat
  in different files after the module split. Splitting a module is exactly the
  operation that makes this kind of deletion invisible in review.

- **A module-level `from time import perf_counter` binds at import time, and a
  lazy import hides the patch from a regression test.** The guard for the
  `lines()` start reference patches `_command_internals.time.perf_counter` and
  expects the run to read the patch. While the production code was still the
  mutant, the test passed when run alone and failed when run after a sibling
  test. The difference was collection order: `lines()` imports
  `cuprum._line_stream.spawn` lazily, so a lone run entered the module for the
  first time *after* the patch was installed and the module-level
  `from time import perf_counter` bound the patched lambda. A sibling having
  touched the module first left it bound to the real clock. The test now calls
  `importlib.import_module("cuprum._line_stream.spawn")` before installing the
  patches, which makes it order-independent; `pyproject.toml` configures no
  random ordering, so "runs alone" and "runs in the suite" are genuinely
  different executions.

- **The queue sink does not stamp `LineEvent.at`, and the first-round refusal
  of finding 1 rested on the opposite belief.** `_stamp_line` is the only
  production construction site of a `LineEvent`; the queue relays events that
  have already been stamped, so the callback path and the queue path carry the
  *same* value rather than two competing ones. Threading the observation clock
  into `_LineEmissionContext` was therefore both safe and necessary: without
  it, the stamp came from `cuprum.lines.perf_counter` while the start reference
  and the deadline came from the injected clock, which under injection is a
  difference between two clocks. `LineEvent.at`'s documented contract was
  widened to say the stamp is read from the run's own clock.

- **The line stream's exit event mixes two clocks, harmlessly in production.**
  `duration_s` is `exited_at - started_at`, where `exited_at` comes from the
  module-level, non-injectable `time.perf_counter` in
  `cuprum/_subprocess_deadline.py` and `started_at` now comes from the
  injectable observation clock. Both are `time.perf_counter` in production, so
  the subtraction is a duration; only under clock injection does it become the
  difference between two unrelated origins. Left as-is deliberately: the
  alternative is threading the observation clock into the deadline module's
  exit sampling, which is a wider change than this branch's scope and would
  make a test-only artefact the reason for a production seam.

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

- Decision (EP-M3/EP-M4 review, 2026-10-21): reject the `_spawn_stdin_writer`
  finding for the third time, re-verified again at `8936c9bfa` rather than
  inherited. This round it is filed as `major` and proposes the same
  four-argument form plus a `_stdin_codec` helper imported from a module that
  does not exist. Re-checked in the tree that produced the review:
  `_spawn_stdin_writer` is still `(process, stdin_data, observation)` at
  `cuprum/_subprocess_stdin.py:139-143`, and `_SubprocessExecution` still
  declares `stdin_data: bytes | None` — this time at
  `cuprum/_subprocess_spawn.py:68` — with no `stdio` field and no
  `_stdin_codec` anywhere under `cuprum/`. The full suite passes at this HEAD,
  including the blocked-stdin timeout tests that would fail with an
  `AttributeError` before the `try`. Three rounds, three rejections, each
  independently checked: the finding is about a different revision of this
  repository, not about a defect that a passing suite hides. Recorded once more
  because a rejection that is not re-verified after a milestone is exactly the
  kind that ships a real defect.

- Decision (post-rebase adjudication, 2026-10-21): the three
  `_spawn_stdin_writer` rejections above are **superseded**. They were correct
  against the revisions they were checked at, and they are false against the
  tree this branch now sits on. `main` has since landed the stdin-plan split
  (issue #445/#511, commit `d93df9563`), and the four-argument contract the
  review described is now exactly what the tree declares:

  - `_spawn_stdin_writer(process, plan, codec, observation)` at
    `cuprum/_subprocess_stdin.py:273-278`.
  - `_SubprocessExecution.stdio: _ResolvedStdio` at
    `cuprum/_subprocess_spawn.py:101`, with `_ResolvedStdio.stdin` as the plan.
  - `_stdin_codec(ctx)` at `cuprum/_subprocess_stdin_stream.py:99`, and the
    module itself exists.

  The rebase resolution takes `main`'s calls everywhere: the direct run now
  passes `execution.stdio.stdin` and `_stdin_codec(execution.ctx)`, and the
  same applies to the streamed path and `_line_stream.drain`. Impact on this
  plan: nothing in EP-M1…EP-M4's *design* changes — the deadline, the public
  exception, and the route-level bound are untouched — but three Decision-log
  entries that a future reader would otherwise trust now misdescribe the tree.
  Rationale for keeping the false entries rather than editing them: they record
  what was true when each review was answered, and rewriting them would erase
  the evidence that the same reviewer was right about a later revision. The
  supplement is the correction. **Lesson:** a rejection is only good for the
  revision it was checked at; a rebase invalidates every one of them, and the
  first thing to re-check after one is whatever was previously rejected.

- Decision (post-rebase adjudication, 2026-10-21): the direct path keeps its
  *own* post-exit policy instead of adopting `main`'s `_settle_stdin_writer`.
  `main`'s direct run settles the writer through the fixed 0.25 s window, which
  returns the child's exit code even when the caller's 2 MiB payload was never
  delivered. This branch's direct path awaits the writer under the run's
  remaining deadline and reports the public `TimeoutExpired` on expiry. The
  branch's reading is the correct one for this issue and is kept, because "the
  configured timeout covers every await after the child exits" is the whole
  point of #533; a fixed window on the direct path is a bound that reports
  success over truncated input. Consequences handled:
  `cuprum.unittests.test_stdin_settle_cancellation
  ::test_a_child_that_never_reads_does_not_wedge_the_run`
  (a `main` test asserting the old contract) is re-expressed as
  `test_a_child_that_never_reads_ends_the_run_on_its_deadline`, and the shared
  harness `_install_post_exit_probe` now patches both post-exit seams because
  the two live in different modules since `main` split the direct run out.

- Decision (post-rebase gate round, 2026-10-21): a lint rule the rebase
  *introduced* is treated as a requirement on this branch too, even where the
  flagged line predates the rebase. `ruff` 0.16's `implicit-return-value`
  rejected `_await_readers`'s bare early `return` at
  `cuprum/_line_stream/drain.py:260`. Rationale for fixing rather than
  suppressing: the function's docstring already declares `BaseException | None`
  and every other completing exit returns `None` explicitly, so the bare exit
  was the odd one out and `return None` is the spelling the surrounding code
  already uses. Impact: one token, no behaviour change, and the focused
  `lines()` module re-run green. **Lesson:** re-running a gate after a rebase
  can fail on *inherited* code, because the gate's own rule set moved with the
  base — the failure belongs to the merge, not to the line's author.

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

- Decision (EP-M3, 2026-10-21): accept four files outside the `Scope`
  tolerance's list, recorded here because that tolerance calls an outside file
  a stop-and-escalate event. They are `cuprum/_stream_drain.py`, `Makefile`,
  `cuprum/unittests/__snapshots__/test_maturin_build.ambr`, and
  `pyproject.toml`. Each was forced by a *gate* rather than chosen while
  designing: the duplication gate refused the pipeline's restatement of the
  direct run's EOF-grace reporting, so the reporting moved to `_stream_drain.py`
  (`_report_eof_grace_expiry`); the extension build contract derives gated
  test modules from the suite and refused the new module until `Makefile`
  declared it; the wheel snapshot enumerates shipped modules and refused the
  build until it listed the new one; and the same duplication gate pairs
  TYPE_CHECKING blocks, so the allow entry needed the two new members. None
  changes a public signature, an exception type, or a result field, which is
  what the adjacent `Interface` tolerance actually protects. Alternative
  considered: stop and ask before each (rejected — each is a mechanical
  consequence of a change already approved, the gates name the exact edit, and
  halting four times would have recorded the same reasoning four times). Date:
  2026-10-21.

- Decision (EP-M3, 2026-10-21): extract the pending-reader accounting and the
  `capture_eof_grace_expired` reporting into
  `cuprum._stream_drain._report_eof_grace_expiry` rather than recording a
  duplication exception for the pipeline's copy. Rationale: the nose gate
  reported the pair as copy-paste, and the overlap is a rule both drains must
  apply identically — which readers are still pending, and how that expiry is
  reported — so a change to it should not be able to reach one route and miss
  the other. What stays route-specific is only the *wait*: the direct run's is
  hookable and typed over its stdout/stderr pair, while the pipeline's is an
  `asyncio.wait` over N readers. The shared helper therefore takes a
  `tuple[_StreamConsumerTask, ...]`, and the hook keeps its existing type.
  Alternative considered: a per-location reasoned exception (rejected: the
  AGENTS.md duplication policy is to treat a new finding as copy-paste until
  proven otherwise, and the parallel structure here is not deliberate).

- Decision (EP-M3, 2026-10-21): widen the TYPE_CHECKING-block allow entry from
  thirty to thirty-two members for `_pipeline_settlement.py` and
  `_pipeline_stream_results.py`, rather than restructuring either block.
  Rationale: that entry already covers the idiom for thirty modules, and these
  two join it on the same terms — each block names its own module's type-only
  collaborators and nothing else. The alternative the gate offers, factoring
  the block into a shared import, cannot remove a per-module binding.

- Decision (EP-M3, 2026-10-21): declare the new
  `test_pipeline_post_exit_deadline.py` in `Makefile`'s
  `EXTENSION_TEST_TARGETS` and add one line to the maturin wheel snapshot,
  rather than leaving the two failing tests to be handled with the docs. Both
  are the repository's own inventories of what this change created, and the
  tests that hold them are what makes a forgotten member fail loudly. The
  snapshot line was inserted in sorted position and the focused module re-run,
  not regenerated with `--snapshot-update`: the diff is then a claim the
  reviewer can check by eye ("this change adds one module and the wheel ships
  it"), where a regeneration would assert only that the snapshot now matches
  whatever the build produced. Date: 2026-10-21.

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
  drift). Date: 2026-10-16. **Superseded on the second rebase (2026-10-22):**
  the split half of this decision stands and the helper half does not. `main`
  has since split the streamed run's post-exit phase into
  `cuprum/_subprocess_stream_post_exit.py` (ADR-007), and the helper now lives
  there with two other callers beside it. The direct-run module meanwhile sits
  *below* the post-exit module in the import order — it imports the helper from
  it — so the "neither shape should import the other" reasoning still holds,
  just with a different owner for the shared rule. See the rebase decision
  entries at the end of this log.

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
  length to a function the collector does not own. The collector's own
  `_await_pipeline_eof_grace` therefore calls
  `asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)` over the whole N+1
  consumer tuple and then `_report_eof_grace_expiry`, sharing the 0.25 s bound
  and the pending-reader accounting with the direct run's drain while each
  keeps its own wait. It still uses `_settle_consumers`,
  `_decode_consumer_result` and `_DrainContext`, and it does not call
  `_drain_stream_consumers`, whose failure reporting is stdout/stderr-pair
  specific. Alternative considered: widen `_EofGraceWaiter` to an arbitrary
  sequence (rejected: outside the allowed files, and it would put
  pipeline-shaped knowledge into a direct-run type).

  Correction (2026-10-21, milestone review): an earlier draft of this entry
  said the collector awaits `(context.eof_grace_waiter or _await_eof_grace)` in
  the shape `_await_capture_eof_grace` uses. That is not what shipped. The hook
  is deliberately **not** consulted on the pipeline path: it exists so a
  direct-run test can replace a wall-clock wait with a deterministic one, and
  honouring it only when a pipeline happens to hold two readers would make that
  test seam behave differently at stage counts the test cannot see.
  `_await_pipeline_eof_grace` documents the same reason in its docstring. The
  shared part is the `_stream_drain` bound and accounting, not the hook.

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

- Decision (EP-M3, 2026-10-21): the collector's bounded observation and
  settlement are split out into a new module, `cuprum/_pipeline_settlement.py`,
  which the collector now imports. Rationale: the EP-M3 work took
  `_pipeline_collect.py` to 444 lines against Pylint's 400-line
  `max-module-lines`, and the width came from exactly this concern — reader
  observation, drain context, shielded settlement, stage termination, pump
  reconciliation and the assembled `_PipelineOutputs`. The split is by feature
  and by direction of travel: settlement is meaningful for any pipeline whose
  stages exist, while the collector owns the *wait* and the `cuprum.sh` shim.
  `_gather_pipeline_outputs` moved with it and `_pipeline_internals.py`
  re-exports it from its new home, so the re-export list its callers rely on is
  unchanged. `_expired_pipeline_outputs` ends an expired run and answers with
  the outputs owed; the collector keeps `_report_pipeline_expiry`, which raises
  the public `TimeoutExpired`. That direction matters: the settlement module
  must not import the collector, or the two would form a cycle. Two dependants
  had to follow the move — the termination stub in `test_pipeline_timeouts.py`,
  which patched `_pipeline_collect._terminate_timed_out_stages` and now patches
  the settlement module, and `test_pipeline_collect.py`, which no longer needs
  to stub the gather at all because the invariant test reaches the real one
  with empty readers. Alternative considered: trim docstrings and comments to
  fit under the cap (rejected: it would have deleted the reasoning the cap
  exists to protect, and the module would still hold two unrelated
  responsibilities).

- Decision (EP-M3, 2026-10-21): the impossible-state invariant is checked
  *after* teardown, not before it. Rationale: a run that reaches the expiry
  handler has live stages by construction, and `_expired_pipeline_outputs` is
  what terminates them. Reporting "expiry without a configured timeout" first
  would be correct about the invariant and wrong about the machine: it would
  leave the very stages the handler exists to reap. The branch is unreachable
  in practice — only a configured deadline can expire — so the ordering is
  chosen for the failure mode, not for the common path. Alternative considered:
  fail fast on the invariant and let the caller's `finally` reconcile
  (rejected: the pipeline's `finally` reconciles the *pumps*, not the stages,
  so the stages would leak).

- Decision (EP-M3, 2026-10-21): no change was made to fit
  `cuprum/unittests/test_pipeline_post_exit_deadline.py` under the 400-line
  cap, because the cap does not bind it. Rationale: `pylint-classic` runs the
  test roots in a second invocation with `--disable=too-many-lines` (the
  `PYLINT_TEST_TARGETS` split in `Makefile`), so the ceiling applies to
  production and benchmark sources only. Adding a split or a suppression for a
  rule that is not enabled would have been churn against a policy the
  repository deliberately scoped. Alternative considered: split the module
  anyway for symmetry (rejected: the tests share one fixture set and one
  harness, and splitting them would duplicate that harness).

- Decision (EP-M3, 2026-10-21): the new gated module was added to
  `EXTENSION_TEST_TARGETS` and the wheel snapshot was edited by hand rather
  than regenerated, and both were staged into the code commit rather than the
  documentation commit. Rationale: the two failures are consequences of the
  code change, so a reader bisecting this branch should find the change and its
  inventory updates in one commit; and a hand-edited snapshot asserts something
  reviewable — "this change adds exactly one module and the wheel ships it" —
  where a regeneration asserts only that the build matched itself. Alternative
  considered: `--snapshot-update` (rejected for the reason above, and because
  the regenerated blob would hide an accidental second addition).

- Decision (EP-M1, 2026-10-21): the direct path's remaining-deadline bound is
  scoped to runs that *have* a deadline; a run with no `timeout=` takes the
  shared `_settle_stdin_writer` window instead. Rationale: `asyncio.timeout`
  accepts `None` and means "no bound", not "no deadline to take a remainder
  from", so passing the `None` straight through silently deleted the bound
  `main` still applies on that path — a regression introduced by this branch,
  not an inherited defect. The two bounds answer different questions. A
  deadlined run has a budget the caller chose and an expiry it can be told
  about, so spending the remainder and reporting `TimeoutExpired` is the run
  saying what happened. An undeadlined run has neither, so the choice is
  between a fixed window and hanging for ever; hanging is exactly the defect
  #533 exists to fix, and it would arrive here on the one route that cannot
  report it. Alternative considered: give the undeadlined run the same
  remainder arithmetic with a sentinel infinity (rejected: an infinite deadline
  is the unbounded await spelled differently, so the reviewer's finding would
  still hold).

- Decision (EP-M1/EP-M2 follow-up, 2026-10-22): the `C0302` module-cap failure
  is fixed by splitting two modules, not by suppressing the check or raising
  the cap. Rationale: this repository forbids both, and the two files were
  already at 393 and 392 lines before this branch's reasoning comments, so the
  overrun is this branch's to absorb. The seam is the patch surface rather than
  the line count: three names are pinned by tests through coordinator's globals
  or by a dotted string, so the movable units were the reader wait (which the
  drain's scope bounds but does not itself *wait* on) and the capture
  finalisation (the one cleanup a completed `lines()` result owes).
  `_coordinate_line_stream` stayed with `_run_to_command_result` because it is
  the caller that resolves that name from coordinator's globals; moving it
  would have silently un-patched `test_line_stream_coordinator_stateful.py`,
  whose import from the package would still have resolved. Alternative
  considered: extract the whole teardown trio into its own module (rejected:
  all three resolve `_shielded_cleanup` and `_reconcile_run_tasks` from
  coordinator's globals, so each move would either un-patch a live test seam or
  require the test to patch two modules for one behaviour).

- Decision (post-review, 2026-10-22): take the `lines()` start reference from
  `execution.observation.monotonic_clock()` rather than from the module-level
  `perf_counter`, and inject that clock in the guard test. Rationale: the
  post-exit deadline is
  `timeout - (observation.monotonic_clock() - started_at)`, so a start from a
  different clock is not a duration under injection. The same reasoning already
  governs the direct path's `_sample_run_start`. Alternative considered: leave
  the start where it was and treat the mismatch as test-only (rejected: the
  observation clock is exactly the seam the existing deadline tests use, so a
  run under that seam would compute a nonsense remainder — the fix is one line
  and makes the two paths agree).

- Decision (post-review, 2026-10-22): apply the half of the same finding that
  asked for the observation clock to be threaded through `_LineEmissionContext`
  and `_stamp_line`. Rationale: the start reference, the per-line stamp, and
  the post-exit deadline must be readings of one callable, or under injection
  the stamp is a difference between two clocks. `_stamp_line` is the only
  production construction site of a `LineEvent`, so the earlier belief that a
  queue sink stamped a second, competing value was mistaken. Scope: three
  production construction sites and nineteen test constructions, plus the
  handle and pipeline start samples, which had the same defect on their own
  routes. Alternative considered: leave the stamp on its own seam and treat the
  mismatch as test-only (rejected: the observation clock is the seam the
  deadline tests inject, so a run under that seam would stamp nonsense — the
  whole point of the finding).

- Decision (post-review, 2026-10-22): reject finding 2's suggested production
  change and fix the docstring instead. Rationale: settling above the direct
  run's writer await would call `_settle_owned_group_before_draining` on the
  success path, where the caller deliberately leaves descendants running, and
  that contradicts what `run()` promises. The redundancy argument is the weaker
  one and is not relied on: the expiry route already settles under
  `_cleanup_direct_run`'s single shield, so an extra call there would be
  redundant rather than harmful — `_members_remain_to_signal`'s probe vetoes a
  second pass — but the success-path contract is decisive on its own. The
  reviewer's premise was the helper's docstring, and the docstring was wrong
  about both its routes and the direct path's ordering. Alternative considered:
  apply the suggestion and rely on `_terminate_process`'s early return for an
  exited child (rejected: the probe is an implementation detail of the helper,
  not a licence to call it where it does not belong).

- Decision (post-gate, 2026-10-22): move the stage path's
  `_LineEmissionContext` construction into `cuprum/_line_callbacks.py` as
  `_compose_observation_line_callback`, rather than recording a duplication
  exception. Rationale: the gate first flagged the two mirrored constructions in
  `cuprum/_pipeline_stage_streams.py`. Extracting them inside that module
  fixed the family it reported, but the helper's type-only names — `cabc`,
  `LineStreamName` and `_LineHookOutcome` — pushed that module's
  `if typ.TYPE_CHECKING:` block past the line threshold at which the detector
  pairs it with the repository-wide import family, which then reported a
  thirty-three-location family at value `301.5`. A probe that stripped only the
  three new type-only imports and re-ran the detector passed with
  `66 allowed by reasoned exceptions`, which proves the block was outside the
  family before this branch and that the change, not `main`, put it in.
  Widening the thirty-seven-module exception would have recorded an
  import-declaration idiom for a helper's annotations, and would have left the
  duplicated construction in place. Moving the construction to the module that
  owns the stamping seam removes the duplication instead: the sharing is now at
  the correct architectural layer, and the one-clock rule holds by construction
  because the helper takes the observation rather than a clock beside it.
  Alternative considered: keep the local helper and add the module to the
  import entry (rejected on both counts above). A second alternative — leaving
  both inline constructions and suppressing the original two-location family —
  was not available under the repository policy, which treats a new finding as
  copy-paste until proven otherwise.

- Decision (rebase onto `9c9008351`, 2026-10-22): accept `main`'s home for
  `_settle_owned_group_before_draining` —
  `cuprum/_subprocess_stream_post_exit.py` — rather than the plan's pre-rebase
  assumption that it lives beside the other wait helpers in
  `cuprum/_subprocess_wait.py`. Rationale: `main`'s ADR-007 split made the
  post-exit phase a module of its own, and the helper is the phase's, not the
  exit wait's. The rebased branch follows it there: the line drain imports it
  from the post-exit module, and the two replays whose docstring correction had
  been written against `_subprocess_wait` had that correction re-applied at the
  new home. What the branch must *not* do is keep a second definition in the
  old module: replays 28 and 30 both conflicted because the child's side of the
  hunk still declared the helper body there, and taking it would have left two
  live definitions and an `__all__` entry for a name no call path resolves. The
  helper's own docstring now names its three callers and the two neighbouring
  shapes that answer the same question without calling it, which is the fact
  the conflict exposed.

- Decision (rebase onto `9c9008351`, 2026-10-22): express the branch's
  post-exit deadlines in `main`'s capture-policy vocabulary rather than
  carrying the pre-rebase `capture=` flag through. Rationale: `main` replaced
  the single boolean with a resolved policy per stream, so a `_DrainContext`
  now carries `captures: tuple[CapturePolicy | None, CapturePolicy | None]` and
  a `capturing` property, and a drain result is a `_CapturedDrain` with
  `.payload` and `.capture` beside each other. The branch's own contributions
  were three: `_drain_context(...)` reads `execution.capture_stdout` and
  `capture_stderr` instead of one flag; `_expire_line_stream` reports
  `stdout_drain.payload` and `stderr_drain.payload` off the returned
  `_DrainPair` instead of unpacking a `(text, text)` tuple; and
  `_pipeline_settlement` and `_pipeline_records` carry per-stream policies
  through `_PipelineOutputs`. The partial-capture case is what makes the
  per-stream shape load-bearing rather than cosmetic: a run can bound one
  stream and spill the other, so a single flag cannot say which of the two
  payloads a `TimeoutExpired` should report.

- Decision (rebase onto `9c9008351`, 2026-10-22): keep the `_PipelineOutputs`
  per-stream correction as its own change, separate from the deadline work that
  motivated it. Rationale: the deadline path needs to answer two separate
  questions — whether the final stage's stdout is worth reporting as the
  expired run's `output`, and whether the stages' stderr is worth joining for
  its `stderr` — and those answers differ exactly when the caller bounded one
  stream and not the other. Folding the correction into the deadline commit
  would have made a pure type change unreviewable inside a behavioural one, and
  `main`'s own record docstring states the same reasoning, so the two sides
  agree on the shape and the branch's commit only carries the deadline that
  reads it.

- Decision (rebase onto `9c9008351`, 2026-10-22): record that
  `cuprum/_subprocess_stream_run.py` no longer binds `_settle_stdin_writer`,
  and rely on the test probe patching the *resolution* site instead. Rationale:
  `main`'s post-exit split moved `_await_stdin_writer` into
  `cuprum/_subprocess_stream_post_exit.py`, and that module resolves
  `_settle_stdin_writer` from its own globals. A test that patched the run
  module's copy would add a name no call path reads and pass vacuously — the
  patched function would never be entered.
  `cuprum/unittests/_stdin_run_cancellation_support.py` already patches the
  post-exit module's binding for the streamed path and
  `cuprum/_subprocess_direct_run.py`'s `_await_writer_within_deadline` for the
  direct path, with a comment saying why, so the rebase needed no test change;
  it needed the *fact* recorded, because the pre-rebase seam was the run
  module's.

- Decision (gate failure, 2026-10-22): split the direct run's *result*
  assembly into `cuprum/_subprocess_outcome.py` rather than trimming comments
  or raising the cap. Rationale: `cuprum/_subprocess_execution.py` was at
  exactly 400 lines on `9c9008351`, and this branch's `TimeoutError` split
  added 29. The module mixed two responsibilities that already had a stated
  seam — deciding *when* a run settles (the spawn, the wait, the streamed
  hand-off) and deciding *what* a settled run reports (`_DirectCompletion`,
  `_build_command_result`, the TypedDict its `**` unpacking is checked
  against). Splitting on that seam removes 152 lines and leaves a 273-line
  orchestration module; trimming the explanation of a four-route `TimeoutError`
  distinction, or asking for a cap exception, would have paid for a size rule
  with the evidence a future reader needs. The orchestration module re-exports
  `_DirectCompletion` and `_relay_fallbacks_for_result` so the two reporting
  modules that import them by that name, and any test patching them there,
  resolve unchanged. Considered and rejected: moving the builder into
  `cuprum/_result_assembly.py` (that module documents itself as the *rules*,
  shared by the single-command and per-stage paths, and the builder is the
  single-command path's own construction); splitting the pipeline path's stage
  builder alongside it in the same module (a real parallel, but one whose two
  sides would then need a third name to say which builder is which, and the
  gate did not report it); a new module for the relay-fallback flattening alone
  (too small to be a module, and it belongs beside the record whose field it
  fills).

- Decision (gate failure, 2026-10-22): widen the duplication gate's import
  family entry by one member rather than suppressing the scan or lowering its
  surface. Rationale: the finding paired `cuprum/_subprocess_outcome.py`'s
  `if typ.TYPE_CHECKING:` prologue with the 45 modules that already declare
  theirs — the same recorded idiom, now with one more declarer, because the new
  module is a split of a member. The entry's existing reason already argues
  this class of pairing cannot be extracted; the change names the new module
  and records which widening did what, so the next reader can tell the
  pipeline-drain widening of forty-three→forty-five from this one of
  forty-five→forty-six.

- Decision (rebase, 2026-10-22): rebase a third time onto `origin/main` =
  `e2c7592b5` immediately, rather than gate and push the head already prepared.
  Rationale: that commit landed while the previous round was running, so a push
  would have published a branch based on a superseded `main` and CI would have
  tested the merge ref, not the ref the review sees. The replay was cheap to
  verify: 38 commits, no conflict, every one patch-identical under
  `range-diff`, and a content-only comparison of the two branch patches
  (target-to-old-head versus new-target-to-new-head) showed zero differences
  once hunk offsets and index lines were excluded. The new base changes
  `cuprum/builders/args.py` and `cuprum/builders/rsync.py` plus three
  documentation files this branch also edits — but in different sections, which
  is why the replay was clean. The in-flight gate round was stopped rather than
  allowed to finish: a log whose `HEAD after` stamp names a tree the rebase has
  replaced certifies nothing, and the rule is that the round must run on the
  candidate that will be pushed.

- Decision (snapshot, 2026-10-22): prove the wheel manifest regression before
  editing `cuprum/unittests/__snapshots__/test_maturin_build.ambr`, rather than
  refresh the snapshot and move on. Rationale: the failing diff showed two
  lines, `+ cuprum/_subprocess_outcome.py` and
  `- cuprum/sh/pipeline_output.py`. The first is the module this branch adds
  and is expected. The second is not: `cuprum/sh/pipeline.py` imports
  `PipelineOutput` and two siblings from that module, so if the entry really
  had left the wheel the package would fail at import, and the fix would be a
  packaging bug rather than a stale snapshot. Building a wheel with
  `maturin build --locked` and comparing its entry set against the snapshot
  settled it in one step — nothing present in the snapshot is missing from the
  wheel, and `_subprocess_outcome.py` is the only addition in either direction.
  The `-` line was the elided middle of syrupy's diff showing a neighbouring
  context line as though it were removed, the artefact the maturin snapshot
  memory already records. The fix is therefore one sorted insertion, not a
  regeneration: regenerating would have rewritten all 223 entries and destroyed
  the snapshot's value as a reviewable record of what the wheel contains. The
  insertion was made by hand and checked — 223 entries, still sorted, no
  duplicates — then `test_maturin_build.py` was run on its own to confirm 6
  passed with 1 snapshot passed.

- Decision (CodeScene review, 2026-10-22): flatten `_pipeline_payloads` in
  `cuprum/_pipeline_stream_results.py` rather than suppress the code-health
  thread. Rationale: the thread names the function this branch introduced in
  `cc0c63efe` — `git show origin/main:` on that module has no
  `_pipeline_payloads` at all — so it is a new finding about new code, not a
  pre-existing marker the branch happened to inherit. CodeScene's Bumpy Road
  Ahead counts nested-conditional blocks per function, and the loop nested an
  `if` around the stream-index selection inside its `else`. The fix splits the
  per-slot decode into `_slot_payload`, passes the shared inputs as a frozen
  `_SlotContext` (which also keeps the helper inside the positional-argument
  limit), and moves the stdout-versus-stderr index rule into `_slot_stream` so
  each helper makes one decision. Suppressing the marker was available in the
  thread's own footer and was rejected: the branch's other new modules carry no
  such marker, and a suppression would hide the next nesting the function
  grows. Behaviour is unchanged and the three deadline test modules still pass
  (34 passed, 8 skipped). Noted for the next round: this decision belongs to
  the same head as the source change, so the gate round must run on that head
  and no later plan edit may follow it.

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
   including the 0.5 s close grace. **Amended after review:** this step is
   scoped to a run that configured a deadline. `_deadline_for_collection`
   returns `None` when it did not, and `asyncio.timeout(None)` is not a short
   bound but no bound, so the literal instruction removed the settle window
   `main` still applies on that route. A run with no `timeout=` keeps
   `_settle_stdin_writer` — see `Decision log` and `Surprises & discoveries`.
2. On expiry, reclaim the writer and settle the owned group under **one**
   shield, through `_cleanup_direct_run`, then reconcile with an *empty*
   consumer bundle, so the group teardown that releases the descendant runs
   before the exception propagates. Two sequential `_shielded_cleanup` calls
   are not one shielded sequence: a cancellation arriving during the first
   would exit before the second was awaited and leave the group unsettled.
   `_cleanup_direct_run` composes both steps into a single shielded coroutine
   for exactly that reason, and the plan's `Constraints` and `Decision log`
   require it. **Amended after review:** the step originally named the writer
   cancellation alone.
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
descendant is reaped in `finally`. A fifth case, added after the review of the
rebased head, covers the no-`timeout=` route that the parametrization cannot
reach: every parametrized case configures `_DEADLINE_S`, which is exactly why
none of them could see the settle-window removal.

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
5. **I5 — no timeout configured means no deadline, not no bound.** A run
   without `timeout=` has no remainder to spend and raises no `TimeoutExpired`,
   but its post-exit writer wait is still bounded by the shared settle window.
   It must return in bounded time and report the child's own result rather than
   hanging. This invariant was first written as "a run without `timeout=`
   behaves exactly as before", which stated the same thing while this branch
   was still honouring it; the review of the rebased head found the branch had
   stopped doing so, and the invariant is now stated positively so the defect
   it missed is the thing it asserts.
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
- **I5:** `test_a_run_without_a_timeout_still_bounds_its_post_exit_writer_wait`
  runs the direct path with no `timeout=` against a descendant that never reads
  a payload larger than the pipe buffer, under `INHERIT`, and asserts the run
  returns with the child's exit code inside the harness budget. `INHERIT` is
  load-bearing for the same reason it is on the deadline arms: a group teardown
  under `OWN_GROUP` would release the writer and the case would pass on a run
  that never bounded anything. Discharge: deleting the `remaining is None`
  branch from `_await_writer_within_deadline` makes the case fail in 20.75 s
  with the intended message; restoring it passes in 0.87 s. A `timeout=`-free
  run whose descendant *does* eventually exit is also covered — both
  settle-cancellation cases and
  `test_a_second_cancel_during_the_direct_cleanup_still_settles_the_group` run
  without a deadline — but none of those pin the *bound*, which is why the new
  case exists.
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
make typecheck
make lint
make check-fmt
make markdownlint
make nixie
```

Expected: each focused module reports a non-zero collected count; the eight
Rust-parametrized arms may skip when the Rust extension is unavailable, which
is the one expected skip on this host, and there must be zero unexpected skips;
no test may run over the 10 s tolerance; `make test` reports no failures and no
warnings; and the six gate targets each exit zero. The Markdown pair is
included because the change surface touches `docs/`, and `make lint` already
depends on `make spelling` but not on `markdownlint`.

Gate runs are delegated to the `scrutineer` subagent, which runs them
sequentially and writes one log per gate under `/tmp`. A gate log certifies the
revision it recorded at its own head; after any further commit, the affected
gates must be re-run rather than the stale log cited.

## Outcomes & retrospective

All three post-exit routes are bounded, and the bound is the run's remaining
deadline rather than a fixed window: the direct run's stdin-writer
reconciliation (EP-M1), `SafeCmd.lines()`'s drain (EP-M2), and
`Pipeline.run()`'s stage-output collection (EP-M3). A fourth case the original
finding did not name turned up while implementing EP-M1 — a run that configures
no `timeout=` has no remainder to spend, so the direct path had to fall back to
the shared settle window rather than pass `None` through to `asyncio.timeout`,
which means "no bound" and would have re-introduced the hang on the one route
that cannot report it.

Overshoot past the deadline is the cleanup allowances and nothing else:
`cancel_grace` (0.5 s default), the 0.25 s capture EOF window once per
capturing run, the 0.5 s stdin close for a run given `stdin` data, and the
bounded one-second `_POST_KILL_SETTLEMENT_S` after a run with
`ProcessGroupPolicy.OWN_GROUP` forcibly kills its group — owed by a direct run,
a streamed run, a `lines()` run, and a pipeline alike, because whether a run
owes it is decided by the policy rather than by which entry point started it.
Each reaches that wait by its own route: the shared helper for the first three,
the handle coordinator's equivalent for an owned handle, and
`_terminate_all_shielded` for a pipeline. No route needed a design change; the
one deviation considered and recorded is the pipeline's deadline stamp, which
is taken when collection begins rather than at spawn.

The `INHERIT` arms discriminated as predicted, and that was the prediction
worth testing: under `OWN_GROUP` the expiry's teardown can signal the group,
which closes the pipe and releases the writer whether or not reconciliation
ever reached it, so an arm that only ran `OWN_GROUP` would pass on a fix that
bounded the await but never settled what it stopped waiting for. The `INHERIT`
arm is what separates a bound that was *entered* from one whose cleanup
actually *completed*, and it is the arm that failed when the settlement half
was deliberately removed.

The clock work was one invariant discovered three times over: a start
reference, a per-line `LineEvent.at` stamp, and a post-exit deadline arc are
meaningful only when all three are readings of one callable. Each route was
fixed separately before that was stated plainly, which is why the guards read
the way they do — the injected origin sits far above any real `perf_counter`
reading and advances in whole steps, so a stray real-clock read either floors
to exactly `0.0` or lands on a value the injected clock's gaps cannot explain.
That makes the assertions value-based rather than tolerance-based. The same
invariant is what the final duplication decision rests on: the construction now
lives beside the stamp that consumes it, so a caller cannot pass a start
reference without the clock it was taken from.

A future contributor should read `cuprum/_subprocess_stream_run.py` first: its
module docstring states the shape all three routes now follow — every await
between the child's exit and the completed gather sits inside one boundary
under one absolute deadline, because each can be cancelled and each needs the
same aftermath. ADR-021's addendum records why bounding that boundary is the
precondition for the settlement rather than a substitute for it.

Two process observations are worth carrying forward. First, a code-health
finding is cheaper to repair than to argue: three of this branch's four were
one hoisted predicate and two docstrings that had grown to restate their
module's own argument. Second, a test helper extracted for a code-health score
has to be shown still to bite — the two-cancellation case was re-run against a
deliberately broken production shield and failed with its own message before
the helper was kept.

The review round that closed this branch added a third, sharper one: a
reviewer's finding is a claim about the tree, not an instruction, and it can be
wrong in a way that the code it cites is responsible for. Finding 2 asked for a
settlement call that already existed; the reviewer had read
`_settle_owned_group_before_draining`'s docstring, which claimed a route matrix
the code did not implement. Acting on the suggestion would have broken the
success-path contract by signalling descendants the caller left running; the
docstring was the defect. Finding 1 was right in full, and the first-round
refusal of its second half was the plan's own error — corrected in the change
that threads the clock. The habit both reward is the one this plan has used
throughout: read the route the code takes, not the route its prose says it
takes, and re-check a refusal against the tree before recording it.

Certifying the result took three rounds, and the first two were wasted for a
reason worth naming. The round on `5ccaf3a35` was stopped because
`make check-fmt` objected to the plan's own wrapping and the fix would have
replaced the tree the round was certifying. The round on `7caf413e3` went red
on the wheel snapshot and, because pytest is a prerequisite of the Rust target,
left the Rust suite and the doctests unobserved — which is the part that
matters, since a red gate hides the ones behind it. Then `e2c7592b5` landed on
`main` while that round was running, so the head it had certified was already
based on a superseded target. The lesson is not "gate later": it is that the
unit of evidence is a head, not a branch. A gate log names one commit, and a
commit, an amend, a rebase, or a new base replaces it, so work that will be
rebased should be gated after the rebase and pushed without further commits.
The third round ran on `d95388a59` with HEAD and the tracked-tree change count
identical before and after every gate, and that is the only log set the push
cites. The snapshot failure also produced the branch's last judgement call: the
diff's `-` line was an artefact of syrupy eliding the middle of a long list,
and building a wheel to compare entry sets proved it, where regenerating the
snapshot would have hidden the question under 223 rewritten lines.

- Decision (second CodeRabbit review, 2026-10-22): re-verify every finding
  against the head named by the review's own `commit_id`, not against a range
  read from somewhere else. The `CHANGES_REQUESTED` at `04:32:30Z` is review
  `5609291319` on `58971f7ee` — the current head — while the
  `5da6d68fb..da5cbeaf7` range belongs to review `5597711617` from the day
  before. Quoting the wrong review's range made the exchange look like a
  stale-head problem when it was not. The finding verdicts were unaffected,
  because each was checked against the tree rather than against its range:
  findings 1 and 3 were valid when raised and already closed by `eda2c9695` and
  `81412e0cf` (branch-only commits, newer than `da5cbeaf7`), and finding 2's
  test premise — "the `OWN_GROUP` arm does not assert reclamation" — is false
  at every head, since the arm calls `assert_reclaimed` at
  `test_run_no_stream_post_exit_deadline.py:246`. CodeRabbit withdrew finding 2
  on that basis. Lesson for the next review round: a review has one commit, and
  the range printed inside it belongs to whichever review printed it.

- Decision (third CodeRabbit review, 2026-10-22): fix both new findings, and
  fix only the one cleanup site the finding names. The review's second finding
  is a genuine leak rather than a style point: `_EXITS_AND_HOLDS_STDIN` spawns
  its grandchild and exits immediately, so the pid file's existence is not
  guaranteed by the time the cancelled run returns, and `reap_descendant`
  treats an absent file as "nothing to reap". The sibling read in
  `test_the_direct_run_expiry_reports_the_configured_timeout` stays a single
  read: that case waits out a 1.0 s `TimeoutExpired` before returning, which
  gives the grandchild far longer than the write needs, and adding a blocking
  poll there would spend a bounded test's budget on a wait the scenario has
  already served. Rationale for recording the asymmetry rather than making the
  two uniform: the reviewer asked for a fix where the race exists, and widening
  it to a test that cannot hit it would be change for symmetry's sake.

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
