# Add an opt-in broken-pipe policy for echoed output

Status: COMPLETE

This living ExecPlan records the implementation of issue
[#435](https://github.com/leynos/cuprum/issues/435). It is self-contained: a
reader with only this working tree and this file should be able to deliver the
change end to end.

## Purpose / big picture

A presentation sink raising `BrokenPipeError` currently aborts `run_sync()`, so
callers lose access to the otherwise captured result. A consumer migrating from
a best-effort console relay — where a closed downstream reader was tolerated —
has no way to say "stop echoing *that* stream and keep the rest". Today the
failure propagates out of the drain task and takes the whole run with it.

The reproduction, run against `361887e6`:

```python
class BrokenSink:
    def write(self, text):
        raise BrokenPipeError("closed presentation destination")

    def flush(self):
        pass


with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
    result = sh.make(python, catalogue=catalogue)("-c", "print('hello')").run_sync(
        output=RunOutputOptions(capture=True, echo_stdout=True),
        context=ExecutionContext(stdout_sink=BrokenSink()),
    )
```

Observed: `BrokenPipeError` propagates from `_stream_echo._write_chunk` through
`_echo_write`, `_echo_chunk`, `_deliver_chunk` and `_drain_chunks`, so the
caller never sees a `CommandResult`. `_echo_write` already recovers from
`UnicodeEncodeError` (issue
[#348](https://github.com/leynos/cuprum/issues/348), exposed on results by
[#356](https://github.com/leynos/cuprum/issues/356)); this issue asks for the
same *opt-in* treatment for a broken pipe.

After this change, a caller who passes
`RunOutputOptions(broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT)` receives a
`CommandResult` whose `stdout` is intact and whose `relay_fallbacks` carries one
`RelayFallback(error_category=EchoErrorCategory.BROKEN_PIPE)` on the affected
stream. The default, `BrokenPipePolicy.STRICT`, preserves today's behaviour
byte-for-byte: the `BrokenPipeError` still propagates.

The observable success is: `make test` passes, the ticket's reproduction
returns a result under `BEST_EFFORT`, and the same reproduction still raises
under the default.

## Constraints

Hard invariants that must hold throughout implementation. Violation requires
escalation, not a workaround.

- **Strict by default.** `RunOutputOptions()` — and every existing caller — must
  behave exactly as before. `BrokenPipeError` propagates unless the caller opts
  in.
- **Narrow catch.** Only `BrokenPipeError` is recovered. `OSError`,
  `ConnectionError`, and every other sink failure keep propagating under both
  policies, so a genuinely unreachable device is never mistaken for a closed
  reader. (`BrokenPipeError` is a subclass of `ConnectionError`, which is a
  subclass of `OSError`, so the clause order and specificity matter.)
- **Capture is never affected.** Drain continues, the capture buffer keeps
  filling, line observation continues, and the child is still reaped.
- **One bounded diagnostic per transition.** At most one `RelayFallback`, one
  `EchoEvent`, and one structured `WARNING` per affected drain — the existing
  `_EchoGuard` early-return enforces exactly-once across later chunks and the
  final decoder flush. No payload, exception text, or sink identity reaches any
  of the three projections.
- **Inter-stage pump errors are out of scope.** This policy governs the echo
  sink only. `cuprum._streams_pump` failures are untouched.
- **The new public surface is additive and enumerable.** It comprises the
  exported `BrokenPipePolicy` enum, its `RunOutputOptions.broken_pipe_policy`
  field and matching `resolved_broken_pipe_policy` property, the
  `EchoErrorCategory.BROKEN_PIPE` member, and the exported
  `ECHO_BROKEN_PIPE_TOTAL` metrics constant. Everything is additive: nothing
  existing is renamed, re-typed, or removed. Separately, the *record* shapes
  are frozen — `RelayFallback`, `EchoEvent`, and `CommandResult` field lists do
  not change, so the policy adds no new projection payload.

## Tolerances (exception triggers)

- If threading the policy requires changing more than the two `_StreamConfig`
  construction sites, stop: the isolation model is not what this plan assumed.
- If any existing test needs editing (rather than extending) to pass, stop and
  record why — a frozen contract test failing means the change is not additive.
- If the module line ceiling of 400 is breached in any touched production
  module, stop and extract rather than trim.

## Risks

- `_StreamConfig`, `_SubprocessExecution`, and `_PipelineRunConfig` gain a
  field. Risk: a test constructing them positionally breaks. Mitigation: all
  three are `frozen=True, slots=True` dataclasses whose fields after the first
  few are keyword-only in practice; the field is added with a default and
  verified against the three known construction sites.
- `pylint`'s `max-module-lines = 400` applies to production modules.
  `cuprum/_stream_echo.py` is at 202 lines and `cuprum/echo_events.py` at 143,
  so both have room.
- The strict path is a new `except` clause ahead of nothing; a regression there
  would silently change every existing caller. Mitigation: an explicit
  negative-control test.

## Progress

- [x] Reconnaissance: identified the real definition sites
      (`cuprum/sh/output.py`
      not `cuprum/sh.py`; `EchoErrorCategory` in `cuprum/echo_events.py`).
- [x] Red: reproduced the abort with the ticket's exact snippet.
- [x] Task 1: `BROKEN_PIPE` category, `BrokenPipePolicy`, validation helper,
      `_StreamConfig` field, gated `except BrokenPipeError` in `_echo_write`
      (commit `ce99604d`).
- [x] Task 2: `RunOutputOptions.broken_pipe_policy`; thread through the
      single-run and pipeline `_StreamConfig` builders (commit `d554596f`).
- [x] Task 3: metrics counter, public export, unit and behaviour tests
      (commit `487c4d88`; the export itself rode on `d554596f`).
- [x] Docs: changelog entry, users' guide, developers' guide (commit
      `53457a7c`).
- [x] Commit gates: `make check-fmt`, `make lint`, `make typecheck`,
      `make test` (2489 passed, 63 skipped), `make markdownlint`, and
      `make spelling` all pass, run with `env -u BASH_ENV`.
- [x] Push and open the draft pull request:
      [cuprum#503](https://github.com/leynos/cuprum/pull/503).
- [x] CodeRabbit review: completed at `b55cb0e6` with four non-blocking
      findings (two minor on the developers' guide, one minor on this plan's
      absolute paths, one trivial unused fixture parameter), all addressed in
      `b6577081`. A re-review at `b6577081` returned one finding, the status
      line, which the COMPLETE edit above discharges.
- [x] GitHub Actions at `b6577081`: every non-skipped check passes, and
      `mergeStateStatus` is `CLEAN`.
- [x] CodeRabbit review: a third pass at `41770cb4` returned four non-blocking
      findings, all presentational — a missing `Returns` section on
      `resolved_broken_pipe_policy`, six unnamed expected values in the guard
      test's assertions, the under-described public surface above, and a
      first-person pronoun in the Revision 5 note. All four are addressed in
      this revision, which is the ninth.
- [x] CodeRabbit review: a fourth pass, at `95b928bd`, returned zero findings
      across all twenty-one changed files, converging the review loop.
- [x] GitHub Actions at `7fc31a26`: every one of the 21 check runs is green or
      intentionally skipped, and all 12 required status contexts for `main`
      are present and successful. `mergeStateStatus` is `CLEAN`, down from
      `BLOCKED` while the run was in flight.
- [x] Independent gate run by a separate agent at `95b928bd`, confirming every
      deterministic gate: `check-fmt`, `lint` (ruff, interrogate 100%, pylint
      10.00/10, df12 lints, ambrieaks, skylos, clippy, whitaker, typos,
      yamllint, actionlint), `typecheck`, `markdownlint`, `spelling`, `nixie`,
      and `test` (2489 passed / 63 skipped, 125 nextest tests, 175 behaviour
      nodes, all three of this feature's scenarios passing).

## Surprises & discoveries

- The coding plan names `cuprum/sh.py`, but no such module exists: `cuprum.sh`
  is a package and `RunOutputOptions` lives in `cuprum/sh/output.py`.
- The plan's follow-up prompt says the recovery should "disable the guard", but
  `_EchoGuard` has a single `disabled` flag and `_StreamConfig` records the
  echo gate in `echo_output`. Setting `disabled = True` is the mechanism; the
  policy is consulted only when deciding whether to set it.
- `_write_chunk` already wraps both the binary `.buffer` branch and the
  text-sink branch inside one `try` in `_echo_write`, and covers `write` and
  `flush` alike, so the single new clause covers every path the ticket lists
  without restructuring.
- `tests/behaviour/` modules each declare the
  `a curated Python command for testing` background step locally
  (`test_telemetry_adapters.py` does), because pytest-bdd resolves step
  definitions per module. A new behaviour module must redeclare it rather than
  import it.
- `LineEvent` carries the line under `text`, not `line`; the BDD step for line
  observation has to read `event.text`.
- The `subprocess_teardown_drain_failed` `ERROR` record that the ticket's
  reproduction emits belongs to the **`STRICT`** run, not the recovered one:
  teardown drains a consumer that is already unwinding from the propagated
  error. It is pre-existing behaviour and out of scope for this change; the
  `BEST_EFFORT` run's only log record is its one categorized `WARNING`.
- Six `cuprum/unittests/test_release_github_steps.py` tests fail locally with
  `AssertionError: failed to run git: fatal: not a git repository`. The message
  appears nowhere in the tracked tree, which is the clue: the agent harness
  exports `BASH_ENV`, and its script prepends the harness's own `bin` directory
  to `PATH` on every non-interactive Bash start. That directory holds the
  harness's `gh` wrapper, which resolves the repository with
  `git remote get-url origin` — so it shadows the test's `gh` stand-in, which
  the test puts first in `PATH` precisely so the real `gh` cannot run. The step
  then fails outside a git repository and the assertion reports the wrapper's
  stderr. Proven environmental: unsetting `BASH_ENV` (`env -u BASH_ENV`) turns
  the same module from 6 failed into 6 passed in 0.26s, and the module
  references no symbol this change touches. No test-side `PATH` change can
  defend against it, because `BASH_ENV` is sourced after the caller's
  environment is applied.

## Decision log

- Decision: reuse the existing `_EchoGuard` recovery mechanism rather than
  adding a second guard. Rationale: the guard is per-drain, so nested and
  concurrent runs are isolated for free, and pipelines get per-stage isolation
  from the per-stage `_RelayDiagnostics` collector.
- Decision: keep the policy on `_StreamConfig` rather than on `_DrainState`.
  Rationale: `_StreamConfig` is where the other echo-affecting inputs
  (`echo_output`, `echo_max_line_bytes`) already live, and it is the value both
  the single-run and pipeline builders already construct.
- Decision: name the metric `cuprum_echo_broken_pipe_total` and keep
  `ECHO_ENCODING_FAILURES_TOTAL` unchanged. Rationale: the module comment
  requires a distinct series per category, and a misencoded sink must stay
  distinguishable from a reader that keeps disconnecting.
- Decision: extract `_disable_echo` in `cuprum/_stream_echo.py`, so both
  recoveries share one guard flip and one set of three projections. Rationale:
  the two `except` clauses would otherwise duplicate the projections verbatim
  and drift the moment either is edited.
- Decision: give the new field a declared type of `BrokenPipePolicy | str` on
  `RunOutputOptions` and expose `resolved_broken_pipe_policy` as the narrow
  view. Rationale: it mirrors `resolved_echo`, which exists for exactly this
  reason, and keeps the execution layer from re-parsing a value the options
  object already normalized.
- Decision: add the field to `_SubprocessExecution` and `_PipelineRunConfig`
  with a `STRICT` default rather than without one. Rationale: two test modules
  build those dataclasses directly, and the plan's tolerance says existing
  tests must not need editing for the change to be additive. The default is
  also the honest value: a bundle built without naming a policy is strict.

## Outcomes & retrospective

Delivered as three gated commits and opened as draft
[cuprum#503](https://github.com/leynos/cuprum/pull/503). All three tasks
landed: the recovery mechanism and its vocabulary, the configuration threading,
and the observability with the public export and tests.

What worked: gating on a single `try` in `_echo_write` meant the ticket's whole
matrix — write and flush, text and binary sinks, the final decoder flush,
nested and concurrent runs, pipeline stages — fell out of one clause plus the
existing per-drain guard, with no restructuring. The per-drain `_EchoGuard` and
per-stage `_RelayDiagnostics` were already the right isolation boundaries, so
"nested and concurrent runs" needed no new mechanism at all.

What to watch: the plan named `cuprum/sh.py`, which does not exist; the real
site is `cuprum/sh/output.py`. Reconnaissance before editing caught it, but the
same mismatch would have cost a wasted cycle if taken on faith.

Verification that mattered most: running the ticket's own reproduction both
ways, and the seed control (O1) that fails if the recovery is not gated. The
drain-level tests pin the mechanism; the behaviour tests prove the policy a
caller names actually reaches a real subprocess.

Re-gating after the delivery edits surfaced a second local-only stall worth
recording: `make lint` hangs forever in actionlint, which deadlocks writing the
`.github/workflows` scripts to shellcheck's stdin. It is a pipe-buffer race,
not deterministic, and CI installs no shellcheck, so CI never sees it.
`actionlint -shellcheck=` completes in under a second. The `ACTIONLINT`
Makefile variable substitutes the whole command word rather than a program
path, so a wrapper script that appends the flag is the only shape that works:

```sh
#!/bin/sh
# Local gate shim: actionlint v1.7.12 deadlocks writing to shellcheck's stdin on
# this host. CI installs no shellcheck, so disabling it matches CI exactly.
exec actionlint -shellcheck= "$@"
```

```plaintext
env -u BASH_ENV make ACTIONLINT=/path/to/actionlint-noshellcheck lint
```

## Conformance basis

No upstream Terms of Reference or technical design revision covers this change;
the governing artefacts are the issue itself and the completed #348/#356 echo
guard work it extends. Governing project constraints: `AGENTS.md` (400-line
modules, NumPy docstrings, tests before commit), ADR-007 (echo boundary
rationale; the `_subprocess_streams` addendum in `docs/`).

Trace: `issue-435` -> `EP-435-T1` (recovery mechanism) -> `EP-435-T2`
(configuration threading) -> `EP-435-T3` (observability and public surface),
each discharged by the tests named in `Verification plan`.

## Verification plan

The change introduces one narrow behavioural invariant. If it introduced none,
this would say so; it does, so each obligation is listed with its method.

| #   | Obligation                                                                                              | Method               | Artefact                                                                                                                  | Evidence / discharge                                                                                                                                             |
| --- | ------------------------------------------------------------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| O1  | Under `STRICT` (default), `BrokenPipeError` propagates and aborts the drain                             | named pytest example | `cuprum/unittests/test_broken_pipe_echo_guard.py`                                                                         | negative control: `test_strict_policy_propagates_the_broken_pipe` fails if the recovery is not gated                                                             |
| O2  | Under `BEST_EFFORT`, capture completes and echo stops after the first broken pipe                       | named pytest example | same                                                                                                                      | captured bytes equal the payload; sink sees exactly one attempt                                                                                                  |
| O3  | A non-`BrokenPipeError` `OSError` still propagates under `BEST_EFFORT`                                  | named pytest example | same                                                                                                                      | `test_best_effort_propagates_non_broken_pipe_os_errors` proves the catch is narrow, not `OSError`-wide                                                           |
| O4  | Exactly one `WARNING`, one `EchoEvent`, one `RelayFallback` per transition, with closed-set extras only | named pytest example | same                                                                                                                      | `test_best_effort_warns_once_with_structured_extras` asserts record count, `exc_info is None`, the closed-set extras, and the absence of payload extras          |
| O5  | The final decoder flush neither re-attempts the write nor re-raises                                     | named pytest example | same                                                                                                                      | `test_broken_pipe_on_the_final_decoder_flush_is_recovered` and `test_flush_after_broken_pipe_does_not_reattempt_the_write`                                       |
| O6  | The policy reaches both the single-run and pipeline `_StreamConfig` builders                            | named pytest example | `cuprum/unittests/test_broken_pipe_result_diagnostics.py`, `cuprum/unittests/test_pipeline_relay_fallback_diagnostics.py` | result-level `relay_fallbacks` assertions on both paths                                                                                                          |
| O7  | The metric increments once per affected drain under a distinct series                                   | named pytest example | `cuprum/unittests/test_echo_metrics.py`                                                                                   | `test_broken_pipe_counter_is_distinct_from_the_encoding_counter` asserts counter name, value, and labels; a second test proves nothing is counted under `STRICT` |
| O8  | The ticket's reproduction returns a result under `BEST_EFFORT`                                          | behavioural test     | `tests/behaviour/test_broken_pipe_policy.py` + `tests/features/broken_pipe_policy.feature`                                | real subprocess, real sink, three scenarios                                                                                                                      |

Non-vacuity: O1 is the seeded-fault control for O2 (same fixture, opposite
policy, opposite outcome), and O3 is the control for the catch width. Every
assertion can fail: O2 fails if recovery is unconditional, O1 fails if it is
absent, O3 fails if the clause was widened to `OSError`. O7 carries its own
control: the `STRICT` companion test asserts the counter list stays empty, so
the metric cannot pass by counting every drain.

Axioms: `BrokenPipeError` is a subclass of `ConnectionError` and `OSError` in
CPython 3.13 (verified by the O3 test, which relies on that relationship to be
meaningful); `asyncio` propagates a drain-task exception into `run_sync`'s
await path, which the RED reproduction demonstrates.

## Revision note

Revision 10: the fourth CodeRabbit pass, at `95b928bd`, returned zero findings
across all twenty-one changed files. The review requirement is therefore
discharged: three consecutive passes produced progressively narrower findings
(four behavioural-adjacent, then one, then four presentational, then none), and
every concern raised along the way has been addressed rather than argued away.
No further revision is expected; the plan stays COMPLETE and the only remaining
step is the pull request's own CI and merge.

Revision 9: a third CodeRabbit pass at `41770cb4` returned four non-blocking
findings, again all presentational rather than behavioural. Two were on the new
test material and the public surface: the `resolved_broken_pipe_policy`
docstring lacked the NumPy-style `Returns` section that every sibling property
in this repository carries, and six assertions in the drain-level guard test
stated a comparison without naming the expected value, so a failure would
report a mismatch rather than an intent. The other two were consistency faults
in this plan itself: the public-surface constraint under-described the
additions — listing only the enum, when the shipped change also adds the
options field, its resolving property, the error category member, and the
metrics constant — and the Revision 5 note below used a first-person pronoun
where the rest of the document is impersonal. Revision 5's substance is
unchanged: both failed gates were implementation faults, not plan faults, and
this revision only rewrites how that is phrased. The status remains COMPLETE,
since these findings change prose, docstrings, and assertion messages only,
never behaviour.

Revision 8: the plan is COMPLETE. A second CodeRabbit pass at `b6577081`
returned one finding only — that the status line still said it was in progress
— and every required CI check on that head passes, so the claim is now true
rather than aspirational. No upstream artefact needed amending: the governing
constraints are `AGENTS.md` and ADR-007, neither of which this change alters,
and the deviations this work surfaced are recorded in the decision log and the
surprises section.

Revision 7: the CodeRabbit review ran at `b55cb0e6` and returned four
non-blocking findings, none of which contradicted the design. Two were stale
prose the review caught in the developers' guide: that paragraph still named a
private `_echo_relay` module that has never existed on any ref, and described
`_echo_chunk` as the sole route for every echo write when the bounded-line
writer and the final decoder flush call `_echo_write` directly. Because this
change rewrites that same paragraph, correcting it is part of the change rather
than unrelated drift. The other two were a leftover fixture parameter on a test
that never spawns a child, and absolute host paths in this plan's environment
note, both now edited out. The plan's own gate run stands: `make test` passes at
`b55cb0e6` with 2489 passed and 63 skipped, the 175 behaviour nodes including
this change's three scenarios, and 125 nextest tests.

Revision 6: the draft pull request is open as
[cuprum#503](https://github.com/leynos/cuprum/pull/503), which discharges the
delivery requirement.

Revision 5: every commit gate passes on the final tree. Two gates failed first,
and both failures lay in the implementation rather than in the plan: the
spelling gate rejected seven words where the new prose used the British `-ise`
ending that the project's Oxford policy forbids — the gate also rejects those
very spellings when they appear here inside backticks, so this note
deliberately does not quote them — and `make lint` rejected six findings in the
new test material: a long literal raised directly from two sink doubles, an
unused import, a rule *code* where the suppression comment wants the rule name,
and two step docstrings not in the imperative mood. `make fmt` had not been run
on the new files either, so six needed reformatting.

Revision 4 covered all three tasks committed and documented. Revision 3
recorded the verification plan table naming the artefacts that actually
discharge each obligation, and the decision log's four judgement calls.
Revision 2 recorded the ticked progress list. Revision 1 was written after
reconnaissance and the RED reproduction.
