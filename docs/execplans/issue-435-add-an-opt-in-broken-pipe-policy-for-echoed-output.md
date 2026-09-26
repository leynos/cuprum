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
- **No existing positional argument changes meaning.** The policy field is
  `kw_only=True`. Declared mid-list, it would otherwise occupy `on_line`'s
  positional slot and renumber every field after it, silently rebinding the
  positional arguments existing callers already pass — a public break that no
  gate detects, because the only positional-contract test uses the first two
  fields. `test_broken_pipe_policy_does_not_take_a_positional_slot` pins the
  positional prefix and the keyword-only kind.
- **No pass-through wrappers.** The df12 `R9104 trivial-attribute-wrapper`
  check bans a method whose body merely forwards `self._helper(...)`. Any
  extraction made to satisfy a review must therefore land as a module-level
  function taking its collaborator explicitly, or be inlined at the call site.
  Suppressing the check is not an option; the rule exists to catch this
  indirection.

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
- [x] CodeRabbit review: a fifth pass, at `21618328`, returned three findings,
      one of them a real public-API break — the mid-list policy field took
      `on_line`'s positional slot. Now `kw_only=True`, with a regression test
      that fails when the fix is reverted. The other two were one request for
      extracting the metrics error dispatch, now `_record_error_metric`.
- [x] GitHub Actions at `7fc31a26`: every one of the 21 check runs is green or
      intentionally skipped, and all 12 required status contexts for `main`
      are present and successful. `mergeStateStatus` is `CLEAN`, down from
      `BLOCKED` while the run was in flight.
- [x] Independent gate run by a separate agent at `95b928bd`, confirming every
      deterministic gate: `check-fmt`, `lint` (ruff, interrogate 100%, pylint
      10.00/10, df12 lints, ambrleaks, skylos, clippy, whitaker, typos,
      yamllint, actionlint), `typecheck`, `markdownlint`, `spelling`, `nixie`,
      and `test` (2489 passed / 63 skipped, 125 nextest tests, 175 behaviour
      nodes, all three of this feature's scenarios passing).
- [x] Gate run on Revision 11's tree: five of seven gates passed, `lint` and
      `typecheck` failed, and both failures were genuine defects in the
      Revision 11 delta. `lint` aborted on `R9104` at the new
      `EchoMetricsHook.__call__` delegate; `typecheck` rejected the positional
      test's `seen.append` for `on_line`. Both fixed in Revision 12 (the
      dispatch moved to module level taking the collector as a parameter, and
      the test's callback replaced with a nested `def`). Re-verified in
      isolation: df12 lints clean, `ruff check` clean, `ruff format` clean,
      `ty check --python .venv` clean, and 45 focused tests pass.
- [x] Gate run on Revision 12's tree exposed one further failure, again created
      by the Revision 12 prose itself: `make check-fmt` reported
      `mdtablefix` wanting a `+7 -6` rewrap of the Surprises bullet that records
      the typecheck fix. Fixed by `make fmt` and committed as `a6b8ba14` — a
      pure line rewrap, no content change. GitHub Actions agreed: on
      `a6b8ba14` the `lint-test` job's `Check formatting` and `Lint Markdown`
      steps both pass, where `Check formatting` had failed on `a801de86`.
- [x] `make check-fmt` re-run on `a6b8ba14`: clean (`676 files already
      formatted`, `78 files left unchanged`).
- [x] `make test` re-run on `a6b8ba14` with the fix in place: exit 0, and this
      time the loop ran to the end. All nine pytest globs executed — 2490,
      638, 2, 116, 4, 126, 12, 21 and 22 passed, with the 63 expected skips —
      including the `tests/behaviour/` globs and
      `tests/integration/test_act_stream_parsing.py` that the doctest timeout
      had masked. `test-rust` ran too: 125 of 125 nextest tests passed. The
      flake that caused the masking did not recur, so the masking finding is
      closed as an observation about the recipe rather than a live defect.
- [x] Gate run on Revision 13's tree exposed a third self-inflicted failure,
      this one in the spelling gate rather than the formatter. The prose cites
      the commit `a6b8ba14`, and `typos` tokenizes before it applies its
      ignore list, so the SHA split into letter runs and the two-letter run
      between the digits was read as a misspelling of "be"/"by" — five errors,
      all from that one SHA. Fixed in
      `f60f04f2` by adding `[0-9][0-9a-f]{6,39}` to `typos.local.toml`'s
      `[patterns] ignore`; the leading digit is what keeps the rule narrow,
      since no English word begins with a digit, so ordinary prose stays
      checked. `typos`' regex engine has no lookahead, so a first attempt using
      one was rejected with "look-around, including look-ahead and
      look-behind, is not supported" — it reports as a whole-file parse error
      rather than a pattern error, which would have been easy to misread as a
      near-miss. The whole tree had exactly five errors before the change and
      zero after, with nothing else masked. This is the class of exemption
      `typos.local.toml` exists for: an externally fixed identifier that cannot
      be reworded without becoming wrong.
- [x] Independent gate run at the current tip `d4e7371c`: `check-fmt`,
      `markdownlint` (and therefore `spelling`), `nixie`, `typecheck` and
      `test` all pass. The `test` run is the decisive one, because the earlier
      masking is now closed on its own evidence: exit 0, `HEAD_DRIFT: no`, and
      all nine pytest globs ran to completion — 2490, 638, 2, 116, 4, 126, 12,
      21 and 22 passed with 63 expected skips — followed by `test-rust`'s 125
      of 125 nextest tests, 0 skipped. The glob list in the log, not the exit
      code, is what proves no target was masked.
- [x] `make lint` passes, at `f60f04f2`: exit 0 in 50 s, with ruff clean,
      interrogate at 100.0%, pylint at 10.00/10 under both the project config
      and the df12 plugin set, and ambrleaks, skylos, clippy and whitaker
      clean. This is the gate whose spelling failure opened this thread. The
      three commits above it are docs-only — `git diff --stat f60f04f2
      d4e7371c` is one `.md` file, 45 insertions and 10 deletions — so the
      only lint sub-check those commits could disturb is `typos`, which
      `markdownlint` re-ran at the tip and which passes.
- [x] CodeRabbit review: a sixth pass, at `a801de86`, returned zero findings
      across all 21 changed files, and the absence is verified rather than
      assumed. The CLI's own persisted record under
      `~/.coderabbit/reviews/` names the reviewed identity
      (`head: a801de86`, `base: 991dee6` = `origin/main`), and its
      `internalState.json` shows the evaluated diff contained both Revision 12
      fixes — `kw_only=True` present and `_record_error_metric` present three
      times — so the zero is an evaluation of the current tree, not a cached
      replay of an earlier pass. CodeRabbit's own file summary describes
      `__call__` as delegating to the dispatch, i.e. it accepted the
      module-level extraction rather than re-proposing the bound-method form
      that R9104 bans.
- [ ] CodeRabbit's *hosted* surfaces remain empty, and that emptiness is not
      evidence of a clean review. See the Surprises entry below: the bot
      declines to review draft PRs, so all six passes have been local CLI
      invocations and the `Kody Code Review` check-run is `skipped`, not
      `success`. Un-drafting the PR is expected to produce a first hosted
      review against the then-current tree.

## Surprises & discoveries

- Adding a defaulted field to the middle of `RunOutputOptions` is a **public
  API break**, not a private refactor. The class is `frozen=True, slots=True`
  but *not* `kw_only`, so every field up to `annotate_failure` is
  positional-or-keyword; a new field inserted before `on_line` takes its slot
  and shifts the rest. Every gate passed anyway — `make test` was green, and
  four CodeRabbit passes and a lint suite that includes ruff, pylint, and df12
  lints all missed it — because no test constructed the object with more than
  two positional arguments. The one existing positional-contract test,
  `RunOutputOptions(False, True)` in `test_idle_heartbeat.py`, uses only
  `capture` and `echo`, the first two fields, so it stays green under any
  insertion further down. The lesson is that "additive" for a dataclass means
  additive *at the end*; a mid-list field is only safe when declared
  `kw_only=True`. The fix follows the precedent `_synthesized_sink` already set
  in this very class.
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

- A review instruction followed **literally** can break a gate. CodeRabbit
  asked for `EchoMetricsHook.__call__` to "delegate to" a focused helper, and
  the literal reading — a method whose whole body is
  `self._record_error_metric(event)` — is exactly the pass-through wrapper this
  repository's own df12 lint rejects as `R9104 trivial-attribute-wrapper`. The
  detection is AST-based: a single-statement body forwarding an attribute chain
  rooted at one of the wrapper's own parameters, with the remaining parameters
  unchanged. Moving the same dispatch to module level as
  `_record_error_metric(collector, event)` keeps the reviewed intent while
  forwarding `self._collector`, an attribute access rather than the wrapper's
  own parameter, so no rule matches. This was verified against the plugin's own
  AST functions before the gates were re-run, rather than by guessing at the
  remedy or adding a suppression.
- Two review instructions on this task have now been *correct in intent but
  destructive as written*: the mid-list positional field (Revision 11) and this
  delegation (Revision 12). Both were caught by gates run on the tree the
  instruction produced, never by re-reading the instruction. The working rule
  this yields: implement the reviewer's intent, then let the gates adjudicate
  the spelling, and never accept a review instruction as validated because it
  reads sensibly.
- A test can pass at runtime and still be wrong. The positional-slot test bound
  `on_line` to `seen.append`, which works because Python does not enforce
  annotations, so `make test` was green while `make typecheck` rejected the
  same line as `invalid-argument-type`: the field is
  `Callable[[LineEvent], None]`, not `Callable[[str], None]`. The
  nested-callback precedent already existed in `test_stream_drain.py`. A green
  `make test` is therefore not evidence that a new test's callables are
  correctly typed, and `make typecheck` must be part of the gate set for any
  delta that adds one.
- CodeRabbit's GitHub app never reviewed this pull request, and the reason is
  this task's own delivery requirement. The bot skips draft PRs: the
  `Kody Code Review` check-run reads `skipped` with
  `output_summary: "Prerequisites validation failed."`, and its only artefact
  on the PR is a boilerplate notice reading "Draft PR not reviewed — Draft PRs
  are not automatically reviewed by default." All six passes have therefore
  been local `coderabbit review --agent` invocations, and the two hosted
  surfaces — inline threads and walkthrough rows — have never held a CodeRabbit
  object. The trap is that every web search of those surfaces returns *clean*:
  zero inline comments, zero `CHANGES_REQUESTED`, so a checker that reads
  absence as approval would conclude the review loop had converged six times
  when in fact the hosted reviewer has never run. This is the same class of
  error as the `isResolved` row above and the `.gitignore` gap in
  `find_dead_imports`: a query that cannot distinguish "nothing found" from
  "nothing looked at". The tell is that the check-run name is
  `Kody Code Review`, so a name search for `coderabbit` finds nothing and is
  easily mistaken for the app being absent. Consequence: un-drafting the PR is
  expected to produce a *first* hosted review, which is not scoped by any
  evidence gathered so far.
- A wrong identifier survives every gate, because the failure mode is a valid
  token rather than an invalid one. Revision 9's Progress entry named the
  scanner `ambrieaks`; the real tool is `ambrleaks`, per Makefile:237 and
  ADR-003. The misspelling entered at `21618328`, when Revision 10 was the
  current note, and left at `e986abf8`, seven commits later. Over that span the
  spelling gate reached the file three times and never once had an opinion
  about the misspelling. Twice it passed a tree containing `ambrieaks` outright
  — inside `make lint` at `f60f04f2` and via `markdownlint` at `d4e7371c` —
  because neither spelling is a dictionary word, so `typos` saw nothing to
  flag. The third run, `make lint` at `a6b8ba14`, did fail at the spelling
  step, but on the unrelated `a6b8ba14` commit-SHA token and it aborted there,
  so it never reported on `ambrieaks` either way. No other gate reads prose for
  correctness, and CodeRabbit's sixth pass at `a801de86` reviewed a tree
  containing it and returned zero findings. It surfaced only when a new
  sentence cited the same tool a second time and the two spellings disagreed
  with each other; confirming which was right then took one `git grep` against
  the Makefile. The lesson is that spelling, lint, and type gates give no
  protection against a *confidently wrong* name for an external tool.
  Cross-check a cited identifier against its definition the first time it is
  written down, because nothing downstream will.
- `make test`'s `test-python` recipe is a `for` loop over the pytest globs with
  `|| exit $$?`, so the first failing glob aborts the loop and silently masks
  every later glob *and* `test-rust`. In this task a single known-flaky doctest
  timeout masked eight of nine globs — including the whole of
  `tests/behaviour/` — while the run still looked like "one flake". The
  isolation evidence was sound (0.82 s against a 30 s bound, a 36× margin, in a
  module importing none of the changed symbols), but the masking is a separate
  defect from the flake, and only the per-glob log shows which targets actually
  executed. Treat the tail of a `make test` log as evidence of what ran, never
  the exit code.

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
- Decision: do **not** split `cuprum/unittests/test_public_api.py`, now 415
  lines, despite the `Tolerances` trigger naming the 400-line ceiling. The
  tolerance is scoped to "any touched production module", and this is a test
  module that `pylint` does not walk at all — `cuprum/unittests` has no
  `__init__.py`, so the recursive walk never descends into it, which is why the
  file has sat above the ceiling without failing `make lint`. Thirty-three test
  modules already exceed 400 lines, the largest at 993. Splitting it would
  contradict the local convention to satisfy a rule that does not apply, so the
  trigger is judged not to fire. Recorded rather than left implicit because the
  FileLength check is a live question on every future touch of this file; if
  the project ever adds `__init__.py` under `cuprum/unittests` or widens
  pylint's walk, this decision must be revisited.

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

Revision 14: no code changed. Two docs-only commits close the findings the
earlier gate runs left open and correct one spelling. `7224bcde` records the
tip's gate evidence: the independent run at `d4e7371c` passed `check-fmt`,
`markdownlint` (and therefore `spelling`), `nixie`, `typecheck` and `test`, with
`make test` exiting 0 at `HEAD_DRIFT: no` after all nine pytest globs ran to
completion — 2490, 638, 2, 116, 4, 126, 12, 21 and 22 passed with 63 expected
skips — followed by 125 of 125 nextest tests. That closes the masking finding
on its own evidence, since the glob list rather than the exit code is what
proves no target was skipped. The same entry records `make lint` passing at
`f60f04f2` in 50 s; the three commits above it are docs-only, so `typos` is the
only lint sub-check they could disturb, and `markdownlint` re-ran it at the tip.
`e986abf8` then corrects `ambrieaks` to `ambrleaks` in the Revision 9 evidence
entry, matching Makefile:237 and ADR-003. The misspelling had survived every
gate and six CodeRabbit passes because `ambrleaks` is not a dictionary word in
either direction, so no checker had an opinion about it; it was found by
grepping the tree for the real target's name while adding a second citation of
it, not by any gate.

Revision 13: no code changed. The gate run on Revision 12's tree failed
`make check-fmt` because the prose Revision 12 added to this file — the
Surprises bullet about the typecheck fix — was wrapped at a width `mdtablefix`
rejects; a `+7 -6` rewrap is all it wanted. `make fmt` applied it and it landed
as `a6b8ba14`, a docs-only commit. GitHub Actions confirms the fix
independently: on `a801de86` the `lint-test` job died at its `Check formatting`
step, and on `a6b8ba14` that step passes and the job runs on through
`Lint Markdown` and skylos to `success`. The Revision 13 prose then failed the
*spelling* gate, for a fourth self-inflicted reason and an instructive one:
citing the commit `a6b8ba14` in prose made `typos` read the SHA's two-letter
run between the digits as a misspelling of "be"/"by", since it tokenizes before
applying its ignore list. `f60f04f2` exempts abbreviated SHAs with
`[0-9][0-9a-f]{6,39}` — narrow because the leading digit means no English word
can match it. The pattern matches the digit-leading *run*, not the whole SHA,
which is what lets a letter-leading SHA such as `f60f04f2` qualify through its
`60f04f2` tail. Two findings from the same gate run are recorded rather than
fixed. First, CodeRabbit's hosted review never ran, because the bot declines to
review drafts — so the six passes behind this plan are all local CLI runs, and
the hosted surfaces being empty means *not reviewed*, not *nothing to report*;
un-drafting will elicit a first hosted review that no evidence here covers.
Second, a known-flaky doctest timeout masked eight of nine pytest globs and all
of `test-rust`, because `make test`'s glob loop exits on the first failure; the
glob list in the log, not the exit code, is what says which targets ran. That
one is now closed on its own evidence: a re-run at `a6b8ba14` completed all
nine globs and all 125 Rust tests with the flake absent. The reviewed SHA has
therefore advanced past the reviewed tree by three commits, all docs or
spelling config.

Revision 12: the gate run on Revision 11's tree failed two gates, both on the
Revision 11 delta itself, and both are now fixed. `make lint` aborted
`python-lint` on `R9104 trivial-attribute-wrapper` at
`cuprum/adapters/echo_metrics.py:147`: taking CodeRabbit's request to "have
`__call__` delegate to it" literally produced a method whose entire body was
`self._record_error_metric(event)`, which is precisely the pass-through shape
this repository's own df12 lint bans. The dispatch now lives at module level as
`_record_error_metric(collector, event)` and `__call__` calls it with
`self._collector` — CodeRabbit's intent (one place mapping category to counter)
is kept, but the forwarded operand is an attribute access rather than the
wrapper's own parameter, so neither `R9104` nor its `R9105` alias form matches.
The plugin's documented remedy is to call the target directly rather than
suppress, and a suppression was never an option. `make typecheck` also failed at
`cuprum/unittests/test_public_api.py:237`: the positional-slot test passed
`seen.append` for `on_line`, which binds at runtime but is typed
`Callable[[LineEvent], None]`, so `ty` rejected it as `invalid-argument-type`.
The test now uses a nested `def record(event: LineEvent) -> None`, matching the
nested-callback precedent in `test_stream_drain.py`. Both failures were genuine
branch regressions rather than flakes or tooling artefacts: scrutineer verified
that the R9104 target and the failing typecheck line each exist only in the
uncommitted delta and not at `HEAD`. This is also the second time in this task
that a review instruction, followed literally, would have broken a gate — the
first was the mid-list positional field — which is why the gates are run before
each review is accepted rather than after.

Revision 11: a fifth CodeRabbit pass, at `21618328`, returned three findings,
and one of them was a genuine defect in the shipped code rather than in prose.
Inserting `broken_pipe_policy` after `max_echo_line_bytes` took the positional
slot that `on_line` had held: `RunOutputOptions` is a public, non-`kw_only`
dataclass, so a caller passing `on_line` positionally would have had its
callback bind to the new field and be rejected by the policy parser, while
`on_line` silently fell back to `None`. The field is now declared with
`kw_only=True`, which moves it to the signature tail and restores the
positional order every existing caller relies on, matching the precedent
`_synthesized_sink` already set in the same class. A regression test,
`test_broken_pipe_policy_does_not_take_a_positional_slot`, pins both the
positional prefix and the keyword-only kind; reverting the fix makes it fail,
so it is a real control and not a restatement of the implementation. The other
two findings were one request seen twice — extract the three-branch error
dispatch in `EchoMetricsHook.__call__` into a focused helper — and it is now
`_record_error_metric`. Revision 10's claim that no further revision was
expected was wrong, and this is the reason the plan keeps a revision history:
the review found a public-API break that four earlier passes and every gate had
passed over.

Revision 10: the fourth CodeRabbit pass, at `95b928bd`, returned zero findings
across all twenty-one changed files. The review requirement is therefore
discharged: three consecutive passes produced progressively narrower findings
(four behavioural-adjacent, then one, then four presentational, then none), and
every concern raised along the way has been addressed rather than argued away.
No further revision was expected at the time; the plan stays COMPLETE and the
only remaining step is the pull request's own CI and merge.

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
