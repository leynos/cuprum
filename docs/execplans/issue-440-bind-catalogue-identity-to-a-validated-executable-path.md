# Bind catalogue identity to a validated executable path

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE — the feature is finished and every local gate passes. All
twelve required CI contexts from the `main-required-checks` ruleset have been
verified green at each head reached so far, at the time each was the pushed
head; whether they are green at the *current* head is a live question with a
fresh answer, and is recorded in `Progress` rather than asserted here. All
eleven findings from the CodeRabbit review of `6efb42b9` are applied and the
closing review at `c738c78a` returned zero findings. A further
`coderabbit review --agent` pass, run through the CLI because the draft pull
request makes the CodeRabbit app check skip itself, returned two advisory
findings; both are applied. The specific head is likewise recorded in
`Progress` rather than here, since naming it in this line has gone stale every
time a commit follows. The work is pushed and draft pull request #571 is open.

## Purpose / big picture

Cuprum currently uses one name for two different jobs. A `Program` is the
logical identity of an executable: it is what a `ProgramCatalogue` allowlists,
what the active context permits, what `project` metadata hangs off, and what
every telemetry record reports. It is *also* the string handed to the operating
system as `argv[0]`.

That conflation forces a bad choice on any caller whose logical name and
on-disk executable differ. A tool such as `sccache` may be installed at a
version-pinned absolute path, inside a virtual environment, or replaced by a
controlled stub during tests. Registering the full path in the catalogue makes
the path the identity, so allowlist entries, policy scopes, log records, and
metric labels all carry `/opt/toolchains/1.79/bin/sccache` instead of
`sccache`. Registering the bare name instead is worse: it either fails to find
the intended binary or, if the caller pre-resolves it, weakens the allowlist by
accepting any executable that happens to share the basename.

After this change a caller can do all of the following, and the distinction
between identity and executable is explicit in the types:

1. Keep a bounded logical label (`Program("sccache")`) as the catalogue
   identity, the allowlist subject, and the telemetry subject.
2. Bind that logical label to an approved executable path, or to a resolver
   that computes one at spawn time, inside a scope whose lifetime is the
   scope's own.
3. Read back the exact path that was executed from `CommandResult.resolved_path`
   and from the observe-event stream.

The approach is a per-context binding map, not a catalogue change:

```python
from cuprum import Program, ProgramCatalogue, bind_executable, sh

SCCACHE = Program("sccache")
catalogue = ProgramCatalogue.from_programs(
    SCCACHE,
    "cc",
    name="build",
)

with bind_executable(SCCACHE, "/opt/toolchains/1.79/bin/sccache"):
    command = sh.make(SCCACHE, catalogue=catalogue)("-s")
    result = command.run_sync()
    assert result.resolved_path == "/opt/toolchains/1.79/bin/sccache"
```

The allowlist still gates on `SCCACHE`, telemetry still reports
`program="sccache"`, and `resolved_path` reports what actually ran. An
unapproved path that shares the basename is rejected by the existing allowlist,
because the binding never participates in allowlist enforcement.

## Constraints

Hard invariants that must hold throughout implementation. Violation requires
escalation, not a workaround.

- **The allowlist stays authoritative.** `CuprumContext.is_allowed` and
  `CuprumContext.check_allowed` keep operating on `Program` alone.
  `resolve_executable` is a lookup, never a permission check, and adding a
  binding for a program outside the allowlist must not make that program
  runnable.
- **Resolution happens after enforcement.** Every spawn path resolves the bound
  executable strictly after `_enforce_allowlist` has run for that command. A
  binding therefore cannot admit a program the allowlist would reject.
- **Identity is unchanged everywhere it is already reported.**
  `ExecEvent.program`, `ExecEvent.project`, the metrics `program` label, the
  logging `program` extra, the tracing `program` attribute, the sink session
  start record, and `CommandResult.program` all keep carrying the logical
  `Program`. The executed path is *additional*, never a replacement.
- **`ExecEvent` is append-only after `exec_id`.** `ExecEvent` is a public
  dataclass with positional fields, and
  `cuprum/unittests/test_public_api.py::test_exec_id_keeps_its_positional_slot`
  pins `exec_id` directly after `error_type`. `resolved_path` is therefore
  declared after `env_mode`. The rebase onto main (#575) reordered this: main
  appends its own `terminal_outcome` after `env_mode` and
  `test_terminal_outcome_public_api` pins that field as the declaration tail, so
  `resolved_path` sits *before* `terminal_outcome` rather than at the end.
  Both follow every pre-existing slot, which is the invariant callers rely on.
- **`CommandResult`'s positional prefix is frozen.** `relay_fallbacks` stays
  the seventh and last positional field
  (`test_command_result_keeps_relay_fallbacks_as_its_trailing_slot`).
  `resolved_path` is a `kw_only` field with a `None` default.
- **No new metrics label.** A resolved path is unbounded, caller-influenced
  text; it never reaches `_extract_labels`. The metrics adapter is unchanged.
- **No new dependency.** The module is pure standard library. `pylint`'s
  `max-module-lines = 400` binds every touched production module.
- **The filesystem check is advisory only.** `executable_path` may report
  whether a path exists and carries an executable bit, but no Cuprum API may
  claim the executed binary's identity is immutable. The documentation must
  state the time-of-check/time-of-use (TOCTOU) limit plainly, and must direct
  operators to filesystem ownership, permissions, and read-only deployment for
  the guarantee Cuprum cannot give.
- **`_policy` stays dependency-light.** `cuprum/context/_policy.py` must not
  import `CuprumContext`, the registration handles, or the `ContextVar`
  plumbing. New pure helpers go in `cuprum/executable_binding.py`, which must
  not import `cuprum.context` at runtime.

## Tolerances (exception triggers)

- If threading the resolved path into the spawn sites needs changes beyond
  `_StageObservation`, `_prepare_execution_observation`,
  `_build_pipeline_observations`, and the four `CommandResult` construction
  sites, stop: the single-resolution-point design is wrong and needs review.
- If any existing test needs editing rather than extending, stop and record
  why. The change is meant to be purely additive; a frozen contract test
  failing means it is not.
- If `cuprum/context/core.py` (385 lines), `cuprum/_pipeline_internals.py`
  (400 lines), or `cuprum/catalogue.py` (400 lines) would exceed 400 lines,
  stop and extract rather than trim.
- If a public signature must change rather than gain a defaulted parameter,
  stop and escalate.
- If tests still fail after three fix attempts on the same failure, stop and
  escalate with the failing log.

## Risks

- Risk: the resolved path leaks into a metric label and blows up cardinality.
  Severity: high. Likelihood: low. Mitigation: `_extract_labels` is not
  touched; a test asserts the label mapping is exactly `{"program", "project"}`.
- Risk: a positional `ExecEvent` construction elsewhere rebinds. Severity:
  high. Likelihood: low. Mitigation: the field is appended last;
  `test_public_api` already pins the `exec_id` slot and is extended to pin
  `resolved_path`'s position.
- Risk: resolution races with a concurrent scope in another thread or task.
  Severity: medium. Likelihood: low. Mitigation: bindings live in the same
  `ContextVar`-backed `CuprumContext` as every other policy, so isolation is
  inherited; explicit per-thread and per-task isolation tests are added.
- Risk: a resolver with side effects is invoked more than once per execution.
  Severity: medium. Likelihood: medium. Mitigation: resolution happens once,
  when the stage observation is built, and the resolved string is carried on
  `_StageObservation`. A test counts resolver invocations across a spawn.
- Risk: documenting a filesystem check overstates the guarantee. Severity:
  medium. Likelihood: medium. Mitigation: the docstrings and the design section
  state TOCTOU explicitly and name the residual operator responsibility.
- Risk: a relative bound path behaves differently once `cwd` is supplied.
  Severity: medium. Likelihood: medium. Mitigation: `resolve_binding` is a pure
  function with an explicit rule, tested against both `cwd=None` and a real
  `cwd`, and pinned by a property test.

## Progress

- [x] (2026-10-01 15:24Z) Reconnaissance complete: spawn sites, context
  plumbing, event/adapter projection, and positional contracts identified.
- [x] (2026-10-01 15:24Z) ExecPlan written.
- [x] (2026-10-01 15:51Z) EP-M1 complete. The module split into
  `cuprum/executable_paths.py` (path vocabulary) and
  `cuprum/executable_binding.py` (binding + resolution) after the single module
  reached 426 lines. 99 tests pass.
- [x] (2026-10-01 15:59Z) EP-M2 complete.
  `cuprum/context/executable_overlay.py` (95 lines) carries
  `merge_executable_bindings`; `cuprum/context/_executable.py` (148 lines)
  carries the bindings field, coercion, `with_executable_binding`, and
  `resolve_executable`; `ExecutableBindingRegistration` and `bind_executable`
  live in `cuprum/context/registration.py`, re-exported through
  `cuprum/context/__init__.py`. `resolve_executable` is pinned as independent
  of the allowlist. 96 focused tests pass.
- [x] (2026-10-01 16:16Z) EP-M2 gate sweep. Five defects sat behind the
  environmental abort: two spelling errors (an Oxford-spelling slip and a
  hyphenation slip, both since reworded out of this document), the R9110
  executable-overlay delegate, four `ty` diagnostics in the
  deliberate-wrong-type tests, and a ruff PT012 trip introduced while fixing the
  `ty` findings. At `0e177d29`, `make check-fmt lint typecheck` exited clean
  (`/tmp/make-code-cuprum-issue-440.out`). That target chain includes the Rust
  gates, so `cargo +nightly-2026-05-28 fmt --check`, rustdoc, clippy, whitaker,
  the typos gate, `yamllint`, and `actionlint` all passed as well;
  `make markdownlint` and `make nixie` passed too.
- [x] (2026-10-01 16:25Z) `make test` run for the first time on this branch, at
  `cfc784db`. Exit 0, no `FAILED` or `ERROR` line in the log
  (`/tmp/test-cuprum-issue-440-bind-catalogue-identity-to-a-validated-executable-path.out`).
  The Python suite, the Rust `nextest` legs, and the separate Cargo doctest
  pass all ran. `make test` drives pytest once per target group, so the log
  holds several per-invocation summaries and no single aggregate count; the
  exit status is the evidence. The working tree was clean at that commit, so
  the log measures exactly what `cfc784db` contains. Every gate this branch can
  run is now green at a recorded revision.
- [x] (2026-10-01 16:54Z) Eleven tests pin the binding join in
  `cuprum/unittests/test_executable_binding_execution.py`. Both claimed
  regressions were confirmed to fail them for the intended reason: `argv0`
  ignoring the binding fails eight of eleven (the decoy's marker, or a
  missing-file error), and resolving ahead of enforcement fails the refusal
  test with a resolver call recorded. The suite is arranged so a name-based
  fallback would *succeed* rather than error — the catalogued program names a
  real executable decoy — so the approved marker can only come from the binding.
- [x] (2026-10-01 17:02Z) EP-M3 complete at `5d1e762f`. Resolution now happens
  at spawn time. `_StageObservation` gained a `resolved_path` field and an
  `argv0` property that is the single home of the fallback rule, so both spawn
  sites read one implementation instead of each rebuilding the argument vector.
  `ExecEvent` gained `resolved_path` appended after `exec_id`; the shared
  `_verbatim_fields` list projects it into both adapters, and because
  `metrics_adapter.py` never calls `_event_common_fields` at all, the field
  cannot reach a metric label by construction — the plan's highest-severity
  risk is closed structurally rather than by inspection.
  `CommandResult.resolved_path` is keyword-only, so no positional slot moved.
- [x] (2026-10-01 17:02Z) `make check-fmt lint typecheck` exits 0 at
  `5d1e762f` (`/tmp/gates-cuprum-issue-440-m3i.out`). The maturin wheel
  snapshot was re-recorded: `cuprum/_context_policy.py` is a new wheel member,
  and the recorded payload grew by exactly that one entry (verified by diffing
  the snapshot's entries against the built wheel's, not by reading the diff
  output, which truncates).
- [x] (2026-10-01 17:06Z) `make test` exits 0 at `5d1e762f`
  (`/tmp/test-cuprum-issue-440-m3.out`), so the whole tree — the new binding
  execution module included — is green under the full suite, not merely under
  the focused run. The main Python group reports
  `2692 passed, 70 skipped in 134.35s` and the log carries no `FAILED` or
  `ERROR` line anywhere; the Rust `nextest` legs report
  `127 tests run: 127 passed, 0 skipped` and the Cargo doctest pass its own
  summary. As with the earlier run, `make test` invokes pytest once per target
  group and so produces several per-invocation summaries rather than one
  aggregate count, which is why a single group's figure is quoted rather than a
  total. The only tracked change at that revision was this document, so the log
  measures the code at `5d1e762f` exactly.
- [x] EP-M4: behavioural scenario, isolation and stateful tests, docs,
  changelog, migration guide, roadmap note.
  - [x] (2026-10-01 17:18Z) The O2 non-vacuity guard is in place. `_FACTORIES`
        gains `bind`, `bind-nested`, and `bind-two`; the two `bind` spellings
        differ (absolute vs `allow_relative=True` relative), so a sequence
        sampling both exercises the layer merge on two path shapes. Recording
        is driven by `_record_binding`'s `isinstance` test on the returned
        handle rather than by the factory entries, so a binding factory added
        later is covered without editing the recorder.
        `test_binding_factories_are_sampled` runs the machine through
        `run_state_machine_as_test` with `database=None`, so it cannot pass by
        replaying a stored example. Negative control: with the three binding
        entries deleted, the guard trips with "no generated sequence installed
        an executable binding".
  - [x] (2026-10-01 17:19Z) O4's adapter surface is locked. The generator in
        `test_adapter_projection.py` now emits `resolved_path`, and two named
        tests pin the literal key each adapter publishes plus the unbound
        omission. Mutation control: deleting the `resolved_path` entry from
        `_verbatim_fields` fails the handwritten expectation. See the
        Surprises entry for the hole this exposed in the sibling identity
        check.
  - [x] (2026-10-01 17:20Z) The behavioural scenarios are in place and
        non-vacuous. `tests/features/catalogue.feature` gains the identity and
        the unapproved-name scenarios; their steps live in
        `tests/behaviour/_catalogue_binding_support.py`, which
        `tests/behaviour/test_catalogue_behaviour.py` re-binds because
        pytest-bdd resolves a step against the fixtures visible to the module
        declaring the scenario. Two negative controls were run: dropping the
        `bind_executable` scope fails with "the child must have been started as
        the bound file, reported '-c im…'", and widening the allowlist fails
        by executing `/opt/tools/sccache` (`FileNotFoundError`), proving the
        refusal is the allowlist's rather than the binding's.
  - [x] (2026-10-01 17:25Z) Documentation is written and its examples execute:
        `docs/cuprum-design.md` section 5.1.2 plus its TOCTOU limits,
        `docs/users-guide.md`, `docs/v0-2-0-migration-guide.md`,
        `CHANGELOG.md`, and the `docs/roadmap.md` note under item 3.3.1. The
        suite's documentation-example runner picked the new
        `tested-example: migration-executable-bindings` fence up on its own
        (behaviour count 44 -> 45).
  - [x] (2026-10-01 17:54Z) `make fmt`, `make check-fmt`, `make typecheck`,
        `make lint`, `make test`, `make markdownlint`, and `make nixie` all
        exit 0 on the uncommitted EP-M4 tree at `4cec4a30`. Two real defects
        were found and fixed on the way: the module-length ceiling (see
        Surprises) and an ambrleaks `[snapshot-posix-path]` finding on 21
        fictional `/opt/tools/echo` snapshot values, allowlisted narrowly in
        `ambrleaks.toml`.
- [x] EP-M5: gates green, push, draft pull request, CodeRabbit review.
  - [x] (2026-10-01 17:56Z) The EP-M4 work landed as seven atomic commits
        (`22fe3311`, `6dc2c66a`, `d6728ec7`, `73943bc9`, `79193636`, `22a609b4`,
        `896677e3`), taking the branch from `4cec4a30` to `896677e3`. Seven
        gates were green on the immediately preceding tree (entry above), and
        the committed content is byte-identical to it — the commits were made
        from that tree with no edits in between, so no post-gate edit can have
        invalidated them.
  - [x] (2026-10-01 17:56Z) Branch pushed and draft PR opened as
        [cuprum#571][pr-571] with `(#440)` in the title and `Closes #440` in the
        body. The reference session URL is in the body's `## References`
        section. The CodeRabbit GitHub App reports `Review skipped: draft pull
        request`, which is expected and is why the CLI review below is the
        operative one.
  - [x] (2026-10-01 17:58Z) The Progress timestamps were re-derived. Thirteen
        entries recorded times that had not yet happened — several hours ahead
        of the wall clock — so they cannot have been observed when written.
        Every entry now carries a time taken from an authoritative artefact:
        the commit that produced the work, or the modification time of the gate
        log the entry cites. Two are anchored to the gate log rather than the
        commit because the log was written before the commit it measures. The
        eleven-test entry now precedes the milestone summary it belongs to,
        because its artefact genuinely predates it. See the Surprises entry.
  - [x] (2026-10-01 18:15Z) `coderabbit review --agent` returned five findings
        against `896677e3`, none of them blocking. All five concern the
        demonstration surface rather than the mechanism: two asserts that
        could not fail, an inaccurate sentence in the design guide about when
        the filesystem is consulted, a base-class docstring that omitted
        executable bindings, and an unrelated assertion standing in for a
        hold-preservation check. The transcript is in
        `/tmp/coderabbit-cd050cee-epm5.out`.
  - [x] (2026-10-01 18:18Z) All five were actioned; the design-guide pair
        overlap at the same lines and the second phrasing is discharged by
        the same rewrite, so the four distinct edits cover five findings. Both
        rewritten assertions were then checked against a seeded fault, because
        a replacement assert that still cannot fail would have "fixed" nothing.
        Leaking the bound path as the *value* of the existing `program` label
        — the shape a leak would really take, and the shape the original
        assert was blind to — now fails. Installing the binding over an empty
        allowlist now fails on the survival assertion rather than passing.
        Two earlier mutations were discarded as inconclusive: each failed with
        a `NameError` or `TypeError` raised by the mutation itself, which
        proves nothing about the assertion.
  - [x] (2026-10-01 18:32Z) Six gates green on the fix set at `ff58bba2`, the
        tree frozen across the whole run: `check-fmt`, `typecheck`, `lint`,
        `test`, `markdownlint`, `nixie`. Every log records the same HEAD at
        start and end, and all four file digests are byte-identical to their
        run-start values, so no gate wrote to a tracked file. Test evidence:
        2700 passed/70 skipped (unit), 127/127 (Rust nextest), plus the
        behaviour and doctest suites. Landed as `5e2e3dd5` and `903fb33e`,
        split so the test-strength change and the prose correction stay
        separately reviewable.
  - [x] (2026-10-01 18:34Z) The review cycle was recorded in the plan and
        landed as `a5c84fa1`, taking head past the `903fb33e` the entries below
        used to name.
  - [x] (2026-10-01 18:39Z) A second `coderabbit review --agent` pass emitted
        three findings and then died with
        `TRPCWebSocketClosedError: WebSocket closed`. The transcript is
        `/tmp/coderabbit-cd050cee-epm5-rereview.out`. It is a **truncated**
        review, not a completed one with three findings: the process exited 1
        after a `{"type":"error"}` record, and no
        `{"type":"complete","status":"review_completed"}` record follows. The
        three it did return were all real.
  - [x] (2026-10-01 18:45Z) A third pass completed cleanly (exit 0, one
        finding) against the same tree, recording
        `"status":"review_completed"` over 51 files. Transcript:
        `/tmp/coderabbit-cd050cee-epm5-rereview2.out`. It returned only the
        plan-path finding. That it did not repeat the earlier three is
        nondeterminism, not evidence they were fixed — the files it cited had
        not been edited between the two runs — so all four were dispositioned
        on their own merits below rather than on which pass happened to report
        them.
  - [x] (2026-10-01 18:52Z) All four findings actioned, each verified against
        the tree with a reproducer before and after:
        - **A, the fail-fast fixture** (`test_adapter_projection.py`). Real:
          production `emit_fail_fast` sets `resolved_path=None` deliberately,
          but `_representative_event` populated it for every phase, so the
          snapshot pinned a wire shape Cuprum never emits. Fixed by making the
          fixture match production; the re-recorded snapshot is a two-line
          deletion, both from the `[pipeline_fail_fast]` variant, which is the
          Red/Green evidence that the fixture and not the snapshot was wrong.
        - **B, a resolver returning a non-`str`** (`executable_binding.py`).
          Real, and worse than reported: `None` is the sentinel the execution
          layer reads as *unbound*, so via `_StageObservation.argv0`'s fallback
          a resolver returning it would run the **catalogued name** — a
          different executable, and the exact substitution this feature exists
          to make deliberate. Fixed with `_checked_resolver_result`, which
          raises `TypeError` for any non-`str`.
        - **C, a raising resolver** (`context/_executable.py`). Real: a
          resolver raising `FileNotFoundError` escaped as itself. The finding
          cited `_command_internals.py:119-123`, but that site is a thin
          wrapper over `CuprumContext.resolve_executable`, so the fix went to
          the definition site; all three call sites (`_command_internals`,
          `_observability`, `_pipeline_internals`) route through it. Fixed with
          `ExecutableResolutionError`, which names the logical program and
          chains the original exception. It lives in
          `executable_binding.py`, not the context module, because the context
          module is the only place that knows both the program and the
          failure, and `ExecutableBinding` carries no program field.
        - **D, a developer-specific path in this plan.** Real, and also wrong:
          the recorded worktree path spelt `github---leynos--cuprum` with two
          hyphens where the real directory has three. Replaced with a
          description that cannot go stale.
  - [x] (2026-10-01 18:53Z) Nine tests added in
        `cuprum/unittests/test_executable_binding_failures.py` covering both
        new behaviours, since the AGENTS.md rule requires a regression test
        alongside a bug fix and neither behaviour had one. Non-vacuity was
        established by three seeded faults rather than by asserting the tests
        pass: reverting the resolver-result check failed five tests, removing
        the context wrap failed four, and removing only the re-raise clause
        failed the double-wrap test. Each failed for its intended reason, and
        production was restored byte-identically afterwards.
  - [x] (2026-10-01 21:30Z) The four `make lint` findings from the
        environment-fixed retry cleared, all four being defects in this
        branch's own code rather than environment artefacts. Fixed by: adding a
        `Raises` section to `resolve_executable` for `DOC501`; deleting the
        `# ruff: ignore[blind-except]` comment for `RUF100`; and splitting two
        over-long doctest lines. Ruff and `ruff format --check` are clean on all
        four touched files, and the longest line in the two production modules
        is 88 columns. That cleared Ruff, which then let the `&&` chain reach
        interrogate for the first time; see the Surprises entry. Four nested
        `resolver` docstrings were added there, taking the estate from 99.9% to
        `PASSED (minimum: 100.0%, actual: 100.0%)`. 182 tests pass across the
        eight binding-related suites.
  - [x] (2026-10-02 12:45Z) Gate set re-run at `a5c84fa1` before the commit:
        `check-fmt`, `typecheck`, `lint`, `test`, `markdownlint`, and `nixie`,
        all exit 0, each log recording its own HEAD. `make lint` regenerated
        nothing, so `typos.toml` needed no separate commit.
  - [x] (2026-10-02 13:05Z) The change set landed as `fa1de37b` (production),
        `ee0401c8` (tests), `fea39f03` (the fail-fast fixture), and `59bf841f`
        (docs and this plan), and pushed. The committed delta was checked
        against the gated delta before pushing — the same twelve files and the
        same `+517/-21`, with the plan's digest still the one the gate logs
        recorded.
  - [x] (2026-10-02 13:40Z) **CI at `a5c84fa1` failed, and the failure was not
        visible locally.** Two legs, Python 3.12 and 3.14, failed on
        `test_the_bound_path_reaches_every_event_of_the_execution`:
        the `stdout` event reported `resolved_path: None` while `plan`,
        `start`, and `exit` reported the bound path. Root cause, established by
        reproducing it rather than by reading: this is a **semantic merge
        conflict**. The branch adds `resolved_path` and routes every event
        through `_StageObservation.emit`; `main` gained `71aaf3eb`, which
        introduces `_LineEventEmitter` to build line events directly from a
        fixed field list, and `main` has no `resolved_path` at all, so its list
        could not mention the field. Neither side edited the other's lines, so
        git merged cleanly while silently dropping the field on `stdout` and
        `stderr` events. Reproduced by merging `origin/main` into the branch
        and re-running the file: the same one-line diff, `stdout: None`.
  - [x] (2026-10-02 13:45Z) Fixed in `cuprum/_line_callbacks.py` by carrying
        `resolved_path` on `_LineEventEmitter` exactly as `env_mode` is
        carried, and for the same documented reason: it is invariant per stage
        observation and known before the spawn, so a line event that omitted it
        would disagree with the events around it about which executable ran.
        The other fields `_StageObservation.emit` sets and `emit_line` does not
        were audited field by field and are all `_EventDetails` payload —
        per-event detail that a line event correctly leaves at its defaults.
        The file's own suite then passed 11/11.
  - [x] (2026-10-02 14:00Z) A `coderabbit review --agent` pass completed
        cleanly (exit 0, `"status":"review_completed"`, four findings over 51
        files) against `59bf841f`, transcript
        `/tmp/coderabbit-cd050cee-epm5-rereview3.out`. All four actioned:
        - **E, second-person pronouns in the users' guide.** Real. The style
          guide forbids first and second person outside `README.md`, and the
          new paragraph was the only second-person text in the whole file
          (`grep -c '\byou\b|\byour\b'` found two hits, both mine). Rewritten
          impersonally; the file is now free of them.
        - **F, the refusal scenario's name.** Real. "An unapproved executable
          cannot borrow an approved name" implies a *path* is borrowing an
          identity, but the scenario asserts the reverse-shaped claim: a
          binding cannot authorize an unlisted *logical program*. Renamed to
          "A binding cannot authorize an unlisted logical program", and the
          `@scenario` declaration re-bound to match.
        - **G and H, the first binding step's ignored `path`.** Two findings
          that contradict each other: one asks the step to bind the configured
          path instead of the interpreter, the other asks the wording to admit
          it binds the interpreter and to leave the behaviour alone. Taken as
          a pair, they expose the real defect, which is that the feature file
          and the step disagreed: the step documented in its own comment that
          `path` cannot be a real location because the suite must run on any
          machine, but the feature text still named a literal path. Resolved by
          making the two agree — the scenario now reads "to the running
          interpreter" and the discarded parameter is gone, so there is no
          longer a value that is silently ignored.
        - Non-vacuity for F and G/H: mutating the step to bind a different
          program fails the scenario with the intended message (the child fell
          back to the catalogued name), and the mutation was reverted exactly.
  - [x] (2026-10-02 15:50Z) The merge into `origin/main` exposed a **second**
        consequence of the same hoist, this time in an `insta`-style whole
        payload snapshot rather than a field list. `main`'s
        `tests/behaviour/_structured_events_support.py::normalize_event`
        renders **every** declared `ExecEvent` field by name, so the snapshot
        failed on `resolved_path` even though the field was present and
        correct: the recorded payload predates it. The probe installs no
        binding, so `None` is the honest value; the snapshot was regenerated
        and now records `'resolved_path': None` on the plan, start, and exit
        events. Evidence: `1 snapshot failed. 1 snapshot passed` before,
        `1 snapshot updated`, `9 passed` after.
  - [x] (2026-10-02 16:05Z) Full gate set re-run at `3e893565`, sequentially,
        each log recording its own HEAD and the plan digest: `check-fmt`,
        `typecheck`, `test` (Python and Rust), `markdownlint`, and `nixie`,
        all exit 0. The committed delta's digest
        (`bd6d7be266471bfa919fec0e47f4554cd622a090f0e55db50898814de6ffc0b3`)
        equals the digest recorded before the commit, and the plan's digest
        matches the gated one, so the gates describe the commit that exists.
        `make lint` is the one exception and it is environmental: it ran every
        sub-gate green — pylint 10.00/10 three times (classic, DF12, and the
        plugin-pass), `ruff check`, interrogate `PASSED (minimum: 100.0%,
        actual: 100.0%)`, `ambrleaks`, skylos, rustdoc, clippy, whitaker,
        yamllint, and the typos gate — then **wedged in `actionlint`, the last
        command**. This is the documented host-only deadlock, not a finding:
        `timeout 120 actionlint …` on this branch exits 124, and the identical
        command against `origin/main`'s own `ci.yml` also exits 124. The
        bounded substitute passes: `timeout 300 actionlint -shellcheck=
        -config-file .github/actionlint.yaml` exits 0 with no diagnostics.
        The branch touches no file under `.github/` at all
        (`git diff --name-only origin/main...HEAD -- .github/` is empty), so
        there is nothing in this change for the workflow linter to catch.
  - [x] (2026-10-02 16:20Z) CI at `3e893565` is running: `changes`,
        `Typecheck and test (Python 3.12)`, `(3.13)`, `(3.14)`, `(3.15a)`,
        both extension-gated suites, `lint-test`, `benchmark-ratchet`, and all
        five wheel builds including `verify-wheel-install` have concluded
        `success`; `coverage` is still running. The two legs that failed at
        `a5c84fa1` — Python 3.12 and 3.14 — both pass, and both **ran the
        suite** rather than skipping it: their step lists show
        `17. success Run typechecker`, `18. success Install dev-fast doctest
        toolchain`, and `19. success Run tests` as executed steps, not
        `skipped`. That is the specific regression this commit fixes, and it
        is established at step granularity rather than inferred from the job's
        overall conclusion.
  - [x] (2026-10-02 16:25Z) Confirmed that the 3.15a leg's green is **not**
        evidence, exactly as the earlier note warned: its job steps after
        "Set up runner" are all `skipped` — checkout, typechecker, and tests
        alike — while the job's own conclusion is `success`. It is the
        skipped-suite leg at this head too, so cite the 3.12 and 3.14 legs for
        suite coverage and treat 3.15a as unobserved.
  - [x] (2026-10-02 18:05Z) All eleven findings from the CodeRabbit review of
        `6efb42b9` are adjudicated: eight were applied in the working tree
        before this entry, and the last three are now applied too — the stale
        `#executable-bindings` anchor at both of its sites, the refusal step's
        fail-on-success branch, and the resolver recording the path it was
        asked for. Each was verified against the code rather than taken on
        faith, and the new branch was probed to confirm it fires.
  - [x] (2026-10-02 18:20Z) Every gate re-run at the tree this commit freezes,
        sequentially, each logged under `/tmp` with the branch name in the
        filename: `check-fmt` exit 0 (716 files formatted, 83 Markdown files
        unchanged), `markdownlint` exit 0 (0 issues, spelling gate included),
        `typecheck` exit 0, `python-lint` exit 0 (ruff, interrogate at 100.0%,
        three pylint passes at 10.00/10, ambrleaks, skylos dead-code gate),
        `rust-lint` exit 0, `nixie` exit 0, and `test` exit 0. The GitHub
        Actions lint is covered by `yamllint --strict` (exit 0) and the
        bounded `actionlint -shellcheck= -config-file .github/actionlint.yaml`
        (exit 0); the unbounded form deadlocks on this host, and the branch
        touches nothing under `.github/`. The substitute was shown to do real
        work rather than merely return zero: the same invocation against a
        probe repository whose workflow puts `run` and `uses` in one step
        exits 1 with `unexpected key "run" for step to execute action`.
  - [x] (2026-10-02 18:40Z) `coderabbit review --agent` at `c738c78a` reports
        `"status":"review_completed"` with **zero findings** across the 51
        files it reviewed, exit 0. The eleven findings raised against
        `6efb42b9` are therefore all discharged and nothing new was raised
        against the fixes. Evidence: the review's own final line, logged at
        `/tmp/coderabbit-cd050cee-head-c738c78a.out`.
  - [x] (2026-10-02 19:05Z) CI at `c738c78a` is green: run `37026701030`
        concludes `success` with every job successful except the Loom smoke
        test, which is skipped as designed. The suite-coverage claim is again
        established at step granularity rather than from job conclusions: the
        Python 3.12 leg (job `110903235097`) and the 3.14 leg (job
        `110903234681`) both report `success` for `Run typechecker` **and**
        `Run tests` as executed steps, while the 3.15a leg
        (`110903234656`) reports `skipped` for checkout, typechecker, and tests
        alike. The trap this plan records has therefore recurred unchanged at a
        second head, and the two legs with genuine coverage are the ones cited.
  - [x] (2026-10-02 19:35Z) CI at the final head `72f0e660` is green: run
        `37028966943` concludes `success`, with every job successful and only
        the Loom smoke test skipped. The same step-granularity check was
        applied a third time and gave the same answer: the 3.12 and 3.14 legs
        report `success` for checkout, typechecker, and tests, while 3.15a
        reports `skipped` for all three. The `Rust boundary verification`
        workflow (`37028968211`) is also `success`. The plan is therefore
        closed: every gate has passed at the pushed head, the review is clean,
        and nothing remains but the human decision to merge.
- [x] (2026-10-02 18:45Z) Rebase onto `origin/main` requested, investigated,
      and found to be a no-op. `origin/main` is `b6bb9a99`, which is already an
      ancestor of the branch head `0ee8ba47`, so the branch is based on the
      current target tip with nothing to replay. Recorded before any mutation:
      `OLD_HEAD=0ee8ba47`, `OLD_BASE=TARGET=origin/main=b6bb9a99`,
      merge-base `= b6bb9a99` (= TARGET, which is what "already rebased" looks
      like), remote branch head `= 0ee8ba47` (= OLD_HEAD, so the local and
      remote views agree). Recovery refs were created before any inspection
      that could mutate, and are inert: four refs under the
      `refs/rebase-recovery/` namespace, named with the `issue440-` prefix and
      the `-20261002` suffix, covering `OLD_HEAD`, `OLD_BASE`, `TARGET`, and
      `REMOTE_HEAD`. The
      `--reapply-cherry-picks` replay hazard was tested rather than assumed: a
      `git patch-id --stable` comparison across the 35 non-merge branch commits
      and the last 400 commits of main reports **zero** overlapping patch-ids,
      so no commit in the range duplicates content main already holds. No
      rebase, and therefore no force push, was performed. See the Decision log
      for why replaying anyway would have been actively harmful.
- [x] (2026-10-02 18:50Z) Semantic audit of the existing base against the new
      target, replacing a check that turned out to be vacuous. The published
      Weave procedure's "target-only paths are byte-identical" test has *no
      subjects* here: because `origin/main` is an ancestor, the set of paths
      main changed since the fork but the branch never touched is empty (`comm
      -23` over 128 main-changed and 171 branch-changed paths yields 0), so the
      check would have passed without examining anything. The check with
      content is whether the branch *deleted* content main added, so it was run
      instead: across all 128 shared paths, not one line that main added since
      the fork is absent at `HEAD`. A conflict-marker scan over all 51 changed
      paths found none, and `git diff --check origin/main HEAD` is clean. Weave
      itself was confirmed unselected for every one of the 51 paths (`git
      check-attr merge` returns `unspecified`; the repository has no
      `.gitattributes`, no `info/attributes`, and no `core.attributesFile`),
      with a command-scoped negative control proving `check-attr` is live and
      the null result is real rather than a silent failure.
- [x] (2026-10-02 18:55Z) `weave check` recorded as **unchecked**, not as a
      pass. The installed toolchain is `weave 0.5.1` with a matching
      `weave-driver 0.5.1` at `/home/leynos/.cargo/bin/weave-driver`, and
      `weave check` is supported. On this tree it exits `0` while printing
      "no merge in progress (no MERGE_HEAD) and HEAD is not a merge commit, so
      there is no three-way context … **NOTHING WAS CHECKED** — this is not a
      clean bill of health." `MERGE_HEAD`, `REBASE_HEAD`, and `CHERRY_PICK_HEAD`
      are all absent, and `HEAD` is a single-parent commit, so no 0.5.1 mode can
      verify this tree: the no-argument mode has no three-way scope, and the
      `--base/--ours/--theirs` mode would describe a three-input comparison
      rather than the tree being accepted. The exit code alone would have
      recorded a pass; the sentence is what makes it an explicit unchecked
      state. `weave check` was also confirmed read-only: `git status
      --porcelain` before and after shows the same single modified path, so it
      mutated nothing.
- [x] (2026-10-02 19:05Z) `sem` (0.5.3) applied as an independent entity-level
      audit, and its arity findings adjudicated rather than adopted or waved
      away. `sem diff --from origin/main --to HEAD` corroborates the design:
      `_resolve_executable_for` added in
      `_observability.py`, `_enforce_allowlist`
      and `_collect_hooks` moved from `_pipeline_internals.py` to
      `_context_policy.py`, and `_LineEventEmitter` modified — the last being
      exactly the merge-conflict site the branch had to reconcile. `sem verify`
      reports 494 call-arity findings across 121 files repo-wide, 15 of them in
      files this branch touches. All 15 were read against the real signatures
      and none is a genuine arity error; each falls into one of four heuristic
      limits: **variadic parameters** (`_merge_tags(*tags)` at
      `_observability.py:33` called with 3 and 4 tags,
      `_catalogue_for(*programs)` called with 2, and
      `env(*overlays, **kwvars)` called with 1 — sem counts `*args` as a single
      fixed parameter); **framework callbacks** (`_events`
      is a Hypothesis `@st.composite` strategy, so the zero-argument call is
      Hypothesis invoking it, not a caller passing nothing); **shadowed local
      functions** (`task_worker` is defined twice with different arities, at
      `test_context_isolation.py:58` taking one parameter and `:87` taking two,
      and sem resolves both call sites to the first definition); and
      **higher-order returns** (`builder = sh.make(...)` followed by
      `builder()`, where sem reads `sh.make`'s own parameters as the returned
      callable's). A fifth shape is a fuzzy name match: the finding citing
      `executable_binding` at `test_executable_context.py:357` points at a
      `CuprumContext().with_executable_binding(...)` call, and sem has matched
      the substring. The upstream project already tolerates this class of
      finding — the 479 findings outside this branch are untouched in `main` —
      so nothing here is repaired, and the audit is recorded as corroborating
      evidence with a known false-positive rate rather than as a defect list.
- [x] (2026-10-02 21:07Z) The head moved after the "final head" claim above:
      recording the rebase investigation required a commit, so the pushed head
      is now `2f89a370` and the `72f0e660` row is superseded rather than
      wrong. The delta is one tracked Markdown file,
      `docs/execplans/issue-440-bind-catalogue-identity-to-a-validated-executable-path.md`,
      **+119/-0**, so every Python and Rust gate input is byte-identical to
      `0ee8ba47` and only the Markdown-reading gates could differ. All six were
      therefore re-run against `2f89a370` and captured with a per-gate
      `head_before`/`head_after`, both equal to `2f89a370` with the tree clean
      before and after: `make check-fmt`, `make typecheck`, `make lint`,
      `make test`, `make markdownlint`, and `make nixie` each exit `0`. Lint
      again reached every one of its twelve sub-checks, `yamllint` and
      `actionlint` included, so none was skipped. `make test` ran its eleven
      `PYTEST_TARGETS` patterns as eleven separate pytest invocations — which
      is the figure re-derived from the `Makefile`, not a remembered one — and
      no summary line reports a failure; nextest reports
      `127 tests run: 127 passed, 0 skipped` and cargo's doctest pass reports
      `0 passed; 0 failed; 3 ignored`. CI was then re-checked at the new head
      and **all twelve** required contexts from the `main-required-checks`
      ruleset are `success` (`missing=0 notgreen=0`), so the earlier
      "final head" claim now holds at `2f89a370`. CodeScene is the only
      non-green check and is not among the twelve. The force-push obligation
      was discharged without a push: `git push --force-with-lease --dry-run`
      reports `Everything up-to-date`, and `git ls-remote origin` confirms both
      `refs/heads/main` (`b6bb9a99`) and the branch tip (`2f89a370`) match the
      local refs, so the earlier no-op finding rests on the live remote rather
      than on a possibly stale tracking ref.
- [x] (2026-10-02 21:58Z) A `coderabbit review --agent` pass through the CLI
      returned two advisory findings, both applied. The CLI route is what makes
      this review possible at all: the CodeRabbit *app* check skips a draft pull
      request, so the app's green status is not evidence of a review, and this
      branch has been a draft throughout. The pass reviewed 51 files and
      returned `{"type":"complete","status":"review_completed"}` with no
      high- or medium-severity concern. The findings were
      `tests/behaviour/_catalogue_binding_support.py:175`, where the
      `pytest.fail` message omitted the resolver's call count that the
      neighbouring assertion already computes, and `docs/cuprum-design.md:333`,
      where "Neither step, alone or together" negates awkwardly. Both were
      read against the tree before being accepted rather than taken on trust,
      and both sites matched the report exactly. A prior head's evidence also
      needed correcting here: an earlier handwritten note claimed the amended
      commit left the tree identical to `2f89a370`, which is wrong — `2f89a370`
      is tree `177294966f` and the newer commit adds this file, `+27/-0`. The
      amend was message-only relative to its own pre-amend commit `0197d45c`,
      which is a different claim. The consequence is that the Markdown-reading
      gates are *not* inherited from the `2f89a370` run and were re-run, while
      the Python and Rust gate inputs remain byte-identical.
- [x] (2026-10-02 18:40Z) The mislabelled-`Z` defect recorded above **is still
      present, and the claim that it was repaired does not hold.** A fresh
      falsifiable check — a stamp cannot post-date the true UTC time of the
      commit that first introduced it, since that commit already contains it —
      finds **31 stamps** in this section violating it, by 12 minutes at the
      least and 526 at the most. Method: `git log --reverse --format=%H
      origin/main..HEAD -- <this file>` to get each stamp's earliest containing
      commit, then that commit's own `%aI` converted to UTC. The two most
      recent entries are the clearest: the one stamped `2026-10-02 21:58Z` was
      written by `901794f0` at `18:09Z`, and the one stamped `21:07Z` by
      `fc92987d` at `17:09Z` — both exactly **two hours** ahead, which is the
      CEST offset applied in the wrong direction. The earlier `2026-10-01`
      stamps do not share the fault: the one reading `17:02Z` sits 63 minutes
      before `ff58bba2`, the commit that first contains it, and equals the true
      UTC time of `5d1e762f`, whose completion it reports. So the defect began
      after that day, recurred after being documented, and survived the
      re-derivation the earlier observation claims to have performed. The
      stamps are therefore left **as written**: restamping them would destroy
      the evidence of how they were produced, and a reader wanting a defensible
      time already has a stronger source than any label — the commit that
      introduced the entry. This is the hand-summed-total failure seen from the
      other side: not a figure no command emits, but a figure no clock emitted.
- [x] (2026-10-02 19:13Z) The status correction above was committed as
      `91c96d51` and pushed, and CI completed green on it: **all twelve**
      required contexts from `main-required-checks` are `success`
      (`required=12 missing=0 notgreen=0`, `coverage` included), with run
      `37050567552` reporting `completed/success`. Only the three
      Markdown-reading gates ran before the push — `make check-fmt`,
      `make markdownlint`, `make nixie`, each exit 0 at a frozen `91c96d51`
      with `head_before == head_after` — because the change is one Markdown
      file, `+32/-2`, and a diff of that range excluding Markdown is
      **empty**, so no Python, Rust, YAML or Mermaid input differs from the
      fully-gated `901794f0`. A sweep at the settle point confirms it: one
      tracked file differs from that head and it is this plan, `+44/-12`
      cumulative, so the combined six-gate run at `901794f0` remains the
      operative evidence for everything non-Markdown. One methodological
      error is worth recording: the first poller treated an empty `gh`
      response as `pending=0` and announced `SETTLED` with `missing=12` — a
      line that reads like a pass while meaning no data was retrieved. It
      was discarded, and the re-poll asserted a non-empty response and the
      presence of all twelve contexts before concluding anything.
- [x] (2026-10-02 19:40Z) `e12fefd3` is the last head whose CI is certified
      here, and this is deliberately the final entry: **it is recorded by the
      commit that follows it**, so whatever commit carries this sentence is one
      ahead of the head it certifies. The lag is inherent rather than an
      oversight — no commit can assert that its own CI passed — and without a
      stopping rule each record-and-commit cycle would demand another round.
      At `e12fefd3` all twelve required contexts are `success`
      (`required=12 missing=0 notgreen=0`, `coverage` included); its diff from
      the fully-gated `901794f0` is Markdown only; and its three
      Markdown-reading gates were re-run green before the push. A reader who
      needs the CI status of the current tip should read it from the tip's own
      checks rather than from this entry, and should treat the introducing
      commit — not the stamp beside it — as the authority for when each entry
      was written.
- [x] (2026-10-02 19:47Z) **Rebased onto a moved target.** `origin/main`
  advanced from `b6bb9a99` to `c65d843c` ("Reject bytes in sh.make arguments
  (#512)") after the no-op check recorded in `2f89a370`, so the ancestry test
  that then held no longer did. Rebased the 41 non-merge commits in
  `b6bb9a99..74b9f004` onto `c65d843c` with the linear procedure; **zero
  conflicts**. Every audit the rebase procedure requires passed:

  - **Oracle tree.** `git merge-tree --write-tree --messages` of
    `74b9f004` into `c65d843c` exits 0 and yields tree `a55aa3b7`; the
    rebased head `babb08a4` has tree `a55aa3b7` — **identical**. This is
    the check that matters here, because the replay range contained a
    merge commit (`82acb1ae`) and the known failure mode for that shape is
    a clean, non-conflicting replay that silently drops content.
  - **The merge was safe to linearize.** `git merge-tree` of `82acb1ae`'s
    own parents reproduces its tree `18e7dd1c` exactly, so that merge was
    purely mechanical and hand-resolved nothing; its second parent
    `b6bb9a99` is an ancestor of `c65d843c`. It therefore carried no
    content of its own for a linear replay to lose, and the oracle proof
    above is not weakened by flattening it.
  - **`range-diff`.** `b6bb9a99..74b9f004` against `c65d843c..babb08a4`
    reports `=` on all 41 commits, so each replayed commit is
    patch-identical to its original.
  - **Semantic audit.** Of the target's 7 changed files, 4 are target-only
    and every one is byte-identical at the new head; the 3 the branch also
    touches (`docs/cuprum-design.md`, `docs/users-guide.md`,
    `docs/v0-2-0-migration-guide.md`) show **pure insertions** against the
    target (`88/0`, `56/0`, `58/0`), so no deletion is unexplained.
    `git diff --check` is clean.
  - **Driver.** Weave is not selected for any path in this repository: the
    global attributes file is empty at 0 bytes, there is no tracked
    `.gitattributes`, and `git check-attr merge` reports `unspecified` for
    the conflicted-candidate files. The driver could not participate, so
    the Weave semantic audit is not applicable here rather than skipped.
  - **Lock files.** Neither the target commit nor the branch range touches
    `uv.lock` or `rust/Cargo.lock`, and both are identical to the target's;
    the lock-file policy has nothing to act on.

  Recovery refs are preserved under `refs/recovery/issue-440/` for the old
  head, old base, and target. **Every SHA cited in entries written before this
  one now names a pre-rebase commit that the rewrite replaced.** Those objects
  still exist under the recovery refs, so `git show <old-sha>` still works and
  a citation is not a lost commit; what changes is that the SHA no longer
  appears on the branch. To map any old SHA to its replayed equivalent, run:

  ```bash
  git range-diff \
    refs/recovery/issue-440/old-base-b6bb9a99..refs/recovery/issue-440/old-head-74b9f004 \
    refs/recovery/issue-440/target-c65d843c..HEAD
  ```

  The left column is the pre-rebase series and the right column is the current
  one, so an entry citing `91c96d51` is found on the left and read across.
  Because it is anchored on the immutable recovery refs rather than on
  `74b9f004`, this command keeps working as later commits are added. Gate
  evidence for `74b9f004` and earlier is **stale for this candidate**, and the
  four repository gates (`check-fmt`, `test`, `typecheck`, `lint`) are re-run
  against the head that carries this entry, with each gate's log recording the
  head it ran at.
- [x] (2026-10-02 19:55Z) A process error is recorded here because it changes
  which evidence is admissible. The gate sweep was launched against the frozen
  head `a6d1e414`, and while it ran this entry set was edited into the plan — a
  tracked file. The sweep reports `head_before == head_after` for every gate,
  but that check compares commit SHAs and **cannot see a dirty working tree**,
  so it cannot certify that a gate read the committed tree. The consequence is
  scoped rather than total: `check-fmt` (via `mdtablefix --check`) and `lint`
  (via the `spelling` sub-gate) read Markdown and are therefore invalidated by
  the edit, whereas `test` and `typecheck` were verified not to read this file
  at all — no test in the repository references the issue-440 plan path — so
  their results stand for any tree differing from `a6d1e414` only in this
  document. The corrective action is to re-run the Markdown-reading gates at
  the head that carries the edit and to prove by an empty non-Markdown diff
  that the `test`/`typecheck` evidence still applies. The lesson for the next
  operator is to treat "freeze the tree" as binding on the working tree and not
  merely on the branch pointer: commit every documentation edit **before**
  launching the sweep, or the sweep's own head assertions will imply a rigour
  it does not have.
- [x] (2026-10-02 20:02Z) The `a6d1e414` sweep: `check-fmt`, `test`, and
  `typecheck` passed; `lint` **failed**, and the failure is environmental
  rather than a defect in the branch. Everything lint ran up to that point
  passed — `ruff check`, both pylint passes at `10.00/10`, `interrogate` at
  `100.0%`, the `df12-python-lints` plugin pass, `ambrleaks`, `skylos`, rustdoc
  under `-D warnings`, Clippy with `--all-targets --all-features`, and Whitaker
  — and the run then died in the `spelling` sub-gate with:

  ```plaintext
  Updating https://github.com/leynos/typos-config-builder.git (v0.1.3)
    × Failed to resolve `--with` requirement
    ╰─▶ Git operation failed
  ```

  That is a `uv` fetch of the pinned config builder failing inside the gate's
  own environment, not a spelling finding. The same fetch, re-attempted
  immediately afterwards, succeeds and prints `0.1.3`, so the cause is
  transient network or credential-helper flakiness on this host and not the
  pinned revision. **Two consequences are recorded rather than glossed.**
  First, the failure is *not* evidence about the Markdown, which is fortunate
  because the tree was also dirty (previous entry) and the spelling gate reads
  prose. Second, `make lint`'s sub-targets run in sequence, so the failure
  aborted the remaining ones: `github-actions-lint` **never ran** — `yamllint`
  and `actionlint` appear zero times in the log — even though the change
  touches no workflow file. An aborted gate thus leaves later checks
  unobserved, and this run cannot be cited as evidence for them. The corrective
  action is a fresh `lint` on a frozen tree.
- [x] (2026-10-02 20:05Z) Re-run of the two gates invalidated by the mid-sweep
  edit, at the frozen head `c6f67235`: both pass. `check-fmt` exits 0 in 1 s;
  `lint` exits 0 in 117 s and, crucially, reaches **all fourteen** sub-linters.
  The `spelling` fetch that died at `a6d1e414` succeeded this time, and the
  `github-actions-lint` sub-gate it had masked — `yamllint` and `actionlint` —
  ran and passed, so the previously-unobserved gap is now closed rather than
  merely re-asserted. The `a6d1e414` `lint` failure is thereby confirmed
  transient: the same pinned fetch, same repository, same tree content for
  every non-Markdown path. Separately, the `test`/`typecheck` evidence from
  `a6d1e414` is retained rather than re-run, on proof rather than on argument:
  `git diff --name-only a6d1e414 c6f67235 -- . ':!*.md'` is empty, so the two
  heads differ only in this document. That the Markdown is genuinely unread by
  those gates was re-verified against the test source rather than inferred from
  a path grep — the only suite that executes documentation examples,
  `tests/behaviour/test_documentation_examples_behaviour.py`, iterates an
  explicit three-item allow-list (`README.md`, `docs/users-guide.md`,
  `docs/v0-2-0-migration-guide.md`) and never globs `docs/`, so no test can
  reach this file by directory walk either.
- [x] (2026-10-02 20:11Z) **A Markdown defect in this document was found and
  fixed; it would have failed CI.** Running `make markdownlint` — which the
  four requested gates do **not** invoke, although CI's `lint-test` job runs it
  before `Run lint, including Skylos dead-code detection` — reported two
  `MD046/code-block-style` errors, at the audit list and at the `uv` transcript
  of the two entries added in `a6d1e414` and `c6f67235`. The mechanism is worth
  recording because the symptom misleads: for a checkbox item, `- [x] text`
  gives the item a content indent of 2, so a blank line followed by indent-6
  content is parsed as an **indented code block**. The failure was therefore
  not a style nit — rendering the affected region with `markdown-it` showed the
  entire five-bullet rebase audit and both recovery-refs paragraphs emitted as
  `<pre><code>`, so the entry's most important evidence was published as
  literal code. Provenance was established before acting: the pre-rebase head
  `74b9f004` is clean under the same config (1 file linted, 0 issues — asserted
  against the file count, because `markdownlint-cli2` reports
  `0 issues in 0 files` for an empty or unmatched scope, a vacuous pass that
  reads as clean). The defect is thus a regression introduced by these two
  entries, not inherited. The fix re-indents every continuation line of the two
  entries from 6 to 2 spaces, matching the file's own long-standing convention,
  and was verified three ways: `markdownlint` drops to 0 issues across 83 files;
  `markdown-it` reports exactly two `<pre><code>` blocks, the two real fences,
  with the prose restored to visible text; and the whole-file **token stream is
  byte-identical to before the change**, so not a word of content moved.
  `mdtablefix` then re-wrapped the freed width (`make check-fmt` initially
  failed at `+52 −57` because narrowing the indent shortens the line budget),
  after which `check-fmt` and `markdownlint` both exit 0 and `nixie` validates
  all diagrams. **The lesson is that the four named gates are not the gate
  set.** `markdownlint` is local, fast, and part of CI's blocking path, and its
  omission from the requested four is exactly how a CI-blocking Markdown defect
  escapes a "fully gated" claim; a documentation edit that never runs it has
  not been gated, whatever the other four report.
- [x] (2026-10-02 20:42Z) **Second rebase round: the target moved again and
  produced a real conflict.** After the push of `068cc43a`, `origin/main`
  advanced from `c65d843c` to `a592b50c` ("RFC 0001: Execution interception for
  test doubles and passthrough (#517)", one commit, touching `docs/contents.md`,
  `docs/rfcs/0001-execution-interception.md`, and `docs/roadmap.md`).
  `git merge-tree --write-tree --messages HEAD origin/main` returned rc=1 with
  exactly one conflicted path, `docs/roadmap.md`. The branch was rebased onto
  `a592b50c` as `86449239`, 45 commits, no merge commits.
- [x] (2026-10-02 20:42Z) **The conflict was two insertions at one anchor, and
  the resolution keeps both.** Both sides insert after roadmap item 10.4.3, at
  the blank line before the file's single link-reference block: this branch
  adds one line (`[#440]:`), main adds a 181-line section
  (`## 11. Test doubles without a process (RFC 0001)`, with subsections
  11.1–11.5). Git sees one insertion point and two different insertions, so the
  base section of the `zdiff3` hunk is empty. The two insertions are only
  coincidentally adjacent: a link-reference definition is addressable and works
  anywhere in the file, so its position carries no meaning, whereas section 11
  is content whose position is fixed. The resolution therefore takes main's
  section 11 verbatim and keeps the `[#440]:` definition above the shared
  `[issue-379]:` line — the ordering the branch already had at `068cc43a`.
  Nothing is dropped from either side.
- [x] (2026-10-02 20:46Z) **The rebase was verified against the true three-way
  merge, not merely observed to apply.**
  `git merge-tree --write-tree --messages 068cc43a origin/main` produced tree
  `53a97c95`, in which `docs/roadmap.md` was the only marker-bearing path;
  substituting the resolved blob into that tree yields `0c93b1c7`, which is
  exactly `86449239^{tree}`. Two independent corroborations:
  `git diff --stat 068cc43a HEAD` lists *only* main's three files, so no branch
  content moved; and the resolved file is identical to the `068cc43a` version
  as a **multiset of lines**, differing solely in the position of the one
  `[#440]:` line, with zero lines lost. Against `origin/main` the net change to
  `docs/roadmap.md` remains `+6 −1`, exactly the patch of `aab210be`.
- [x] (2026-10-02 20:47Z) **Two of this round's own verification checks were
  defective and were caught rather than trusted.** (1) The first attempt to
  rebuild the merge tree used `git ls-tree` without `-r`, which lists `docs/`
  as a subtree rather than `docs/roadmap.md` as a path, so the substitution
  matched nothing and the comparison silently compared the tree to itself — it
  had no assertion to catch this, and the retry added one. The defect surfaced
  only because a later, explicit `mktree` attempt failed loudly
  (`fatal: path .codescene/code-health-rules.json contains slash`), which is
  what led to inspecting the first attempt. (2) An earlier "rebase in progress:
  absent" probe was vacuous: in a worktree `.git` is a *file*, so
  `[ -e "$R/.git/rebase-merge" ]` can never be true. The correct form is
  `git rev-parse --git-path rebase-merge`, which confirmed the dry run had in
  fact stopped at step 33 of 44.
- [x] (2026-10-02 20:47Z) **The dry run also disproved an assumption carried
  into this round.** This branch touches `docs/roadmap.md` in two commits,
  `aab210be` (references `[#440]`) and `ed0e12b0` (relocates its definition to
  the tail), so the plan expected the conflict to arise at step 21 and to recur
  at step 33. In fact `aab210be` applied cleanly — its insertion is at line
  ~136, far from main's tail — and the only conflict was at step 33. The
  relocation in `ed0e12b0` is load-bearing and could not simply be dropped: a
  link-reference definition nested in a list item indented 2 spaces has a
  content indent of 2, so the next line indented 2 spaces would be parsed as a
  continuation of the *definition* rather than as a new roadmap item.
- [x] (2026-10-02 20:51Z) **Pushed, mid-sweep, and CI now builds exactly the
  certified head.** `--force-with-lease` bound to the then-current remote head
  `068cc43a` succeeded as `+ 068cc43a...86449239 (forced update)`; the
  displaced head remains recoverable at
  `refs/recovery/issue-440/r2-20261002T204153Z-old-head`. Because the branch is
  now a fast-forward of `origin/main`, `git merge-tree HEAD origin/main`
  returns rc=0 and its result tree `0c93b1c7` equals `HEAD^{tree}`, so the
  merge ref CI builds is the head that was gated — the `CONFLICTING` mergeable
  state is resolved. **This push preceded the end of the sweep**, which is why
  the timestamp is `20:51Z` and not later: GitHub created run `37063193930` at
  `20:51:00Z`, while `make test` was still running (`20:47:43Z`–`20:51:38Z`).
  The ordering is recorded rather than smoothed over; see the audit entry
  below, which found and corrected two stamps this entry had wrong.
- [x] (2026-10-02 20:54Z) **Six gates at `86449239`, all exit 0, tree clean.**
  `check-fmt` 0s, `test` 235s, `typecheck` 1s, `lint` 165s, `markdownlint` 19s,
  `nixie` 0s; every log records `head_before=head_after=86449239`. Two positive
  findings worth keeping: `lint` reached **actionlint**, its final sub-target,
  so unlike a chained abort nothing was left *unobserved*; and clippy ran
  `--all-targets --all-features -- -D warnings`. Test denominators, each quoted
  from one command's own summary: nextest 127 tests run / 127 passed / 0
  skipped; the largest pytest run 2832 passed, 70 skipped; cargo doctests via
  `test result: ok` lines. Zero `FAILED`/`ERROR` lines in the pytest output.
- [x] (2026-10-02 20:55Z) **`markdownlint`'s `Summary: 0 issues in 0 files`
  was resolved rather than hand-waved, and the earlier reading of it is now
  corrected.** The line reads as a vacuous 0-files pass, but the authoritative
  scope number is `Linting: 84 files`, and the repository has exactly 84 tracked
  `.md` files, so the scope was complete. The summary's second number counts
  only files carrying issues — the `Summary:` line is not a pass/fail signal at
  all. Positive controls settle it: a `*` bullet (MD004 is configured as
  `style: dash`) yields `Summary: 1 issue in 1 file` and exit 1, and an
  over-length prose line yields `Summary: 2 issues in 1 file` and exit 1. A
  probe file containing *only* an indented block after a blank line is clean
  because MD046's default style is `consistent`, which requires only that a
  document not mix fenced and indented styles — it does not forbid indented
  blocks in isolation. The earlier entry's phrasing ("0 issues across 83
  files", "reports `0 issues in 0 files` for an empty or unmatched scope, a
  vacuous pass") conflated these two numbers and should be read with this
  correction; the fix it describes remains correct and is confirmed here. Note
  this also refines the `0 files` warning in
  `[[md046-checkbox-item-indent-6-renders-as-code]]`: a bare `--diff`-style
  file-count check can be vacuous, but the reliable test is the
  `Linting: N files` line against the tracked count, plus a positive control.
- [x] (2026-10-02 21:17Z) **CI certified all twelve required contexts at
  `ecebfe7c`, and an audit of these eight stamps found two of them wrong.** Run
  `37064253018` — created `21:01:07Z` at head `ecebfe7c`, the commit that
  carries this entry — returned `completed/success`, and every one of the
  twelve contexts read from ruleset `18427980` is `success` with none absent:
  `lint-test`, `Typecheck and test (Python 3.12)` and `(Python 3.14)`,
  `coverage`, `benchmark-ratchet`, the five `build-native-wheels` legs,
  `verify-wheel-install`, and `Extension-gated tests (Python/Rust boundary)`
  (12 success, 0 not-green, 0 absent). The prior run `37063193930` at
  `86449239` was **cancelled** by this push at `21:01:51Z` with one job
  unfinished (`coverage`); its other fifteen jobs had passed, but a cancelled
  run cannot be cited as green, which is why the certification rests on the new
  run. The two heads differ by exactly the 93-line plan addition and nothing
  else, so the code under test is identical. Separately, a timestamp audit
  against hard evidence — reflog epochs, gate-log footers, file mtimes, and
  GitHub's own `createdAt` — found two of the eight stamps wrong: the push
  stamp read `20:57Z` when CI proves the push was `20:51:00Z` (a 6-minute late
  error that silently reordered it *after* the "six gates green" entry, hiding
  that the push preceded full validation), and the conflict-resolution stamp
  read `20:45Z` against a `20:42Z` rebase finish (2.9 minutes late). Both are
  corrected here. This is the same fault the retrospective below records,
  recurring in the very entries that report it, and re-derived from the
  introducing commits rather than from labels.
- [x] (2026-10-02 21:40Z) **CI is green at `963e06bf`, and this is the last
  fact this document can record about itself.** Run `37066696955` (created
  `21:24:59Z`) returned `completed/success` at `21:40:46Z`, with the companion
  `Rust boundary verification` run `37066696666` also `success`, and all twelve
  required contexts green with none absent — the same twelve listed in the
  entry above, re-read at this head. The rebase itself is now fully verified on
  three independent axes: no commit was lost (all 44 pre-rebase subjects are
  present in the rebased range, with only these two plan commits added, 44 →
  46); every source, test, and build path is byte-identical to the pre-rebase
  head (`git diff 068cc43a 963e06bf -- . ':(exclude)docs'` is empty), so only
  documentation moved; and the merge ref equals `HEAD^{tree}` (`8f7b1493`,
  `merge-tree` rc=0), so CI builds exactly the gated head. **The limit worth
  recording:** a commit cannot carry the certification of its own head, because
  pushing the entry that would contain it creates a new head to certify. Every
  Progress entry here therefore certifies the head *before* the commit that
  carries it, and this one is no exception. The terminal evidence for the final
  head lives in GitHub's run list, not in this file, and the workflow ends by
  reading it there rather than by adding another entry.
- [x] (2026-10-10 13:05Z) **Four statements in `Verification plan` were
  corrected against the shipped code before the proof assessment was posted.**
  Verifying the claims that were about to be published found them describing
  tests that do not exist in the form claimed. O5's statement asserted a
  path-separator rule that lives in `advisory_path_rejection`
  (`cuprum/executable_paths.py:264`) rather than in `resolve_binding`; the
  shipped `resolve_binding` (`cuprum/executable_binding.py:283-288`) tests only
  `is_absolute()`, so it anchors *any* non-absolute binding — bare name
  included — at `cwd` when one is supplied. O3, O4, and O5 each described a
  single test body carrying a paired assertion where the repository has two
  tests. The code, the `resolve_binding` docstring, and the named tests agree
  with each other in every case; only this plan's prose was wrong, and nothing
  in the feature changed. The `PurePath.parts` axiom was corrected on the same
  grounds: `resolve_binding` depends on the separator facts by declining to
  guess, not by testing for a separator.
- [x] (2026-10-10 13:05Z) **O5's strategy was measured rather than assumed, and
  the first figure was discarded as unstable.** The property module carries no
  assertion that both relative-path shapes were generated, unlike O2's real
  guard, so the obligation's non-vacuity rests on the strategy's construction
  and on measurement. A first 200-draw sample gave 164 separator-bearing and 36
  bare names; re-running the same 200 draws gave 118/82, and 2000 draws gave
  1123/877, so the single sample was not reproducible and was not recorded. The
  plan now cites the 2000-draw distribution and states the weaker non-vacuity
  position explicitly. Separately, `_CWD` draws an absolute path four times
  more often than `None` (360/40 in 400 draws), so the no-`cwd` branch is
  reached but is the rarer of the two.
- [x] (2026-10-10 16:20Z) **The four corrections above tripped the Markdown
  formatting gate and were reflowed.** Prose added by the O3/O4/O5 corrections
  wrapped at a fill width `mdtablefix` disagrees with, so `make check-fmt`
  failed `+12 -12` on this file and CI's `lint-test` job failed on the same
  step; the local and hosted runs used the same `mdtablefix` 0.6.1, so the
  failure was reproducible rather than environmental. `make fmt` reflowed the
  affected paragraphs; the diff is pure line wrapping, with no change to
  wording, code spans, or em-dashes. The lesson is that `mdtablefix`'s
  effective fill width is narrower than 80 columns wherever a paragraph
  contains inline code spans, so the repository's own wrapping convention is
  not the gate's.
- [x] (2026-10-10 16:55Z) **The branch was rebased onto the moved `main`
  (`24c39650`, "Resolve the scoped catalogue in `sh.make` (#514)") and the six
  textual collisions were resolved by hand, then the audit found a defect in
  the rehearsal and it was repaired.** GitHub reported the PR `CONFLICTING` /
  `DIRTY` because `main` advanced while CI ran, and the green rollup from the
  previous head tested a merge ref built on the older base, so it could not be
  transferred. The nine overlapping files were resolved by keeping both sides'
  intent: `main`'s `catalogue` field and `_resolve_narrowed_catalogue` coexist
  with this branch's `executable_bindings` field and its inlined
  `merge_executable_bindings` call, and both contribute tests and scenarios.
  The rebase was rehearsed in a scratch worktree first, and the rehearsal is
  what caught the defect: `git diff --check` reported four leftover conflict
  markers committed inside `cuprum/unittests/test_context_isolation.py`, whose
  blob consequently did not parse. A union resolution script had correctly
  refused to write that file, but its assertion was misread as a pass and the
  file was staged unresolved. The lesson is that a resolution script's refusal
  is a failure signal, and that `git diff --check` across the rebased range --
  not a spot-check of the files that parsed -- is what proves no markers were
  committed. Repairing the single affected commit and replaying the remainder
  produced `f30c9b1c`; the live branch was then advanced to that audited result
  rather than resolving the same six conflicts a second time by hand, and every
  post-condition was re-verified in place.
- [x] (2026-10-10 16:55Z) **The rebase audit passes on all counts, and the
  merge-tree oracle now reports the branch as content-identical to a merge with
  `main`.** All 49 commits replayed (no commits lost, none became empty, no
  merges), 43 of the 49 are byte-identical to their originals, and the 6 that
  differ are exactly the conflict-resolved commits and their dependants. The 11
  files `main` changed but this branch never touched are byte-identical to
  `main`, every Python file parses, and the `catalogue.feature` scenarios and
  their `pytest-bdd` bindings remain in 7-to-7 correspondence.
  `git diff --check` against the target is clean.
  `git merge-tree --write-tree HEAD origin/main` returns rc=0 with a result
  tree equal to `HEAD`'s own tree (`b8cc7c417e4a761fc865110524b77dac7f35e57d`),
  which is the strongest available evidence that nothing remains to reconcile
  against `main`.
- [x] (2026-10-10 17:35Z) **The union resolution left two real defects that the
  rebase audit could not see, and both are repaired.** CI at `9e9b9315` failed
  `lint-test` at its `Check formatting` step because the join point in
  `cuprum/unittests/test_context_isolation.py` carried one blank line where
  `ruff format` wants two; the same commit carried a second, independent defect
  that CI never reached, and a third surfaced only under `make lint`.
  `make check-fmt` aborts at the first failing stage, so the `mdtablefix`
  re-wrap this plan needed was invisible from the served failure; running the
  whole target locally reported both in one pass, and both were real. A fourth
  defect was found by the scrutineer's sweep rather than by CI:
  `tests/behaviour/test_catalogue_behaviour.py` reached 419 lines after the
  merge added `main`'s scoped-catalogue scenario to this branch's two binding
  scenarios, and the strict Pylint pass enforces 400 there. The
  scoped-catalogue scenario and the four steps only it uses moved to a new
  `tests/behaviour/test_catalogue_scope_behaviour.py`, leaving the original at
  331 lines; the cut is self-contained because those four step texts each occur
  exactly once in `tests/features/catalogue.feature` and reach for no fixture
  outside the scenario. The move is proven non-vacuous by two mutation probes,
  each of which failed the extracted step with its own message. All four
  repairs are recorded above under Surprises and discoveries.
- [x] (2026-10-10 17:40Z) **The scenario-to-step correspondence survives the
  split, and the new module is collected.** `pytest --collect-only` over the
  two catalogue behaviour modules reports exactly 7 tests, matching the feature
  file's 7 scenarios one for one, and all 7 pass. The new filename matches the
  `tests/behaviour/test_[a-h]*.py` glob in `PYTEST_TARGETS`, so `make test`
  collects it without a Makefile change, and no test or configuration file
  enumerates behaviour module filenames, so nothing else needed updating.
  `pylint --jobs=1 tests/behaviour` now reports 10.00/10 with no `C0302`, and
  `ruff check` and `ruff format --check` are clean on both modules.
- [x] (2026-10-10 16:38Z) **The double anchoring the reassessment found is
  repaired, and the repair is witnessed by the child rather than by the audit
  surface.** `_execution_cwd` joins `_cwd_arg` in
  `cuprum/_subprocess_context.py` as the absolute anchoring base for a relative
  binding; both observation builders — `_prepare_execution_observation`
  (`cuprum/_command_internals.py`) and `_build_pipeline_observations`
  (`cuprum/_pipeline_internals.py`) — resolve against it, while the spawn keeps
  the caller's own spelling through `_cwd_arg`, which stays a pure
  pass-through. The three regression tests assert the child's reported path
  *before* `resolved_path`, and that order is load-bearing: under a double
  anchoring `resolved_path` still names the singly-anchored file, so an
  assertion on it alone would pass while the divergence stood. Coverage spans
  the single command, a two-stage pipeline with a per-stage decoy, and line
  iteration through `SafeCmd.lines()`, which reaches the same fix by sharing
  `_prepare_execution_observation`. Every witness was proven non-vacuous by a
  mutation probe that failed it for its own stated reason — reverting
  `_execution_cwd` to the pre-fix pass-through failed all three at once — and
  each probe was restored with the restore verified by `sha256sum`.
- [x] (2026-10-10 16:38Z) **The resolver-error contract was documented as one
  thing and shipped as two, and both guides now distinguish the boundaries.**
  `resolve_binding` raises `TypeError` for a non-`str` resolver result, but
  `CuprumContext.resolve_executable` catches `Exception` and re-raises
  `ExecutableResolutionError`, so a caller reaching a resolver through a
  command never sees the `TypeError` that `docs/users-guide.md` and
  `docs/v0-2-0-migration-guide.md` both named. Both now say which boundary
  reports which error, and a new test pins the outer half — the only half
  reachable from a command a caller actually runs — by requiring the run to
  raise `ExecutableResolutionError` with the `TypeError` chained as `__cause__`.
- [x] (2026-10-10 16:38Z) **A pipeline that is refused at a later stage now has
  a test proving no stage resolves, and the empty working directory is pinned
  where its behaviour is visible.** The refusal test attaches a counting
  resolver to both an allowed first stage and a forbidden later one and asserts
  `ForbiddenProgramError` with both counters at zero; a mutation probe that
  interleaved enforcement with resolution made it fail with
  `the permitted stage's resolver must not run for a refused pipeline, ran 1
  times`,
  which is the causality the test claims. For `""`, `Path("")` is `Path(".")`,
  so it falls through `_execution_cwd`'s ordinary arithmetic and is left as it
  falls; mapping it to `None` would claim the caller named no directory when
  they named one, converting a loud `FileNotFoundError` at the spawn into a
  silent run somewhere the caller never asked for. That answer is unobservable
  in a run only because `asyncio` refuses an empty `cwd` before a child starts,
  so it is pinned in `cuprum/unittests/test_stage_stream_fds.py` at the helper
  level, beside the `_cwd_arg` row that renders the same spelling.

- [x] (2026-10-10 18:22Z) **The bound fail-fast case the proof assessment asked
  for now exists, and building it disproved the arrangement it first assumed.**
  The gap was real: every fail-fast run in the suite binds nothing, so the
  sanitized decision event's `resolved_path=None` was unexercised against a
  stage that actually had a path to withhold. The first attempt reused the
  existing three-stage helper and bound its failing stage, which is where the
  arrangement failed — a binding is keyed by logical program, and all three
  stages ran the same interpreter, so the binding captured all three. A probe
  showed the consequence directly: no `pipeline_fail_fast` phase at all, just
  three `exit 3` events, because three stages that all settle in one batch
  leave nothing to terminate and the run latches a failure index silently.
  `run_bound_failing_pipeline` therefore gives the failing stage a program of
  its own, which is the only stage bound and the only one the two downstream
  stages do not share. The new module
  `cuprum/unittests/test_bound_fail_fast_event.py` then asserts both halves:
  the bound stage reports its path on `plan`, `start`, and `exit`, while the
  decision event withholds it and still names the logical program. The third
  test is the negative control — the unbound siblings must carry no path, so a
  regression that resolved every stage could not pass the module by accident.

- [x] (2026-10-10 18:24Z) **The anchoring change pushed
  `cuprum/_pipeline_internals.py` one line over the 400-line Pylint cap, and
  the fix was to delete a comment rather than add a suppression.** The file sat
  at 398 lines on `HEAD`; the new import and a three-line call-site comment
  took it to 401, and `make pylint-classic` failed with
  `C0302: Too many lines in module (401/400)`. The gate is real and the file
  was already within two lines of it, so this was a latent trap rather than a
  surprise — the next unrelated one-line addition would have hit it too. The
  comment was the removable part: `_execution_cwd`'s own docstring already
  carries the full rationale, down to why `""` falls through, so the call sites
  only need to point at it. Both call sites now read
  `# Anchored once, here, for the reason documented on ``_execution_cwd``.`,
  which brings `_pipeline_internals.py` to 399 and `_command_internals.py` to
  341. No lint was silenced, and `pylint-classic` rates 10.00/10.

- [x] (2026-10-10 16:59Z) **Both pre-readiness assessments returned at `16:06Z`
  and `16:07Z`, and all eight of their findings are actioned in the working
  tree.** The completeness/correctness reply raised three findings and the
  proof reply five obligation notes; the dispositions are recorded here so the
  next reader can check them against the code rather than re-deriving them.

  The correctness reply's first finding was the relative-`cwd` double anchoring
  — the production defect — and its second was the pipeline refusal test the
  plan's O3 promised but never carried. Both are now repaired, and the refusal
  test asserts zero resolver calls on *both* stages, so a regression that
  interleaved enforcement with resolution fails on the permitted stage rather
  than passing on the refused one. Its third finding was documentation: the
  guides described a resolver's wrong-type result as reaching the caller as
  `TypeError`, which is true of `resolve_binding` directly but not of normal
  execution, where `ExecutableResolutionError` is raised with the `TypeError`
  chained. `docs/users-guide.md` and `docs/v0-2-0-migration-guide.md` now
  distinguish the two boundaries, and the tested implementation was left
  unchanged, which is what the finding asked for.

  The proof reply's five obligations are each closed against the artefact it
  named. O1 now separates the universal requirement from the sampled evidence
  and scopes the `NOT_FOUND`/`NOT_EXECUTABLE` witnesses to POSIX. O2's
  state-machine docstring gives up the coverage claim it could not support and
  names the named tests as the binding-content evidence. O3's pipeline refusal
  case is the same one the correctness reply asked for. O4 gained the bound
  fail-fast case (recorded at `18:22Z`) and had its real-execution event test
  rewritten off the phase-keyed mapping that could hide an earlier wrong event.
  O5's relative-path property now asserts exact equality with the composed path
  using native `Path` comparisons. This sentence originally continued "and the
  `_CWD` strategy generates relative directories so the anchoring defect is
  reachable from the generator", which is **false** and is corrected here
  rather than deleted, because it was the basis of a closure claim: the
  anchoring defect lives in the execution path, not in `resolve_binding`, and
  `_CWD` feeds only the absolute-binding properties. What reaches the defect is
  the named execution regression, not the generator; see the `20:15Z` entry
  below.

  Two evidence errors in the request comment itself were raised by the
  correctness reply and are corrected in a separate comment posted at `16:58Z`:
  the series is 51 commits rather than 50, and `_catalogue_binding_support.py`
  has two `pytest.fail` call sites rather than three. Neither affects a finding.

- [x] (2026-10-10 17:12Z) The six commit gates pass at tree `73d5d2a0`'s
  contents once two Markdown files are reflowed and one word is corrected; no
  entry here names the resulting tree, because this entry is itself part of it
  and a digest written here would be false by the act of writing it. A first
  gate sweep failed `make check-fmt` at its third stage (`mdtablefix --check`
  wanted to reflow this document and `docs/users-guide.md`) and failed
  `make markdownlint` and `make lint` on a single spelling violation at this
  file's line 1229: the US-spelled form of "artefact", in a sentence about the
  proof reply. Both were mine and both were mechanical, but they were found the
  hard way, and the sweep also exposed that a `spelling` failure inside
  `make lint` aborts before `github-actions-lint`, so `yamllint --strict` and
  `actionlint` went unobserved until the re-run. The re-run fixes the word,
  applies the reflow, and re-runs `check-fmt`, `markdownlint`, `nixie`, and
  `lint`; all four exit 0, and `actionlint` is observed for the first time at
  `17:12:05Z`. `make typecheck` and `env -u BASH_ENV make test` were not
  re-run, and deliberately so: the two trees differ *only* in Markdown files,
  so the cached results are valid for the unchanged Python and Rust inputs
  rather than merely convenient. `nixie` was re-run anyway despite that
  reasoning, because it is a Markdown-reading gate and the reasoning covered
  only `typecheck` and `test`. The mdtablefix reflow was verified to be
  whitespace-only before it was trusted: 24126 words before and after with zero
  non-equal diff opcodes, and all 17 fenced blocks byte-identical, so
  `--ellipsis` did not reach into a code span here. Landing this entry reflowed
  a second time and re-ran `check-fmt`, `markdownlint`, `nixie`, and `lint`,
  then caught one more self-inflicted defect: the entry had quoted the
  misspelled word verbatim in a backticked span, which the spelling gate
  checks. It now describes the word instead of repeating it.

- [x] (2026-10-10 19:40Z) **Both assessments were rejected at `16:06Z` and five
  defects are now closed, four of them mine.** The correctness reply accepted
  the production repair on static evidence but declined to accept the
  verification work; the proof reply accepted the sampled evidence as
  substantive without accepting it as exhaustive. The three evidence findings
  and one wording correction are actioned here.

  The central finding was the sharpest and I had it wrong twice over. I had
  claimed that widening `_CWD` to draw relative directories extended
  relative-binding coverage. It does not: `_CWD` is consumed only by the
  *absolute*-binding properties (`cwd=_CWD` at three decorators), while
  `test_relative_binding_resolves_inside_the_working_directory` is decorated
  `@given(path=_RELATIVE_PATH, cwd=_ABSOLUTE_PATH)`. A strategy feeding the
  properties I never touched cannot extend a property I did. Worse, the
  widening was *actively misleading*: those absolute-binding properties assert
  the resolution is absolute, so a relative directory there would break them,
  and the same claim in O5's text made the augmented strategy look like
  relative-resolution evidence when it was evidence for the opposite rule. The
  fix is a property of its own,
  `test_relative_binding_joins_a_relative_directory_verbatim`, asserting the
  join is relative — deliberately the *opposite* claim, so each contract is
  falsifiable alone. A mutation making `resolve_binding` anchor the directory
  itself (`Path(cwd).resolve() / resolved`) fails the new property with
  `A relative directory is joined, not anchored` while the absolute-directory
  property still passes, which is the proof that the absolute strategy was
  structurally blind to the defect. O5's text now describes the split and says
  why a widened decorator would have been the wrong fix.

  O3 named `test_a_pipeline_anchors_a_relative_cwd_once_for_every_stage` as the
  positive control for the pipeline's resolvers. It is not: that test binds two
  static string paths and counts nothing, so it can never fail on a
  resolver-count regression. The new
  `test_a_permitted_pipeline_resolves_each_stage_exactly_once` is that control
  — the same pipeline shape as the refusal test, differing only in whether the
  later stage is allowlisted, with a counting resolver per stage. A mutation
  evaluating each resolver twice per resolution failed it with
  `the producing stage must resolve exactly once, ran 2 times`, an assertion
  the refusal test cannot reach; the first probe I tried tripped a `zip()`
  `ValueError` instead, which is a weaker signal and was not accepted as the
  non-vacuity evidence.

  F3 replaced a pipeline test that asserted only each stage's `resolved_path`
  with one that runs a relaying script and asserts the child's own stdout in
  exact stage order. Under a double anchoring `resolved_path` still names the
  singly-anchored file, so the path assertions alone could pass while the
  impostor under the doubled prefix did the work; the stdout assertion is what
  makes the divergence observable. Reverting the pipeline's anchoring makes it
  fail with both stages reporting `IMPOSTOR:`.

  Both replies then asked for one further assertion on that test: that each
  stage exited successfully. The stdout assertion alone would already fail on a
  decoy, so this is not the same claim — it upgrades "the stage printed the
  bound file's report" to "the bound file ran to completion", which matters
  because a child that failed part-way could in principle have emitted some
  output first. `result.stages[i].exit_code` reads the `CommandResult`, not the
  `exit` *event*, and a first probe that perturbed the event's exit code was
  therefore correctly not detected: the two are separate constructions. A
  second probe adding one to the `CommandResult`'s `exit_code` failed the new
  assertion with `both stages must run to completion, got exit codes [1, 1]`.

  The assertion binds no name for the exit codes. Writing it first as
  `exits = [...]` pushed the function to eleven locals and `ruff check`'s
  `too-many-locals` (limit ten) rejected it, which the gate run reported before
  any commit. The fix inlines the comprehension in both the comparison and the
  failure message rather than raising the limit or adding a blanket
  suppression, since the limit is what prompted the earlier extraction
  discipline in this module. The message is then built only on failure, and the
  cost is a repeated comprehension that reads as a deliberate trade at a local
  cap. The probe was re-run against the inlined form and fails on the assertion
  itself —
  `AssertionError: both stages must run to completion, got exit codes [1, 1]`,
  with pytest's own `assert [1, 1] == [0, 0]` beneath it — so the recorded
  evidence describes the form that is committed, not the form that was first
  written and rejected.

  F4 corrected `Validation and acceptance` clause 6, which stated that a
  non-`str` resolver result raises `TypeError` without naming the boundary.
  Both behaviours are tested and both are now stated: `TypeError` from a direct
  `resolve_binding` call, `ExecutableResolutionError` with that `TypeError` as
  `__cause__` from `CuprumContext.resolve_executable` and from execution. The
  module docstring's resolution-shape bullet was corrected for the same reason
  the property was split: it claimed a relative binding "yields an absolute
  path inside that directory", which is true only for an absolute directory.

  The lesson worth keeping is that a misleading test claim is worse than a
  missing one. `_CWD` drawing relative paths *looked* like coverage of the
  defect that had just been fixed, and I reported it as coverage in both the
  request comment and the plan; the augmentation was real, the inference from
  it was not, and only checking which decorators consume the strategy showed
  the difference.

- [x] (2026-10-10 20:15Z) **Three proof findings survived the `19:40Z` closure
  claim, and cross-checking the reply against the source is what found them.**
  The entry above says all five obligations are closed; two of the three
  remaining were prose claims I had written as closed without verifying, and
  the third was a test assertion the reply asked be strengthened. This is the
  same failure mode the earlier entries record — asserting closure from intent
  rather than from the artefact — and it is recorded here rather than quietly
  repaired.

  The two prose defects were in O1 and O2 of the verification plan. O1 said the
  universal claim "is discharged for the shapes the alphabet can form", which
  overstates sampled evidence as discharge and contradicts the sentence
  immediately before it; it now says the evidence covers sampled inputs from
  the alphabet and the deterministic boundary cases the named table pins. O2
  said a generator guard "establishes is exactly one binding factory ran" where
  the guard asserts at least one did; "exactly one" was an overclaim in the
  opposite direction from O1's, overstating a lower bound as an exact count.

  The third was `test_bound_fail_fast_event.py`'s lifecycle assertion, which
  accepted any path ending in `failing.py`. The path is now exposed by a
  sibling module-scoped fixture — `bound_failing_script`, consumed by the
  events fixture so both describe the same run — and the assertion is equality
  with that path. A fourth mutation probe rewrites only the reported path to a
  sibling directory under the same basename, leaving the spawn to run the
  genuinely bound file: it failed the assertion, reporting
  `.../bound0/elsewhere/failing.py` against the expected
  `.../bound0/failing.py`. The produced value still ends in `failing.py`, which
  is the demonstration the reply asked for that the suffix form could not have
  detected it.

  A second pass over the same reply found **two further findings it raised that
  the `19:40Z` entry had also claimed closed**, both about what the generator
  actually reaches. The first is that the `_CWD` comment in
  `test_executable_binding_property_based.py` still implied the strategy
  reaches the anchoring defect. It does not, and the comment is the mechanism
  by which that false belief spread: `_CWD` feeds only the absolute-binding
  properties (verified by enumerating every decorator that names it —
  `test_absolute_binding_resolves_to_itself`, `test_resolution_is_idempotent`,
  and `test_resolver_is_invoked_once_per_resolution`), so its relative branch
  raises a claim about absolute bindings rather than exercising relative
  resolution. The comment now states what the strategy feeds, what covers the
  relative case, and why the distinction matters. The second is O2's method,
  which described the state machine as "model checking"; Hypothesis drives it
  with generated rules, so it is sampled state-machine testing and the plan now
  says so, with the guard's corresponding weakness restated beside it. The O5
  line in the `19:40Z` entry carried the same false generator claim and is
  corrected in place rather than deleted, because it was the stated basis of a
  closure claim. The lesson repeats the one already recorded above: I checked
  the reply's *headline* findings against the source and stopped, when the
  graded obligations were where the unverified claims were.

- [x] (2026-10-10 21:05Z) **The gate run caught the fourth finding's assertion
  pushing its test over the local-variable limit, and the fix was to inline the
  comprehension rather than raise the limit.** The exit-code assertion recorded
  in the `20:15Z` entry was written as `exits = [...]` followed by
  `assert exits == [0, 0]`. That made
  `test_a_pipeline_anchors_a_relative_cwd_once_for_every_stage` bind eleven
  locals against `pyproject.toml`'s `max-locals = 10`, and `make lint` failed in
  `python-lint`'s first line with `too-many-locals` before the tree was ever
  committed. The scrutineer established causality rather than inferring it:
  piping the `HEAD` revision of that file through `ruff check --stdin-filename`
  passes, the working tree reports `11 > 10`, and the only difference is the
  one added binding, so this was not a stale-cache or environment artefact.

  The narrowing matters because this module has already been reshaped once to
  stay under that same limit, and the temptation here was to relax the limit or
  add a suppression for one assertion. Inlining the comprehension in both the
  comparison and the failure message keeps the check at full strength, leaves
  the limit as a genuine constraint, and costs only a repeated comprehension
  that the code comments as a deliberate trade at a cap. Ruff then reports
  `All checks passed!` on the file and the module's sixteen tests pass.

  Re-running the probe against the *inlined* form was necessary rather than
  ceremonial: the recorded evidence named a failure message the old form
  produced, and a form change invalidates the evidence unless re-measured. The
  probe adding one to `CommandResult`'s `exit_code` fails the inlined assertion
  with
  `AssertionError: both stages must run to completion, got exit codes [1, 1]`
  and pytest's own `assert [1, 1] == [0, 0]` beneath it, so the plan's claim
  now describes the committed form. The production file was restored and
  `git diff HEAD` for it is empty.

  Two incidental facts from the same run are worth keeping. The gate log shows
  `make lint` failing *earlier* than the spelling stage that aborted a previous
  sweep, so `interrogate`, `df12-pylint`, `ambrleaks` and `skylos` were
  short-circuited inside `python-lint` while `lint-clippy`, `lint-whitaker` and
  `github-actions-lint` never ran at all — a partial lint observation, not a
  clean one. And `typos.toml` was *not* regenerated by this `make lint` run
  (digest unchanged before and after), because the run never reached the
  spelling stage that rewrites it; the green spelling result for this tree
  comes from `make markdownlint`'s prerequisite instead.
- [x] (2026-10-10 21:40Z) **Both `92ab8f17` assessments returned accepted, and
  the proof reply's three wording corrections were applied.** The completeness
  reply
  ([`6100996433`](https://github.com/leynos/cuprum/pull/571#issuecomment-6100996433))
  closed all four findings and echoed
  `EP440-COMPLETE-92ab8f1-20261010T201500Z` against head `92ab8f17`; the proof
  reply
  ([`6101015126`](https://github.com/leynos/cuprum/pull/571#issuecomment-6101015126))
  accepted O1–O5 as sampled verification and echoed
  `EP440-PROOF-92ab8f1-20261010T201500Z`. Neither started a formal review, and
  neither reopened a repaired test gap. Two substantive assertions in these
  corrections were verified against the tree before editing rather than taken
  on the reviewer's word:

  - The claimed construction "bind `sys.executable`, run
    `print(sys.executable)`" **does not exist anywhere in `cuprum/`**. The
    child-identity mechanism the tests actually use is the generated script
    reporting its own `sys.argv[0]`, per `_SCRIPT`'s own comment. This was a
    plan-prose defect, not a code defect, and it affected four descriptions
    (O3's evidence, O5's evidence, EP-M3's acceptance line, and acceptance
    clause 2), plus acceptance clause 2's preamble promise that *each* clause
    "is asserted by a named test" — the `sys.executable` form named no test.
  - The claim that a combined property "would have to drop the `is_absolute`
    assertion" is **false**, and it appeared in three maintained places: the
    property's docstring, O5's non-vacuity paragraph, and the Decision log
    entry that recorded the split, where the same assertion read "would have to
    abandon its absoluteness assertion" and so is easy to miss when sweeping
    for only the one wording. A single property over `_CWD` could branch on
    which spelling the generator drew. The split is still right, but for a
    different reason: two properties give each domain its own run, whereas a
    conditional assertion inside one property is unexercised on a run that
    draws only the other spelling. All three texts now state the real
    justification, so none overstates what the design forces.

  The third correction separated the helper-level invocation guarantee
  (`resolve_binding` calls a resolver once per *invocation*) from the
  execution-level one, which the observation preparation supplies. O5's
  statement had attributed the once-per-execution property to the helper alone.

  These are documentation and docstring edits only: no production code changed
  this round, which is why the tree needs a fresh gate run before it can be
  published as a new head.
- [x] (2026-10-10 22:53Z) **The corrections follow-up for `92656c79` was
  answered: corrections 1 and 2 discharged, the four `sys.executable`
  replacements accepted, and one remaining prose defect accepted and fixed.**
  The reply
  ([`6103000584`](https://github.com/leynos/cuprum/pull/571#issuecomment-6103000584),
  `EP440-CORRECTIONS-92656c79-20261010T224545Z`) confirmed that the combined-
  property argument is discharged at all three sites, that O5 now separates the
  helper-level invocation count from the execution-level one, and that O3, O5,
  EP-M3 and acceptance clause 2 all describe `sys.argv[0]` with their named
  witnesses existing. It found no missed occurrence of the three original
  claims *in the inspected correction areas* — a scope the reply states
  explicitly, so this is not a whole-tree clearance — and it did not reopen the
  accepted implementation or test evidence.

  It did find a defect in the replacement text I wrote for acceptance clause 2,
  and the finding is correct. The sentence read that an ignored binding "would
  still hold" the assertion "because both report *some* path — only the marker
  on the executed file distinguishes them". That understates the assertion:
  clause 2 is not that the child reports *a* path, it is that the child's
  report equals `f"{_APPROVED}:{approved}\n"`, an exact equality against the
  bound path. A run that executed the decoy reports the decoy's own, different
  path, so the assertion fails there — the exact comparison is itself the
  discriminator, and a second distinguishing observation does not make it
  weaker. The clause now states the real mechanism: the decoy is a working
  executable, so ignoring the binding yields a successful but wrong run rather
  than only a missing-file error, and the child's differing report is what the
  exact-equality assertion catches. The named witnesses and every existing
  assertion were retained, as the reply asked.

  The replacement text was verified against the assertions before it was
  written, not taken on the reviewer's word:
  `test_a_bound_program_runs_the_bound_file_not_the_catalogued_one` asserts
  `result.stdout == f"{_APPROVED}:{approved}\n"` and separately that the
  decoy's marker is absent;
  `test_the_child_receives_the_bound_path_as_its_own_argv0` asserts the same
  stdout plus `result.argv == ("--flag",)`; and
  `test_a_relative_execution_cwd_anchors_a_relative_binding_once` asserts the
  child's report *first* and the audit path second, so a doubly-anchored spawn
  is observed rather than masked by an agreeing `resolved_path`.

  The sweep that preceded the edit returned a false negative and is worth
  recording as a lesson: `grep ... 2>/dev/null || echo "(no matches)"` reports
  an error as a clean result, and the pattern had a near-miss in `*some* path`,
  whose emphasis markers a fixed-string probe for `some path` does not match.
  Re-running with `git grep`, no stderr masking, and the exit code printed
  found the live site on the first attempt. The corrected sweep is the reason
  the fix is believed to be at the only remaining site.

  This moves the head again by one commit, so the hosted CI observation that
  was in flight for `92656c79` is superseded rather than failed, and the
  corrected head needs its own.
- [x] (2026-10-11 00:25Z) **The rebase onto `a026142e` exposed two conflicts
  between this branch's delta and `main`'s, both of which failed only in
  combination.** Each side is correct alone; the red sweep was the first
  observation that the pair cannot coexist. Two repair commits followed.

  *`cuprum/_pipeline_internals.py` was one line over the pylint ceiling.*
  `main` restored that module to exactly 399 lines, leaving zero headroom under
  the 400-line cap, and this branch had added a three-line comment and two
  `__all__` entries on top of it — 404 lines. The two entries, `_collect_hooks`
  and `_enforce_allowlist`, are provably redundant rather than merely optional,
  and `main` itself is the evidence: it re-exports those same two names through
  `as`-self-aliased imports
  (`from cuprum._pipeline_observation import _collect_hooks as _collect_hooks`)
  and does *not* list either in `__all__`, whose five entries are the other
  names. The alias is what performs the re-export; the `__all__` entry was
  never doing that work. No star-import of this module exists anywhere in the
  tree, so nothing consults `__all__` for those names either. Removing the
  comment and the two entries (`4484e1df`, five deletions) restores 399 lines,
  the same count as `main`. The claim that the entries were load-bearing was
  mine, from the earlier round, and it was wrong.

  *`ExecEvent`'s declaration tail was pinned by `main`.* `#575` appends
  `terminal_outcome` and pins it with `test_terminal_outcome_public_api`, which
  asserts `dc.fields()[-1]`. This branch had appended `resolved_path` *after*
  it, so both could not hold. The error was in the rule as I had stated it, in
  the class docstring and in this plan: "new fields are appended to the end of
  the declaration". The real invariant is that a new field must follow every
  field that already had a *positional slot*, which is what protects callers
  who pass arguments positionally. Both reordered fields are brand-new and
  optional, and no caller binds either by position, so their order relative to
  each other is unconstrained. `resolved_path` is now declared before
  `terminal_outcome` (`2ac2e207`), and both docstring and plan say "after every
  field that already had a positional slot" with the tail pin named as the
  reason the distinction matters. This mirrors the remedy this branch had
  already applied to `CommandResult.resolved_path`, which sits before the
  tail-pinned `relay_fallbacks` for exactly the same reason.

  The reorder was checked for positional exposure rather than assumed safe, and
  the check corrected an initial guess of mine. There *is* a positional
  `ExecEvent(...)` construction — `test_public_api.py` builds one to prove that
  positional binding still reaches `exec_id` — so "no positional callers exist"
  would have been false. What makes the reorder safe is narrower and was
  verified directly: that call passes exactly the sixteen fields from `phase`
  through `exec_id`, and both `resolved_path` and `terminal_outcome` sit at
  indices 28 and 29, outside every positionally bound slot. The declared
  sixteen-field prefix is unchanged, which is checked by asserting it equals
  `fields[:16]`. Beyond that call there is no `dataclasses.astuple` call and no
  `__match_args__` consumer of the type.

  This is the second time in this round that a plausible-sounding negative
  claim about the tree turned out to be false when measured, which is why it is
  recorded as the correction it was rather than as a clean verification.

  The general lesson is that a rebase can fail on *composition* alone. A target
  that adds a file at exactly its line cap, or adds a test pinning a
  declaration tail, is correct in itself and breaks a branch whose delta adds a
  line to that file or a field after that tail. Neither defect is visible on
  either side in isolation, and both appear only once the two are replayed
  together — which is why the pre-rebase green said nothing about the rebased
  head.

  The local gate run for this head is recorded on the pull request rather than
  here, so that the record of it cannot invalidate the run it reports.

## Surprises & discoveries

- Observation: there are three spawn call sites but only two `argv[0]`
  constructions. `cuprum/_line_stream/coordinator.py` reuses
  `cuprum._subprocess_execution._spawn_subprocess`, so resolving in that one
  helper covers both the direct and the line-stream paths. Evidence:
  `grep -rn "argv_with_program" cuprum/` shows only
  `_subprocess_execution.py:173` and `_pipeline_spawn.py:98` building a child's
  argument vector. Impact: EP-M3 has two edits, not three.
- Observation: `_StageObservation` is already the per-stage carrier of
  execution-wide policy (`env_overlay`, `env_mode`, `cwd`), and both spawn
  sites already hold one. Resolving into it gives a single resolution point per
  execution and one obvious place to read the effective path from. Evidence:
  `cuprum/_pipeline_spawn.py:97` and `cuprum/_subprocess_execution.py:171` both
  receive the observation. Impact: `_StageObservation` gains `resolved_path`;
  the spawn sites read it.
- Observation: the task packet names `docs/migration-0.2.0.md`, which does not
  exist. The real file is `docs/v0-2-0-migration-guide.md`, and its Python
  fences are executed by the behavioural suite via `tested-example` markers.
  Evidence: `ls docs/` and the `<!-- tested-example: ... -->` markers in that
  file. Impact: the migration note goes there, and any code fence added must
  actually run.
- Observation: the single planned module reached 426 lines against pylint's
  400-line ceiling, because mandatory NumPy docstrings dominate it. Trimming
  prose to fit would have removed the TOCTOU warning the issue requires.
  Impact: the plan's Task 1 module split into `cuprum/executable_paths.py` (219
  lines: `ExecutablePath`, `PathBindingRejection`,
  `InvalidExecutableBindingError`, `classify_executable_path`,
  `executable_path`, `coerce_path_string`, `advisory_path_rejection`) and
  `cuprum/executable_binding.py` (326 lines: `ExecutableResolver`,
  `ExecutableBinding`, `executable_binding`, `resolve_binding`, re-exports).
  The error type moved *down* into the paths module so the dependency stays
  acyclic: the paths module raises it, and the binding module imports it. Both
  are still pure and still import no context module.
- Observation: `type X = typ.NewType("X", str)` produces a `TypeAliasType`,
  which is not callable. Only the plain assignment form
  `X = typ.NewType("X", str)` yields a callable newtype. Evidence: the first
  green run failed 27 tests with
  `TypeError: 'typing.TypeAliasType' object is not callable`. Impact:
  `ExecutablePath` uses the assignment form, matching the sibling `SafePath` in
  `cuprum/builders/args.py`. The repository's `type` statements are reserved
  for genuine alias shapes such as `ExecutableResolver`.
- Observation: `os.access(path, os.X_OK)` returns true for a directory, so the
  obvious advisory probe accepts a directory as an executable. Evidence: a test
  asserting a directory is reported `NOT_EXECUTABLE` failed with `None` before
  `os.path.isfile` was added to the condition. Impact: the probe requires a
  regular file as well as an execute bit.
- Observation: `get_type_hints` is called on public dataclasses by the
  repository's API-contract tests, so a `Program` annotation must stay
  resolvable at runtime. Evidence:
  `cuprum/unittests/test_public_api.py:349-396` resolves `CommandResult`,
  `SafeCmd`, `sh.make`, and `Pipeline.concat`. Impact: `ExecutableBinding`'s
  annotations must remain resolvable, which the two-module split preserves
  because nothing in a TYPE_CHECKING block is referenced at runtime.
- Observation: no gate executes Python doctests. The wheel gate runs Cargo
  doctests and CI has a `doctests` input, but nothing runs
  `pytest --doctest-modules` or equivalent over `cuprum/`. Evidence:
  `grep -rn "doctest" Makefile` matches only the Rust rustdoc flags, and a
  repository-wide search for `doctest.testmod`/`--doctest-modules` finds
  nothing. Impact: the documented examples shipped in EP-M1 were never
  executed, and two of them were broken — `executable_binding` and
  `resolve_binding` both called `Program("tool")`, which is not callable at
  runtime, so every example in those docstrings raised `NameError`. Repaired by
  hand and verified with `uv run python -m doctest`; future examples must be
  checked the same way, because no gate will catch them.
- Observation: adding files under `cuprum/` breaks
  `test_maturin_wheel_build_snapshot`, which pins the built wheel's file list.
  Impact: each milestone that adds a shipped module must re-record
  `cuprum/unittests/__snapshots__/test_maturin_build.ambr` with
  `--snapshot-update` and confirm the diff is exactly the new modules.
- Observation: the Stop hook's gate environment cannot reach the Lody git
  remote helper, so every gate recipe that shells out to
  `uv tool run --from git+https://github.com/...` aborts before it measures
  anything. Evidence: `~/.claude/settings.json` pins `env.PATH` without the
  helper directory; the hook runs `subprocess.run` with no `env=`, and several
  Makefile recipes shadow `PATH` again through `LOCAL_TOOL_ENV`, which defeats
  any `BASH_ENV`-based prepend. Both `make spelling` and `verify-df12-pylint`
  failed as `Failed to resolve '--with' requirement / Git operation failed`,
  which reads like a missing ref rather than a PATH fault. Impact: two gates
  were reported red for environmental reasons, masking three genuine defects
  behind them: the spelling errors, the R9110 delegate, and four ty
  diagnostics. Run the gates with the helper directory prepended until the
  hook's `env.PATH` is fixed; the failure is not caused by any change on this
  branch.
- Observation: the repository's own tests already treat an ambient `BASH_ENV`
  as a hazard, because a host `BASH_ENV` once prepended a `gh` wrapper and
  perturbed the shell the helpers spawn. Evidence:
  `tests/test_ci_workflow_step_bash_env.py` and
  `tests/helpers/workflow_steps.py` both drop it, and
  `docs/developers-guide.md` documents the policy. Impact: on this machine the
  correct remedy is the opposite of the usual one. Dropping `BASH_ENV` is what
  removes the helper from `PATH` here, so the two rules must be applied
  together: keep the helper directory on `PATH` explicitly, and only then
  consider whether `BASH_ENV` also needs clearing.
- Observation: three linters police the same test idiom and pull in opposite
  directions. A deliberate wrong-type call cannot carry
  `# type: ignore[arg-type]`, because `ty` does not honour mypy-style codes;
  `setattr` avoids ruff B010 only to trip ruff PT012; and assigning to a
  read-only attribute needs `Any` to pass `ty` at all. Evidence: four `ty`
  diagnostics, then a ruff B010, then a ruff PT012, each surfacing only after
  the previous fix. Impact: the working idiom is to cast the receiver or the
  argument to `typ.Any` *outside* the `pytest.raises` block and keep exactly
  one simple statement inside it. This is already how
  `cuprum/unittests/test_executable_context.py` handles the same shape.
- Observation: `merge_executable_bindings` needed no mode-reconciling wrapper.
  The sibling `_resolve_env_policy` earns its keep because it must reconcile an
  `EnvMode`; bindings carry no mode, so a matching wrapper forwards its
  arguments unchanged and the DF12 plugin rejects it as R9110. Impact: `narrow`
  calls `merge_executable_bindings` directly, and the asymmetry with the
  environment policy is recorded at the call site rather than in a wrapper
  docstring. Justifying the wrapper by adding a second statement
  (`return merged if merged else None`) would have changed the
  `{}`-versus-`None` contract to satisfy a linter, and was rejected for that
  reason.
- Observation: the Rust gates were reported, and briefly recorded, as
  outstanding when they had in fact passed. `make check-fmt lint typecheck`
  builds a prerequisite chain, and `make` aborts at the first failure, so a
  clean final target proves every gate in that chain ran clean — including
  `rust-lint`, which is the whole of `cargo +nightly-2026-05-28 fmt --check`,
  rustdoc, clippy, whitaker, and the typos gate. Evidence: `make -n` shows the
  chain, and `/tmp/make-code-cuprum-issue-440.out` contains the clippy and
  whitaker invocations and ends with `All checks passed!` from the last
  prerequisite. Impact: do not read a gate's coverage from its target name.
  Check the prerequisite chain before writing down what a target did and did
  not cover.

- Observation: the 400-line tolerance was breached by the *comment* explaining
  a re-export, not by any logic. `_pipeline_internals.py` sat at exactly 400
  lines before EP-M3, so a three-line `__all__` comment was enough to trip
  `C0302` at 405, and shortening it only reached 404. Evidence: three
  successive gate runs, each reporting a smaller overage but still an overage.
  Impact: the remaining slack has to come from moving code, and the plan's own
  tolerance ("stop and extract rather than trim") already said so. Trimming
  prose to fit a size gate degrades the explanation without addressing the
  cause.
- Observation: the extracted helper found a better home than the one the plan
  named. `_resolve_executable_for` was written into `_context_policy.py`, but
  it is not a policy read — it produces a field of the stage observation. It
  sits naturally in `_observability.py` alongside `_resolve_env_overlay` and
  `_base_stage_tags`, which answer the other two observation-input questions.
  Evidence: `_observability.py` already imported `current_context` and had
  `SafeCmd` under `TYPE_CHECKING`, so the move needed no new imports and made
  both call sites shorter. Impact: `_context_policy.py` now holds exactly the
  two *policy* reads, and its docstring says where the third went and why.
- Observation: `CommandResult`'s public-API test pins `relay_fallbacks` as the
  *declaration* tail, not only the positional one. The new keyword-only
  `resolved_path` passed the positional check but failed
  `fields[-1] == "relay_fallbacks"`. Evidence:
  `test_command_result_keeps_relay_fallbacks_as_its_trailing_slot` failed only
  on its last assertion. Impact: the field is declared *before*
  `relay_fallbacks`, which satisfies both readings without weakening a test
  that exists to protect a public contract. A keyword-only field's declaration
  order still communicates the wire shape to anyone reading `dc.fields()`.
- Observation: the two artefacts this plan names for O2 and O4 —
  `test_executable_binding_context.py` and
  `test_executable_binding_telemetry.py` — were never created under those
  names. The coverage landed in `test_executable_context.py` and
  `test_executable_binding_execution.py`, and O4's adapter surface belongs to
  `test_adapter_projection.py`, which already owns the projection contract.
  Impact: the plan's artefact lines are corrected rather than new modules
  conjured; splitting a module to match a name would be work with no behaviour
  behind it.
- Observation: widening `test_adapter_projection.py`'s generator to emit
  `resolved_path` exposed a hole in that module. The sibling
  `test_adapters_agree_on_common_keys_modulo_prefix` derives its expectation
  from `_event_common_fields` itself, so it is an identity check that passes
  whatever the projection carries or drops. `_OPTIONAL_FIELDS` likewise listed
  neither `resolved_path` nor `project`, `operation`, `error_type`, `note`,
  `timeout_s`, `timeout_mode`, `eof_grace_s`, or `pending_readers`, so the
  generator could not reach them at all. Evidence: a seeded mutation removing
  `("resolved_path", event.resolved_path)` from `_verbatim_fields` fails
  `test_projection_includes_exactly_the_non_none_fields`, whose expected
  dictionary is handwritten, yet is invisible to the sibling identity check.
  Impact: the literal key each adapter publishes is pinned by
  `test_the_executed_path_reaches_every_adapter_but_the_labels` and the unbound
  case by `test_the_unbound_case_emits_no_path_key_at_all`; the per-phase
  snapshots lock the bound wire shape.
- Observation: the 400-line module ceiling reaches behaviour tests after all,
  but only *outside* the `test_*` naming convention. `pylint-classic` runs
  twice: a strict pass over `PYLINT_STRICT_TARGETS` and a second pass that
  disables `too-many-lines` for `PYLINT_TEST_TARGETS`. Adding
  `tests/behaviour/test_catalogue_behaviour.py` to the second list does *not*
  exempt it, because Pylint's recursive walk descends from the named roots and
  `tests/` already pulls the whole tree into the strict pass. Evidence:
  `make lint` failed with `C0302: Too many lines in module (485/400)` on a file
  whose own root is in `PYLINT_TEST_TARGETS`. Impact: the executable-binding
  steps moved to `tests/behaviour/_catalogue_binding_support.py` (241 lines),
  leaving the scenario module at 331. A support module is not named `test_*`,
  so it also loses ruff's `assert` exemption; its assertions go through a
  `_require` helper that calls `pytest.fail`, matching
  `_telemetry_adapter_tracing_steps.py`.
- Observation: a plain `import` of a step support module is not enough to make
  its steps resolvable. Each decorator plants a pytest fixture in the
  *defining* module's locals, so the importing module must re-bind every step
  it wants. Re-binding with `when(name)(impl)` also silently drops
  `target_fixture`, which surfaces as `fixture 'refusal_outcome' not found`
  rather than as a missing step. Evidence: after the split,
  `pytest_bdd.exceptions.StepDefinitionNotFoundError` for both binding steps;
  after a naive re-bind, `fixture ... not found` for the two target fixtures.
  Impact: the re-bindings carry `target_fixture=` and use `parsers.parse` for
  parameterized names, following `tests/behaviour/test_telemetry_adapters.py`.
- Observation: the `ambrleaks` snapshot scanner has no notion of fixture data,
  so a deterministic fictional path in a snapshot trips `[snapshot-posix-path]`
  exactly as a real host path would. Evidence: 21 findings, one per
  `/opt/tools/echo` line added to `test_adapter_projection.ambr`. Impact: the
  value is allowlisted narrowly in `ambrleaks.toml` as `^/opt/tools/[^/]+$`
  rather than redacted in the snapshot, because the snapshot exists to pin the
  exact spelling each adapter publishes the path under and a placeholder would
  erode that. Verified non-vacuous: the pattern admits `/opt/tools/echo` and
  `/opt/tools/sccache` but rejects `/home/leynos/...`, `/etc/passwd`,
  `/opt/tools/`, and `/opt/tools/a/b`.
- Observation: thirteen of this plan's own Progress timestamps were in the
  future when checked, so they cannot have recorded observed events. Evidence:
  at 17:59Z the entries claimed times up to 23:24Z, while the commits that did
  the work were authored 15:31Z-17:56Z and `/tmp` log mtimes fall in the same
  window. The entries had been labelled `Z` (UTC) while carrying a time derived
  from a local-time reading, so they were uniformly ahead of reality by the
  machine's offset, and further drifted because they were written from memory
  rather than from an artefact. Impact: every Progress timestamp was re-derived
  from a primary source — the commit that produced the work, or the mtime of
  the gate log the entry cites — and then re-checked for both future-dating and
  ordering. **That repair did not hold:** a later check on 2026-10-02 found 31
  stamps still failing the future-dating test, including entries written hours
  after this observation was recorded, so the re-derivation was either partial
  or itself written from displayed values. See `Progress` for the method and
  the counts. The lesson generalizes to every plan in this repository: a
  timestamp is a claim like any other and needs a source; recording one from
  recollection produces a figure that is unfalsifiable until someone measures
  the wall clock against it. Note the local zone is CEST (UTC+2), so a
  `--date=format-local` reading is not UTC and must not be written with a `Z`.
- Observation: the defect recurred once more, on the entries recording this
  very lesson. Shell command output and gate logs print local time, so those
  readings were again two hours ahead of the truth when written with a `Z`; the
  CodeRabbit entry was written as `20:13Z` against an artefact that records
  `18:15Z`. Knowing the rule was not sufficient — the entries were transcribed
  from display values rather than converted. The durable fix is to convert at
  the point of writing, with `TZ=UTC stat -c %y` or
  `TZ=UTC git log --date=format-local`, and never to copy a displayed time into
  a `Z` field.
- Observation: a `coderabbit review --agent` pass can die mid-review and still
  look like a completed one. The 18:39Z pass emitted three well-formed
  `{"type":"finding"}` records and then a
  `{"type":"error","errorType": "connection"}` record, exiting 1 with no
  `review_completed` record. Treating the three findings as the review's
  verdict would have been wrong twice over: the review never finished, and the
  pass that *did* finish (18:45Z, over the same unedited tree) reported a
  different set — one finding, none of the three. Evidence: the two transcripts
  named above, compared record by record. Impact: a review's absence of a
  finding is not evidence a finding was fixed, and its presence is not evidence
  the rest were examined. Every finding is dispositioned against the tree with
  a reproducer, and the run is only accepted as complete when it exits 0 having
  recorded `review_completed`.
- Observation: two of the four findings were worse than the reviewer's wording
  implied, and one-of-the-four's cited location was not where the fix belonged.
  Finding B was described as "validate the returned value"; the actual
  consequence is that a resolver returning `None` sends the child to a
  *different executable* by way of `argv0`'s fallback. Finding C was cited
  against a call site rather than the definition, so a literal fix there would
  have left the other two callers unprotected. Evidence: the `argv0` fallback
  rule in `_pipeline_types.py` and the three `_resolve_executable_for` callers.
  Impact: a finding is a pointer to a place to look, not a specification of the
  fix. Confirm the stated mechanism, then check whether the real one is broader
  or the right repair is elsewhere.
- Observation: `python -m doctest cuprum/executable_binding.py` is not a valid
  check of this package, and its failure is an artefact of the harness rather
  than of the code. The runner splits the path, inserts the file's own
  directory on `sys.path`, and imports the file as a *top-level* module named
  `executable_binding`. The example's `cuprum.context` import loads the same
  file a second time under its real package name, so the error class reachable
  as `cuprum.executable_binding.ExecutableResolutionError` is a different class
  object from the one the example binds, and the `except` clause cannot match
  the raised instance. The traceback makes this look like a raising problem,
  because the escaped exception carries the right *name* while belonging to the
  wrong *class object*, which sends the reader after the example's source
  rather than the harness. Evidence: the same docstring, run as
  `doctest.testmod(cuprum.executable_binding)`, reports
  `attempted: 9 failed: 0`. Impact: run Python doctests package-qualified.
  Better, the repository has no Python doctest gate at all — `pyproject.toml`
  sets only `timeout = 30` with no `--doctest-modules`, and the sole `doctests`
  inputs in `.github/workflows/` belong to the Rust coverage job — so an
  example's correctness rests on review, and a broken one would sit unnoticed.
  Two modules already carry failures on `HEAD` under the naive harness
  (`cuprum/context/scoped.py` references an undefined `ECHO`;
  `cuprum/context/registration.py` fails twice), which is corroborating
  evidence that no such gate has ever run.
- Observation: a gate run whose tree changes mid-flight reports evidence for a
  revision that no longer exists, and the change is invisible to a
  status-code-only digest. Scrutineer captured `status_digest` before and after
  its lint attempt, saw the same value, and correctly refused to read that as
  "unchanged": a first run batching scrutineer's lint-2 with the repairs to the
  very files lint-2 had flagged. `git status --porcelain` still reported a
  modified-worktree status for both files, so the digest matched while the
  contents did not. Evidence: the `sha256sum` values scrutineer took at 20:55Z
  versus the ones recorded here. Impact: freeze a digest of file *contents*,
  not of status codes, before a gate run, and re-run any gate whose inputs
  moved.
- Observation: clearing the four lint defects was entirely mechanical once the
  real rule names were known, but two of the four repairs cost a round because
  the first guess was aimed at the wrong object. `DOC501`
  (`docstring-missing-exception`) attaches to the *raising function* — here
  `resolve_executable` — not to the exception class it names; the first
  `Raises` section was written on the class and did not satisfy the rule. And
  the `# ruff: ignore[blind-except]` suppression was not merely useless but
  itself an error: `try/except Specific/except Exception` is the shape ruff
  recommends, so `TRY`/`BLE` do not flag it and the suppression trips
  `RUF100 unused-noqa`. Evidence: the lint-2 transcript, and
  `All checks passed!` after both were addressed. Impact: prefer the minimal
  edit — removing the suppression — over writing a justification for it, and
  place a rule's fix where the rule is anchored rather than where the message
  points.
- Observation: `python-lint` chains Ruff to interrogate with `&&`, so Ruff's
  four findings had been **hiding** a second, unrelated gate — and the branch
  had therefore never passed `make lint` at all. Fixing Ruff let the run reach
  `interrogate --fail-under 100`, which reported 99.9% over the whole Python
  estate. The four missing docstrings were all on nested `resolver` functions
  inside test modules this branch adds, and none existed on `origin/main`
  (`git cat-file -e origin/main:<path>` fails for both files), so the shortfall
  was this branch's own. Evidence: interrogate's estate totals of 7533
  definitions with 4 missed, and `RESULT: PASSED ... actual: 100.0%` after the
  four one-line docstrings. Impact: a `&&` chain reports only the first
  failure, so a green Ruff is not evidence the lint target passed — read the
  gate's exit status, not its loudest sub-check. This is the same shape as the
  earlier "an aborting gate leaves later checks unobserved" lesson.
- Observation: interrogate counts docstrings on **nested** functions, not only
  module-level definitions, which is easy to miss because the surrounding test
  has one and the inner callable reads as an implementation detail. All four
  misses were inner `def resolver()` closures. Evidence: the four-site list from
  `interrogate --fail-under 0 -vv`, each rendering its qualname as
  `<test_name>.resolver`. Impact: add the one-line docstring when introducing a
  closure inside a test; the estate is at exactly 100%, so a single omission
  fails the gate for everyone.
- Observation: **a clean merge can silently drop a field, and the local tree
  cannot see it.** The branch's `resolved_path` reached every event through
  `_StageObservation.emit`. Meanwhile `main` gained `71aaf3eb`, which added
  `_LineEventEmitter` and gave it its own `ExecEvent(...)` call with an
  explicit field list — written when `resolved_path` did not exist, so the list
  could not name it. The two edits touch different functions, git merged them
  without a single conflict marker, and every local run passed. Only the merge
  ref failed: `stdout` and `stderr` events carried `resolved_path: None` while
  `plan`, `start`, and `exit` carried the bound path, so a consumer watching
  the stream would see the executable appear to change mid-run and then change
  back. Evidence: CI run at `a5c84fa1` on Python 3.12 and 3.14,
  `test_the_bound_path_reaches_every_event_of_the_execution` reporting
  `stdout: None`; `git merge-tree` on the two heads showed no conflict; merging
  `origin/main` locally reproduced the identical assertion failure. Impact:
  **PR CI builds the merge ref, not the branch head**, so a branch that is
  green locally and green in isolation can still be red, and a clean merge is
  no evidence that the merged result is correct. When two branches each add a
  field to, or a constructor of, the same data structure, merge the base into
  the branch and run the affected suite before trusting either.
- Observation: fixing that merge conflict required knowing *which* fields
  belong on a line event, and the file already answers it. `env_mode` is
  documented as invariant per stage observation and known before the spawn, so
  it is carried on every phase and must appear on line events too, or the mode
  would appear to change mid-stream. `resolved_path` has exactly that shape, so
  the fix was to give it the same treatment rather than to invent a rule.
  Evidence: the four remaining fields `_StageObservation.emit` sets and
  `emit_line` does not — the `_EventDetails` payload — are per-event detail
  that a line event correctly leaves at its defaults. Impact: when adding a
  field to a shared event, classify it by the invariant the file already
  states, and thread it through every construction site; a field that is fixed
  for the whole observation must be on the line events or the stream
  contradicts itself.
- Observation: **a green CI leg is not evidence the suite ran.** At `a5c84fa1`
  the Python 3.13 and 3.15a legs reported `success` while having run only the
  typechecker — 1470 log lines, zero occurrences of the failing test file. The
  3.12 and 3.14 legs ran the suite and failed. Evidence: the two "successful"
  logs contain no reference to `test_executable_binding_execution.py` at all.
  Impact: read a leg's log for the test file under scrutiny before citing its
  green as coverage of the suite; "no leg failed" and "every leg tested this"
  are different claims.
- Observation: the stale documentation anchor CodeRabbit reported was not the
  only one, and the audit that found the second one was itself nearly vacuous.
  Resolving every `docs/<file>.md#<fragment>` reference in the tree against the
  slugs its headings actually generate found two stale anchors: this issue's
  `#executable-bindings` (the heading is "Bind a catalogued program to a
  specific executable") and `#environment-policy-modes` in
  `cuprum/unittests/test_env_context_policies.py`, which arrived with #434/#466
  and is stale on `origin/main` today, so it is outside this issue's scope.
  Evidence: a first version of the audit printed "stale anchors: 0" because
  `rg -o` puts the line number in the field it treated as a path, so every
  reference was skipped; the corrected version reports
  `anchors resolved against real headings: 7; stale: 2`. Impact: a reference
  audit needs a count of what it *resolved*, not only of what it rejected, or a
  parser that reads nothing reports perfect health.
- Observation: **a three-stage gate target hides its later stages behind an
  early abort.** `check-fmt` runs `ruff format --check`, then
  `cargo fmt --all -- --check`, then `mdtablefix --check`. The round-3 rebase
  union joined `main`'s scoped-catalogue tests to this branch's
  executable-binding tests in `cuprum/unittests/test_context_isolation.py`
  leaving a single blank line before the first binding test, where the file's
  other seven definitions have two. `ruff format` wants two, so CI's
  `lint-test` job failed at its `Check formatting` step and never reached the
  third stage — where a second, independent defect was waiting: `mdtablefix`
  wanted a nine-line re-wrap of this very plan. Both were real; only the first
  was visible. Evidence: the failing job at `9e9b9315` reports
  `ruff format --check` exit 2 at `test_context_isolation.py:176:1`, and the
  pre-rebase head `6e4b8f7e` satisfies the same check because the union had not
  yet happened. Running the whole target locally, both stages were reported in
  one pass. Impact: verifying one stage is not verifying the target. Check
  whether a gate is composite before treating its exit code as coverage of the
  whole thing, and prefer running the target itself over the command one
  believes it wraps.
- Observation: **the 400-line module ceiling reaches behaviour tests through a
  path the exemption does not cover.** `pylint-classic` runs twice: a strict
  pass over `PYLINT_STRICT_TARGETS` and a second pass that disables
  `too-many-lines` for `PYLINT_TEST_TARGETS`. The strict pass names `tests`,
  and Pylint's `recursive = true` descends from it into `tests/behaviour`, so
  the relaxation at line 437 never reaches a file whose own root
  (`tests/behaviour`) is on the relaxed list. `cuprum/unittests/...` is
  unaffected because the strict pass names `cuprum/unittests` too, and an
  exact-path `filter-out` removes it. Evidence: `make lint` failed with
  `tests/behaviour/test_catalogue_behaviour.py:1:0: C0302: Too many lines in
  module (419/400)`,
  printed by the strict invocation
  `python -m pylint --jobs=1 benchmarks conftest.py cuprum scripts tests`.
  Impact: the merge that added `main`'s fifth scenario to this branch's two
  pushed the module 46 lines over the cap, and the de facto ceiling in
  `tests/behaviour/` is real rather than nominal: every other module there
  respects it, `test_context_hooks.py` sitting at exactly 400. The repair is a
  cohesive split, not a suppression.
- Observation: **a relative `ExecutionContext.cwd` double-anchored a relative
  binding, and no test could see it.** `resolve_binding` composes
  `Path(cwd) / resolved`, and when `cwd` is itself *relative* that composition
  stays relative. The spawn then hands the same relative `cwd` to the child,
  which resolves the relative `argv[0]` against the very directory it was
  already placed in — applying the prefix twice. `resolved_path` still reported
  the singly-anchored path, so the audit surface named the intended file while
  the child executed a different one. An absolute `cwd` composes absolutely, so
  every existing test was blind to it. Evidence: the RED regression test failed
  with
  `the child reported 'IMPOSTOR:srv/work/bin/tool.py\n' while
  'srv/work/bin/tool.py' was claimed` —
  the doubly-anchored spawn ran the decoy while `resolved_path` named the real
  script. Impact: two independent assessments raised this, and it is a genuine
  audit-surface divergence rather than a mere `FileNotFoundError`. The repair
  anchors once, in `_execution_cwd`, at both observation builders.
- Observation: the anchor belongs beside `_cwd_arg`, not inside it. `_cwd_arg`
  is pinned as a pure pass-through by
  `test_stage_stream_fds.py::test_cwd_arg_conversion`, including the `("", "")`
  row, and the spawn must keep receiving the caller's own spelling while only
  the *resolution base* is anchored. Evidence: that parametrization already
  asserts `_cwd_arg(Path("relative/dir")) == "relative/dir"`. Impact:
  `cuprum/_subprocess_context.py` gained `_execution_cwd` as a second,
  separately-tested helper rather than changing the existing one.
- Observation: **`""` as a working directory is unobservable, which is what
  makes the fall-through safe.** `Path("")` is `Path(".")`, so `_execution_cwd`
  answers an empty string with the process's own directory while `_cwd_arg`
  still renders `""`. Those two only fail to compose because `asyncio` refuses
  an empty `cwd` at the spawn, so the run raises `FileNotFoundError` before a
  child starts and no resolved path reaches a caller. Mapping `""` to `None`
  would have converted that loud failure into a silent run in a directory the
  caller never named. Evidence: an
  `asyncio.create_subprocess_exec(..., cwd="")` probe raises
  `FileNotFoundError: [Errno 2] No such file or directory: ''`, and the same
  happens through `SafeCmd.run_sync`. Impact: the empty string is left to fall
  through the ordinary arithmetic and is pinned at the helper level, where the
  behaviour is visible, rather than special-cased.
- Observation: **the resolver-error contract is split by boundary, and both
  guides documented only the inner half.** `resolve_binding` raises `TypeError`
  for a non-`str` resolver result, but `CuprumContext.resolve_executable`
  catches `except Exception` and re-raises `ExecutableResolutionError`. Every
  caller reaching a resolver through a command therefore sees the binding
  error, never the `TypeError` both guides named. Evidence: a probe calling
  `resolve_binding` directly reported
  `TypeError: ExecutableResolver must return str; got NoneType`, while the same
  resolver reached through `SafeCmd.run_sync` reported
  `ExecutableResolutionError: Program
  'boundary-tool' cannot resolve its executable: TypeError`
  with the `TypeError` chained as `__cause__`. Impact: `docs/users-guide.md`
  and `docs/v0-2-0-migration-guide.md` now distinguish the two boundaries, and
  a test pins the outer one because only it is reachable from a command.
- Observation: **the mislabelled-`Z` defect is still present and has grown
  since it was last measured.** The falsifiable check recorded above — a stamp
  cannot post-date the true UTC time of the commit that first introduced it —
  was re-run across the whole `Progress` section at 2026-10-10 16:38Z and found
  **21 of 72** `Z` stamps later than their introducing commit, by 12 minutes at
  the least and 237 at the most. The 2026-10-02 measurement found 31 and the
  repair rounds did not hold, so the figure has moved but the fault has not
  gone. Evidence: `git blame --line-porcelain HEAD -- <this plan>` maps every
  stamp line to its introducing commit, and each commit's own `%aI` converted
  to UTC is compared against the stamp; the four worst are `2026-10-10 17:40Z`
  written by `de410302` at `15:24Z`, `17:35Z` by the same commit, and two at
  `16:55Z` by `9e9b9315` at `15:00Z` — all roughly the CEST offset ahead, which
  is the tell that they were transcribed from a local-time reading. Two of the
  four post-date the wall clock itself at the moment of measurement. Impact:
  the stamps from this round were written only after cross-checking the system
  clock against GitHub's HTTP `Date` header, and the method generalizes — the
  independent source matters more than the reading, because the system clock is
  the very thing the error is measured against.

- Observation: **binding the failing stage of the existing fail-fast helper
  silently converted the run into a no-decision run, and only a probe showed
  it.** The helper's three stages all run the same interpreter program, so a
  binding for that program binds every stage. Each stage then runs the failing
  script, all three settle inside one `FIRST_COMPLETED` batch, and
  `should_terminate_others` finds no sibling left running — the run latches a
  failure index and emits nothing. Evidence: a probe of that arrangement
  printed ten events with no `pipeline_fail_fast` phase, just three `exit 3`
  events; the corrected arrangement, with the failing stage on its own program,
  prints the decision alongside stage 0's `plan`/`start`/`exit` and the
  siblings' `exit -15`. Impact: the bound case is built by
  `run_bound_failing_pipeline` rather than by a parameter on the shared helper,
  and the reason is recorded on that helper so a later reader does not
  "simplify" the two back together. The general lesson is that this helper's
  run is sensitive to stage *topology* and not only to stage count: a change
  that alters what any stage runs can quietly make the decision unreachable,
  and the tests that read its output would not report an error — they would
  report an empty decision and fail on a count, which is the harmless version
  of the same trap.

## Decision log

- Decision: bindings live on `CuprumContext`/`ScopeConfig`, not on
  `ProgramCatalogue` or on `SafeCmd`. Rationale: the issue asks for a specified
  *scope lifetime*. The context is the only existing mechanism with a defined
  lifetime, a `ContextVar`-backed isolation story, and a registration handle
  (`bind_executable`) that mirrors `env`/`allow`/`before`. A catalogue field
  would be global and immutable; a `SafeCmd` field would be per-command and
  unshareable. Date/Author: 2026-10-01, implementing agent.
- Decision: resolution happens once, in the stage-observation builder, and the
  resolved string travels on `_StageObservation`. Rationale: one resolution
  point per execution keeps a lazy resolver from being called twice, gives both
  spawn sites and every `CommandResult` construction site the same value, and
  keeps the allowlist gate ahead of it on every path. Date/Author: 2026-10-01,
  implementing agent.
- Decision: `resolved_path` is projected into the logging and tracing adapters
  but never into metrics. Rationale: the issue requires the executed path to
  stay inspectable, and both adapters already carry unbounded fields such as
  `argv`. Metrics labels are explicitly low-cardinality by documented policy.
  Date/Author: 2026-10-01, implementing agent.
- Decision: `_resolve_executable_for` lives in `cuprum/_observability.py`
  rather than in `cuprum/_context_policy.py`, and `_context_policy` keeps only
  the two policy reads. Rationale: the split is by kind, not by caller. The
  allowlist gate and the hook bundle are decisions the context makes; the
  executable is an input the observation carries, and its siblings
  (`_resolve_env_overlay`, `_base_stage_tags`) already live in
  `_observability`. The move also took `_pipeline_internals.py` back under the
  400-line cap without a second extraction. Date/Author: 2026-10-01,
  implementing agent.
- Decision: `CommandResult.resolved_path` is declared before
  `relay_fallbacks`, both keyword-only. Rationale: `test_public_api` pins
  `relay_fallbacks` as the last field of both the positional projection and
  `dc.fields()`. Declaring the new field ahead of it satisfies both without
  loosening that test; the alternative — relaxing the declaration-tail
  assertion — would weaken a contract test to accommodate an implementation
  detail. Date/Author: 2026-10-01, implementing agent.
- Decision: the plan is executed without a separate approval round trip.
  Rationale: the task packet that commissioned this plan already directs
  implementation, gates, and review; the ExecPlan is the recorded contract for
  that work rather than a separate decision to be approved. Date/Author:
  2026-10-01, implementing agent.
- Decision: split the value types across `cuprum/executable_paths.py` and
  `cuprum/executable_binding.py` rather than trimming the single module.
  Rationale: the tolerance says to extract rather than trim when a module would
  exceed 400 lines. The split is also a genuine responsibility boundary — "is
  this string an acceptable path?" is answerable without knowing anything about
  bindings — and it lets the rejection enum and the error type sit together.
  The error type moved into the paths module so the dependency runs one way.
  Date/Author: 2026-10-01, implementing agent.
- Decision: `advisory_path_rejection` requires a regular file, not just an
  execute bit. Rationale: directories carry an execute bit, so the bit alone
  would report a directory as a usable executable, which is never what a caller
  binding an executable means. `NOT_EXECUTABLE` is a rejection category, not a
  claim that the path is a file. Date/Author: 2026-10-01, implementing agent.
- Decision: `executable_path` raises `InvalidExecutableBindingError` with
  `program=None` rather than a plain `ValueError`. Rationale: one error type
  with one message shape is easier to document and catch than two, and the
  optional `program` is what distinguishes standalone path validation from
  binding construction. `executable_path` remains a subtype of `ValueError`, so
  callers written against the sibling `safe_path` contract keep working.
  Date/Author: 2026-10-01, implementing agent.
- Decision: extract `_registration_base.py` and `_env_registration.py` in
  addition to the planned `_executable.py` and `_hooks.py`. Rationale: the
  planned extractions alone brought `cuprum/context/core.py` from 409 to 314
  lines but left `cuprum/context/registration.py` at 469, over pylint's
  400-line ceiling. Extracting the `env` handle alone produced a cycle, because
  `_env_registration` needed `_TokenRegistration` from the module that imports
  it; moving the base out as well breaks the cycle by giving both modules a
  leaf dependency. `registration` re-exports `EnvRegistration`, `env`, and
  `_TokenRegistration`, so every existing import path resolves to the same
  object. Date/Author: 2026-10-01, implementing agent.
- Decision: `executable_binding` discriminates on
  `isinstance(value, (str, Path))` rather than `callable(value)`. Rationale:
  the negative branch of `callable` leaves the argument union un-narrowed, so
  `ty` rejected the resolver assignment outright. Branching on the path types
  narrows cleanly and states the intended contract — a resolver is anything
  that is not a path — without a cast or a `TypeIs` guard, which the 3.12 floor
  rules out anyway. Date/Author: 2026-10-01, implementing agent.
- Decision: repair two pre-existing interrogate misses in
  `scripts/tests/test_boundary_*.py`, which are byte-identical to
  `origin/main`. Rationale: `make lint` failed at 99.9% against a 100.0% floor.
  Fixing only the three misses this branch introduced lands at 99.9865%, which
  rounds to 100.0% and passes — but that leaves the gate depending on
  interrogate's one-decimal rounding of main's existing debt rather than on the
  tree being covered. Fixing all five removes that dependency. Date/Author:
  2026-10-01, implementing agent.
- Decision: resolve the CI failure by merging `origin/main` into the branch and
  threading `resolved_path` through `_LineEventEmitter`, rather than by
  relaxing the assertion that caught it. Rationale: the failing assertion is
  the contract the issue asks for — "the executed path is inspectable" — and it
  was doing its job, catching a field that vanished in the merge. Weakening it
  to accommodate `stdout: None` would have deleted the only test that notices a
  line event disagreeing with the events around it about which executable ran.
  Date/Author: 2026-10-02, implementing agent.
- Decision: the feature scenario binds "the running interpreter" rather than a
  literal path, and its step no longer takes a `path` parameter. Rationale: the
  two CodeRabbit findings on this step contradicted each other — one asked the
  step to bind the configured path, the other asked the wording to admit it
  binds the interpreter — and taking them as a pair exposed the defect they
  were both circling: the feature text named a path the step never read. The
  suite has to run on any machine, so the executable must be one guaranteed to
  exist; naming it in the step text and deleting the unused parameter removes
  the disagreement instead of documenting it. Date/Author: 2026-10-02,
  implementing agent.
- Decision: the refusal scenario's counting resolver records the path it was
  asked for and its step fails immediately if the run unexpectedly succeeds,
  rather than relying on the later assertion to notice a missing error.
  Rationale: recording `len(calls)` made the list a counter wearing a list's
  type, and a resolver that ran but did not execute — the case the ordering
  claim actually probes — would have been invisible. Failing at the call site
  keeps the diagnostic next to the run that should not have happened. The new
  branch was probed rather than assumed: driving the step with the unlisted
  program also curated makes the allowlist admit it, and the step then raises
  the intended `pytest.fail` message, so the branch is reachable and the check
  is not vacuous. Date/Author: 2026-10-02, implementing agent.
- Decision: leave `#environment-policy-modes` in
  `cuprum/unittests/test_env_context_policies.py` alone. Rationale: the anchor
  is stale — the heading it means is "Choose how a child environment is
  composed" — but it arrived with #434/#466 and is stale on `origin/main`, so
  fixing it here would widen an issue about executable bindings into an
  unrelated one. It is recorded under Surprises so the next reader inherits the
  finding without inheriting an unrequested diff. Date/Author: 2026-10-02,
  implementing agent.
- Decision: the requested rebase onto `origin/main` is a no-op, so the branch
  history is left alone rather than replayed. Rationale: `origin/main`
  (`b6bb9a99`) is already an *ancestor* of `0ee8ba47`, so the rebase the
  request describes has, in effect, already happened — the branch absorbed main
  by merge at `82acb1ae` ("Merge remote-tracking branch 'origin/main' into
  issue-440-…"), which makes `git merge-base HEAD origin/main` equal to
  `origin/main` exactly. Three independent sources agree the tips are
  `b6bb9a99` and `0ee8ba47`: the local tracking refs, `git ls-remote origin`,
  and the GitHub REST API for both `refs/heads/main` and `refs/heads/<branch>`.
  Replaying the 35 non-merge commits with
  `rebase --no-fork-point --reapply-cherry-picks` would have produced new SHAs
  and destroyed the merge, changing the candidate head for no gain and
  invalidating the CI evidence and the zero-finding CodeRabbit result that the
  status line above relies on. The claim was verified rather than assumed:
  `git merge-tree --write-tree --name-only origin/main HEAD` exits 0 and its
  tree hash (`8d1ba226ac4cc7b8d28593f56c74d116ed1fd415`) is byte-identical to
  `git rev-parse HEAD^{tree}`, which is proof that merging main in would change
  nothing. Date/Author: 2026-10-02, implementing agent.
- Decision: the CodeScene "Code Health Review" failure is not a regression from
  this rebase and is not repaired here. Rationale: it has failed on every one
  of the branch's recent heads (`59bf841f`, `3e893565`, `6efb42b9`, `c738c78a`,
  `72f0e660`, `0ee8ba47`), so it is a standing property of the branch rather
  than anything a rebase could have introduced. The failed rule is
  `code duplication` on `cuprum/context/_hooks.py`, a 137-line file this branch
  *adds* by extracting six hook mutators out of `cuprum/context/core.py` so
  that module stays under the repository's 400-line ceiling; CodeScene scores
  the resulting near-identical `with_…_hook`/`without_…_hook` accessor bodies
  at 9.10 against its 10.00 threshold. Repairing it is a genuine design
  question — whether the six accessors should collapse into a table-driven pair
  — and is out of scope for an instruction to rebase. It is recorded here so
  the reader is not misled by the status line's "every gate passes", which
  refers to the repository's own commit gates and not to the PR's external
  quality checks. CodeScene is also not one of `main`'s required status checks:
  ruleset `main-required-checks` (id `18427980`, `enforcement: active`) lists
  twelve contexts — `lint-test`, `Typecheck and test (Python 3.12)`,
  `Typecheck and test (Python 3.14)`, `coverage`, `benchmark-ratchet`,
  `build-wheels / …` (five targets plus `verify-wheel-install`), and
  `Extension-gated tests (Python/Rust boundary)` — and CodeScene is not among
  them. All twelve pass at `0ee8ba47`, so the failure does not gate the merge.
  Date/Author: 2026-10-02, implementing agent.
- Decision: a relative execution working directory is anchored once, in a new
  `_execution_cwd` helper beside `_cwd_arg`, rather than by changing `_cwd_arg`
  or by making the spawn absolute. Rationale: the two consumers want different
  things from the same value, and conflating them is what produced the defect.
  The *resolution base* must be absolute so a relative binding composes to a
  path the child cannot re-anchor; the *spawn argument* must stay exactly what
  the caller spelled, which `test_stage_stream_fds.py::test_cwd_arg_conversion`
  already pins, including its `("", "")` row. Anchoring in the spawn instead
  would have changed what the child receives and broken that contract;
  anchoring inside `_cwd_arg` would have coupled the two. Placing the helper in
  `cuprum/_subprocess_context.py` follows the module's own docstring, which
  already names that module as the shared home for both spawn sites'
  working-directory handling, and it makes the helper reachable by the same
  exhaustive finite-domain test style the module's other helper uses.
  Date/Author: 2026-10-10, implementing agent.
- Decision: `""` falls through `_execution_cwd`'s ordinary arithmetic rather
  than being mapped to `None`. Rationale: `None` means *the caller named no
  directory*, and the caller who passes `""` did name one — an empty one.
  Returning `None` would silently substitute the process's own directory for a
  directory the caller asked for, upgrading a loud spawn-time
  `FileNotFoundError` into a run somewhere the caller never intended. The
  fall-through keeps the failure loud and is unobservable in any run
  regardless, because `asyncio` refuses an empty `cwd` before a child starts,
  so there is no behaviour to protect by special-casing it. The choice is
  pinned by a test row at the helper level, where it is the only place the
  decision is visible. Date/Author: 2026-10-10, implementing agent.
- Decision: the Progress stamps this work added are written from a clock
  reading verified against an independent source, and the four stamps already
  committed that post-date their own commits are left as written. Rationale:
  the plan's earlier decision to leave mislabelled stamps alone applies here
  and is the right call for the same reason — restamping destroys the evidence
  of how the entries were produced, and a reader already has a stronger source
  in the introducing commit. What this entry adds is the measurement: at
  2026-10-10 the check was re-run across the whole `Progress` section and found
  **21 of 72** `Z` stamps later than the commit that introduced them, by 12
  minutes at the least and 237 at the most, uniformly by roughly the CEST
  offset. So the defect did not merely persist; it grew. Every stamp in this
  round's own entries was therefore read from a clock cross-checked against
  GitHub's HTTP `Date` header and against the commit list before being written,
  rather than transcribed from displayed local time. Date/Author: 2026-10-10,
  implementing agent.
- Decision: the bound fail-fast case gets its own support helper
  (`run_bound_failing_pipeline`) rather than an optional parameter on the shared
  `run_failing_pipeline`, and the failing stage gets a program of its own.
  Rationale: an optional binding parameter on the shared helper is the tidier
  API and is wrong, because the binding would capture all three stages and make
  the decision unreachable. The two runs differ in topology, not just in a
  flag, and encoding that as a parameter would present a knob that cannot be
  turned — the parameter would have to be documented as "supply this and the
  run stops producing the event you came for". Two helpers that each state
  their own arrangement say that plainly. Date/Author: 2026-10-10, implementing
  agent.

- Decision: the relative-path resolution rule is verified by two properties
  asserting opposite claims, not by one property whose `cwd` strategy draws
  both spellings. Rationale: the contract *is* opposite in the two cases — a
  relative binding under an absolute directory resolves absolute, under a
  relative directory it stays relative. A single property over `_CWD` could
  express both by branching on the spelling the generator drew, so the split is
  not forced by inexpressibility; it is preferred because it gives each domain
  its own run, and an assertion inside a branch is not exercised by a run that
  never draws that branch. Splitting therefore keeps each claim falsifiable
  alone; the mutation evidence is that a helper anchoring its own directory
  fails the relative-directory property and passes the absolute-directory one.
  The rejected alternative was widening `_CWD`, which I had already done and
  reported as relative-binding coverage; it extends only the absolute-binding
  properties, and in them a relative directory would violate the assertion
  rather than exercise the defect. Date/Author: 2026-10-10, implementing agent.
- Decision: the bound fail-fast module exposes the failing script through a
  sibling module-scoped fixture, and the lifecycle assertion compares by
  equality rather than by filename suffix. Rationale: the assertion's claim is
  that each event names *the executable the stage was bound to*, and a suffix
  accepts any file of that basename anywhere under the doubled prefix — the
  precise divergence the assertion exists to catch. The alternative of
  returning a tuple from the events fixture was rejected because it would
  change three tests' signatures to serve one, and returning a tuple from the
  script writer was rejected because it couples the writer to the reader's
  needs. A sibling fixture consumed by the events fixture states the dependency
  in the direction it actually runs: the script is written first and both
  describe the same run. Date/Author: 2026-10-10, implementing agent.

- Decision: the three wording corrections the proof reply requested are applied
  at all sites carrying the disproved claim, not only the two it named.
  Rationale: the reply asked to replace the `sys.executable` descriptions "in
  O3 and O5", but the same false construction appears in EP-M3's acceptance
  line and in acceptance clause 2 as well, so fixing only the named sites would
  leave the claim standing where it was equally wrong. A disproved premise is
  disproved wherever it appears. The same reasoning applies to the combined-
  property argument: it is corrected in both the docstring and O5 rather than
  in whichever one the review happened to cite. Date/Author: 2026-10-10,
  implementing agent.
- Decision: the corrections are applied rather than deferred, even though they
  move the head after both assessments accepted `92ab8f17`. Rationale: "apply
  the small wording corrections" is a request, and a request is a requirement
  however small; the alternative — merging at a head known to contain text the
  reviewer had just asked to correct — trades a bounded extra verification
  cycle for a delivered document that states something false about its own
  evidence. The corrections are prose and docstrings only, so the production
  and test behaviour the assessments accepted is unchanged, but the tree is not
  identical and therefore needs its own gate run and its own hosted CI.
  Date/Author: 2026-10-10, implementing agent.
- Decision: the clause-2 replacement text was itself corrected rather than
  defended, because the reviewer's finding held against the assertion.
  Rationale: my replacement said an ignored binding "would still hold" the
  assertion "because both report *some* path", which is weaker than what clause
  2 actually requires — exact equality with the bound path, so a decoy run
  reports its own different path and fails. A reviewer's counter-claim deserves
  the same evidence standard as the original text, so the assertions were read
  before the sentence was rewritten, and the rewritten sentence now names the
  exact comparison as the discriminator instead of crediting the marker alone.
  The named witnesses and all assertions were kept, per the reply's explicit
  instruction that this is prose, not a production or test gap. Date/Author:
  2026-10-10, implementing agent.

## Outcomes & retrospective

Filled in at EP-M5, comparing the shipped surface against the issue's
acceptance list.

The two authorities are now separate in code and not merely in prose. The
allowlist from `ProgramCatalogue` still decides which logical programs may run;
a scoped `ExecutableBinding` decides which executable a permitted program runs.
`resolve_executable` takes no part in `is_allowed`/`check_allowed`, and the
refusal path is tested to resolve nothing at all, so a binding cannot widen the
allowlist even accidentally. The issue's rejected workaround — accepting any
executable with a matching basename — is absent: the basename plays no part in
any decision, and the execution suite is arranged so a basename-based fallback
would *succeed* rather than error, which means the passing test can only be
explained by the binding having been honoured.

Each acceptance clause is discharged by a named test or a named documentation
section. An unapproved path is refused before its resolver runs
(`test_executable_binding_execution.py`, the refusal case, asserting a recorded
call count of zero, plus the `catalogue.feature` scenario whose counting
resolver must never fire). A configured approved path runs exactly, witnessed
from both sides — the library's `resolved_path` and the child's own report of
its `sys.argv[0]` — because checking only the former could not distinguish the
two. Nested and concurrent bindings stay isolated, covered by the stateful
property test and the context-isolation suite. The filesystem-replacement limit
is documented rather than papered over: `docs/users-guide.md` and section 5.1.2
of `docs/cuprum-design.md` state that a path binding narrows the window between
validation and `exec` without closing it, so it is not an immutable binary
identity.

The retrospective lesson is the one recorded under Surprises: the plan's own
Progress timestamps were the least trustworthy artefact in it. Every other
number in this document was derived from a command's output or a commit, while
the timestamps were written from recollection and were wrong by hours. A living
document that records progress is only as good as the provenance of what it
records. The correction belongs in that lesson too: documenting the fault and
re-deriving the values did not end it. A final check at `901794f0` found 31
stamps still ahead of the commits that carry them, so the durable remedy is not
a one-time repair but reading the introducing commit instead of the label, and
converting at the point of writing rather than transcribing a displayed time.

## Context and orientation

Cuprum is a typed Python command runner. The pieces this change touches are:

- `cuprum/program.py` defines `Program = typ.NewType("Program", str)`, the
  nominal identity. It is not modified.
- `cuprum/catalogue.py` defines `ProgramCatalogue`, the curated allowlist of
  `Program` values grouped into `ProjectSettings`. It is not modified.
- `cuprum/context/` is the scoped-policy package (`ADR-006`). `core.py` holds
  the immutable `CuprumContext` dataclass and its `narrow`, `with_*` methods;
  `_scope.py` holds `ScopeConfig` and the context errors; `_policy.py` holds
  pure narrowing helpers; `env_overlay.py` holds the `EnvMode`/`EnvOverlay`
  model and `merge_env_overlays`; `registration.py` holds `_TokenRegistration`
  and the public factories `allow`, `before`, `after`, `observe`, `env`;
  `state.py` owns the `ContextVar`.
- `cuprum/_command_internals.py` prepares a single command's
  `_StageObservation` and bundles a `_SubprocessExecution`.
- `cuprum/_pipeline_internals.py` holds `_enforce_allowlist` and
  `_build_pipeline_observations`, which builds one `_StageObservation` per
  pipeline stage.
- `cuprum/_subprocess_execution.py` and `cuprum/_pipeline_spawn.py` are the
  two sites that construct a child's argument vector.
- `cuprum/_pipeline_types.py` defines `_StageObservation`, whose `emit` builds
  every `ExecEvent` for both paths.
- `cuprum/events.py` defines `ExecEvent`, the public observe-event record.
- `cuprum/adapters/_support.py` projects an `ExecEvent` into the logging and
  tracing adapters' key/value pairs.
- `cuprum/sh/results.py` defines `CommandResult` and `PipelineResult`.
- `cuprum/sh/safe_cmd.py` defines `SafeCmd` (with the `argv_with_program`
  property) and `Pipeline`.

Terms used below. *Logical program* means the `Program` value used as catalogue
identity and allowlist subject. *Binding* means the association from a logical
program to the string that should actually be executed. *Resolution* means
evaluating a binding to that string for one execution. *Resolver* means a
zero-argument callable returning a path string, evaluated at resolution time.

## Conformance basis

No upstream Terms of Reference or technical design revision covers this change.
The governing artefacts are the issue itself, `AGENTS.md`, and the existing
accepted decisions it must not contradict:

- `docs/adr-006-context-package-split.md` — the `cuprum/context/` package
  layout this change extends.
- `docs/adr-007-subprocess-execution-module-boundaries.md` — the private
  subprocess module boundaries the spawn edits live inside.
- `docs/adr-018-typed-environment-policies.md` — the nearest precedent for a
  typed, scoped policy carried on `CuprumContext` and projected into telemetry.
  This change deliberately mirrors its shape.

Trace. The rightmost column is a test *concern*, not a module path: this
repository has no `tests::` namespace, so each name below groups the artefacts
that discharge the requirement rather than naming a file. The concrete
artefacts behind each group are listed in `Verification plan`.

```plaintext
issue-440 (bindings + validation)      -> EP-M1 -> tests::executable_binding
                                                   [test_executable_binding{,_property_based}.py,
                                                    test_executable_paths.py]
issue-440 (scope lifetime + isolation) -> EP-M2 -> tests::context::executable_bindings
                                                   [test_context_isolation.py,
                                                    test_token_registration_stateful.py]
issue-440 (exact execution + telemetry)-> EP-M3 -> tests::execution::resolved_path
                                                   [test_executable_binding_execution.py,
                                                    test_structured_events.py]
issue-440 (acceptance + docs)          -> EP-M4 -> tests::behaviour::catalogue,
                                                   docs::cuprum-design §5.1.2
                                                   [features/catalogue.feature,
                                                    docs/users-guide.md]
```

## Verification plan

The change introduces five non-trivial invariants. Each is stated with the
method chosen for it, the artefact that carries it, and the evidence that
discharges it. A passing result is only meaningful if the check can fail, so
each obligation carries a non-vacuity argument.

O1 — Classification totality and first-match order.

- Statement: for every `str` input and every `allow_relative`,
  `classify_executable_path` returns either `None` or exactly one
  `PathBindingRejection`, and when it returns a rejection that member's `value`
  is the message `executable_path` raises for the same input.
- Method: property test over generated strings, plus a parameterized table for
  the boundaries that a generator reaches only by luck.
- Rationale: the classifier is a pure total function over an infinite domain,
  which is exactly the shape property testing covers; the table pins the
  documented check order that a generator cannot assert.
- Artefact: `cuprum/unittests/test_executable_binding_property_based.py`,
  `cuprum/unittests/test_executable_paths.py`,
  `cuprum/unittests/test_executable_binding.py`.
- Evidence: `make test-python` passes; the construction round trip is asserted
  with `pytest.raises(InvalidExecutableBindingError)` and the raised `reason`
  compared to the classifier's answer.
- Non-vacuity: the generator's alphabet includes NUL, both path separators, and
  the parent-segment dot, and dedicated properties pin `EMPTY`, `NUL`,
  `PARENT_SEGMENT`, and `NOT_ABSOLUTE` individually. The two
  filesystem-dependent members are pinned by a witness table in
  `test_executable_binding.py` that asserts coverage of the *entire* enum, so a
  newly added member fails that guard until it is given a witness. A mutation
  that reordered the NUL check after the absolute check would be caught by
  `test_nul_check_precedes_the_parent_segment_check` and by
  `test_nul_pins_the_nul_category`.
- Scope of the generated domain: `_FUZZ_TEXT` draws from a fixed 9-character
  alphabet with `max_size=12`, so the sampled domain is finite while the stated
  domain — every `str` — is infinite. The property therefore constitutes
  sampled evidence for the universal claim, not a proof of it: the evidence
  covers sampled inputs drawn from the alphabet and the deterministic boundary
  cases the named table pins, and no further. `_CWD` likewise draws `None`, an
  absolute path, and a relative path rather than every string a caller might
  pass.
- Platform scope of the filesystem witnesses: on Windows,
  `advisory_path_rejection` returns `None` by design, because the execute bit
  is not part of a file's identity there and Cuprum will not guess at ACL-based
  executability. The `NOT_FOUND` and `NOT_EXECUTABLE` rows of the witness table
  — and the whole-enum coverage guard that depends on them — are therefore
  POSIX expectations. Windows CI does not run these modules:
  `EXTENSION_TEST_TARGETS` excludes the executable-binding test files, so the
  Windows job is narrowly scoped to the Python/Rust boundary and these rows
  never execute there. The scope is recorded rather than enforced with a
  platform skip, because a skip would let the rows silently disappear on the
  platform where they are meaningful.

O2 — Binding isolation across nested scopes, threads, and tasks.

- Statement: a binding installed in one scope is visible inside that scope and
  absent outside it; a child scope overrides a parent binding for the same
  program and leaves the parent's other bindings intact; two threads, and two
  asyncio tasks, never observe each other's bindings.
- Method: named pytest examples for the nested case; **sampled** state-machine
  testing for the register/detach sequences — Hypothesis drives the machine
  with generated rules, so this is sampling, not exhaustive model checking, and
  the guard below is correspondingly weaker; named examples with a
  `threading.Barrier` and with `asyncio.gather` for the concurrency cases.
- Rationale: the nesting behaviour is a small finite set of transitions that
  the existing Hypothesis state machine already models for every other handle
  type, so extending it is cheaper and stronger than enumerating cases by hand;
  the concurrency claims need real threads and real tasks, which a model cannot
  supply.
- Artefact: `cuprum/unittests/test_token_registration_stateful.py` (extended
  `_FACTORIES`), `cuprum/unittests/test_context_isolation.py`,
  `cuprum/unittests/test_executable_context.py`.
- Evidence: `make test-python` passes; the state machine's
  `active_context_matches_stack_top` invariant holds across generated sequences
  that include the new `bind` factory.
- Non-vacuity: the state machine's `_FACTORIES` tuple gains `bind`,
  `bind-nested`, and `bind-two` entries, and a check asserts that at least one
  generated sequence actually installed a binding (a generator that never
  samples the new entry would otherwise pass vacuously). What that guard
  establishes is at least one binding factory ran, not that all three did, and
  not that same-key override and distinct-key merge both occurred; those are
  sampled. The guard depends on generated coverage and can therefore fail on a
  valid run that happens to sample no recorded binding; making it deterministic
  would need required transitions rather than generated rules, which is a
  change to the machine's contract and not one this work makes. The machine's
  own invariant is object identity of the restored context —
  `active_context_matches_stack_top` compares with `is` — and it does not
  independently model binding *contents*: its expected context comes from
  `current_context()` after production registration, so a registration that
  installed a wrong mapping would still satisfy it. Binding-content evidence
  comes from the named tests in `test_context_isolation.py`, notably
  `test_a_child_scope_binding_leaves_sibling_bindings_intact`, which checks
  both programs before, during, and after an override. The concurrency examples
  deliberately assert the *other* context's binding is absent, so a test that
  leaked a binding would fail rather than pass.

O3 — Resolution ordering: enforcement precedes resolution.

- Statement: for a command whose program is not allowlisted, execution raises
  `ForbiddenProgramError` and the binding's resolver is never invoked; for an
  allowlisted program with a binding, the spawned `argv[0]` is exactly the
  resolved string.
- Method: named pytest example with a resolver that records its calls, on both
  the direct and the pipeline path.
- Rationale: the property is about a call ordering between two collaborating
  functions, which a counting side effect observes directly and precisely.
- Artefact: `cuprum/unittests/test_executable_binding_execution.py`.
- Evidence: `make test-python` passes; the denial test asserts
  `calls == []`; the happy-path test asserts `result.resolved_path` is the
  resolver's value and `len(calls) == 1`. The ordering claim is separate from
  the identity claim: what the child thinks it ran is the script's own
  `sys.argv[0]` report, asserted in
  `test_the_child_receives_the_bound_path_as_its_own_argv0`, and a bound run's
  logical identity is asserted per lifecycle event as `event.program == tool`.
- Non-vacuity: the control is a pair of tests, not one body.
  `test_a_resolver_runs_once_per_execution_not_once_per_event` asserts
  `len(calls) == 1` for the allowlisted run, proving the counter works;
  `test_a_bound_but_unlisted_program_is_refused_without_resolving` then asserts
  `len(calls) == 0`, proving the ordering. Without the positive case the empty
  list would be indistinguishable from a broken counter. The pipeline path
  carries its own pair, because ordering inside one stage's observation is not
  the same property as ordering *across* stages:
  `test_a_pipeline_refused_at_a_later_stage_resolves_no_stage` binds an allowed
  first stage and a forbidden later one with a counting resolver on each and
  asserts `ForbiddenProgramError` with **both** counters at zero. A mutation
  probe that interleaved enforcement with resolution made it fail with
  `the permitted stage's resolver must not run for a refused pipeline, ran 1
  times`,
  which is precisely the interleaving it exists to catch — the direct refusal
  test cannot detect that pipeline-specific regression.
  `test_a_permitted_pipeline_resolves_each_stage_exactly_once` supplies the
  positive control for the pipeline's resolvers: the same pipeline shape with
  every stage permitted, each stage bound to a counting resolver, asserting
  `len(calls) == 1` per stage and each stage's reported path. It is the direct
  counterpart to the refusal test — the two differ only in whether the later
  stage is allowlisted — so a pipeline-wide resolver regression must fail one
  of them whichever direction it errs in. A mutation that evaluated each
  resolver twice per resolution failed it with
  `the producing stage must resolve exactly once, ran 2 times`, an assertion
  the refusal test cannot reach.

O4 — Telemetry projection: identity preserved, path added, metrics untouched.

- Statement: every `ExecEvent` for an execution whose binding resolved carries
  `resolved_path` set to that string and leaves `program` as the logical
  `Program`; the sanitized `pipeline_fail_fast` event carries no
  `resolved_path`; the logging adapter emits `cuprum_resolved_path`, the
  tracing adapter emits `cuprum.resolved_path`, and the metrics label mapping
  stays exactly `{"program", "project"}`.
- Method: two distinct methods, split by what each can actually observe.
  Named pytest examples *driving a real subprocess* for the execution surface
  (`test_executable_binding_execution.py`, `test_bound_fail_fast_event.py`),
  and named pytest examples over *constructed* `ExecEvent` values for the
  adapter projections (`test_adapter_projection.py`). The adapter tests do not
  attach an adapter to a live child; an earlier revision of this line claimed
  they did, which the proof assessment correctly rejected. What the adapter
  module pins is the projection of a record shape, and a constructed record is
  the faithful input for that.
- Rationale: this is a finite set of surface-by-surface assertions over a
  record shape, which examples express more readably than a generator. The
  split follows the boundary being tested rather than convenience: an adapter
  reads a record and writes a projection, so a constructed record exercises the
  projection completely; a spawn chooses the executable, so only a real child
  can witness that.
- Artefact: `cuprum/unittests/test_executable_binding_execution.py` (the
  per-phase event assertions driven by a real spawn),
  `cuprum/unittests/test_bound_fail_fast_event.py` (the *bound* fail-fast case,
  also driven by a real spawn), and
  `cuprum/unittests/test_adapter_projection.py` (the adapter surface, over
  constructed records).
- Evidence: `make test-python` passes; the metrics test asserts the label set
  equals `{"program", "project"}` and would fail if a path were added.
- Non-vacuity: two tests carry the unbound case.
  `test_an_unbound_program_runs_under_its_catalogued_name` asserts
  `resolved_path is None`, and `test_the_unbound_case_emits_no_path_key_at_all`
  asserts no `cuprum_resolved_path` key is present, so an adapter that
  unconditionally emitted the key would fail. The bound fail-fast case was
  missing at the assessed head, and no existing test covered it: every
  fail-fast run in the suite binds nothing, so a regression copying the failing
  stage's path onto the sanitized decision event passed them all. The new
  module binds the failing stage, which requires giving it a program of its own
  — binding the shared interpreter program binds all three stages, and a probe
  confirmed those three then settle in one batch and emit no decision event at
  all. Three mutation probes were run against it, and their verdicts differ in
  a way worth recording rather than flattening. Leaking `resolved_path` onto
  `emit_fail_fast` failed `test_the_decision_event_withholds_the_bound_path` on
  its own assertion, and dropping it from `emit` failed
  `test_a_bound_failing_stage_reports_its_path_on_its_own_events` likewise —
  both are true assertion failures, which is the evidence that these tests
  discriminate on what they claim to. Making `argv0` ignore the binding is
  detected differently: the fixture errors with
  `FileNotFoundError: 'bound-failing-stage'`, because the child cannot start at
  all. That is a genuine detection, but it is not an assertion doing the work,
  and it is recorded as the weaker of the two kinds.

  A fourth probe was run after the proof assessment observed that this module's
  lifecycle assertion accepted any path *ending* in `failing.py`. The assertion
  is now equality against the path the module wrote, and the probe corrupts
  only the *reported* path — the spawn still runs the bound file — by rewriting
  it to a sibling directory under the same basename. It failed
  `test_a_bound_failing_stage_reports_its_path_on_its_own_events` on its own
  assertion, reporting `.../bound0/elsewhere/failing.py` where
  `.../bound0/failing.py` was expected. The value it produced still ends in
  `failing.py`, so the suffix form this replaced would have accepted it: that
  is the concrete demonstration that the weaker assertion could not have
  detected this class of divergence, which is what the assessment asked be
  strengthened. Restricting the corruption to the reported path matters — a
  probe that also redirects the spawn dies with `FileNotFoundError` instead,
  which is the weaker detection kind recorded above and would not have shown
  the assertion doing the work.

O5 — Resolver invocation count and relative-path resolution.

- Statement: `resolve_binding` calls a resolver at most once per *invocation*
  and returns its result verbatim; an absolute binding is returned unchanged
  whatever the working directory, and any other binding — a relative path or a
  bare name alike — is anchored at the supplied `cwd`, or returned unchanged
  when there is no `cwd` so the platform resolves it. The separator distinction
  belongs to `advisory_path_rejection`, which skips its filesystem probe for a
  bare name because Cuprum does not replicate the platform's `PATH` search; it
  is not part of the resolution rule. The helper guarantees one resolver call
  per *helper* invocation only; the once-per-stage execution guarantee is
  supplied by the observation preparation (`_prepare_execution_observation` and
  `_build_pipeline_observations`), which each resolve once and reuse the
  result. The end-to-end counter measures the composed property, and the
  helper-level counter measures the helper's own.
- Method: property test over generated binding/cwd/relative-path combinations,
  plus one end-to-end named example.
- Rationale: the relative-path rule is a total function over three small
  domains, so a property states it once and covers the cross product; the
  invocation count needs one real spawn.
- Artefact: `cuprum/unittests/test_executable_binding_property_based.py`,
  `cuprum/unittests/test_executable_binding_execution.py`.
- Evidence: `make test-python` passes; the end-to-end case asserts the child's
  own `sys.argv[0]` report and the recorded `resolved_path` are equal, via
  `test_a_relative_execution_cwd_anchors_a_relative_binding_once` and the
  relative-`cwd` pipeline regression.
- Non-vacuity: the relative-path rule is split across **two** properties, one
  per directory spelling, and the split is load-bearing rather than
  presentational. `test_relative_binding_resolves_inside_the_working_directory`
  draws `@given(path=_RELATIVE_PATH, cwd=_ABSOLUTE_PATH)` and asserts the
  result *is* absolute;
  `test_relative_binding_joins_a_relative_directory_verbatim` draws
  `@given(path=_RELATIVE_PATH, cwd=_RELATIVE_PATH)` and asserts the result is
  **not** absolute. These are opposite claims about the same helper. A single
  property over a `cwd` strategy drawing both spellings *could* still express
  both contracts by asserting exact equality and checking absoluteness
  conditionally on which spelling `cwd` drew, so the split is not forced by
  inexpressibility. It is kept because it gives each directory domain its own
  run rather than one run whose branch is chosen by the generator: a
  conditional assertion inside one property is not exercised when the strategy
  happens not to draw the spelling its branch needs, whereas two properties
  each fail on their own for their own domain. Both properties assert **exact
  equality** with `str(Path(cwd) / str(binding.path))` rather than a prefix or
  containment test, because a resolution returning some other executable under
  the same directory would satisfy "lives under `cwd`" while running a file the
  binding never named. Both sides are composed through `Path`, so the
  comparison speaks the platform's separator instead of assuming `/`. The
  separation is what makes each falsifiable alone. A mutation making
  `resolve_binding` anchor the directory itself
  (`Path(cwd).resolve() / resolved`) fails the relative-directory property with
  `A relative directory is joined, not anchored` while the absolute-directory
  property still passes — the absolute strategy could never have detected that
  defect, which is why the relative case needed a property of its own rather
  than a widened decorator on an existing one. `_CWD` separately draws `None`,
  an absolute path, **and** a relative path for the absolute-binding
  properties, where a relative directory must never displace an absolute
  binding; that covers a different claim and is not the relative-resolution
  evidence. Neither relative-path property asserts that both binding shapes
  were generated, so this obligation is weaker than O2's: its non-vacuity rests
  on the strategy's construction rather than on a guard that fails when a shape
  goes ungenerated. The invocation counter shares O3's positive/negative
  control, a `len(calls) == 1` assertion in one test paired with a
  `len(calls) == 0` assertion in the refusal test rather than a single body
  carrying both.

Axioms relied on, and why they are not verified here:

- CPython's `asyncio.create_subprocess_exec` uses its first argument as
  `argv[0]` and as the program to execute, and honours `cwd` by changing
  directory in the child before `exec`. This is the documented interface of a
  third-party runtime; the end-to-end tests exercise it against the real
  interface rather than a stub.
- `PurePath.parts` splits on the platform separator, and a POSIX path
  containing no separator is `PATH`-searched rather than looked up relative to
  the working directory. Two pieces of repository-owned logic depend on these
  facts. `advisory_path_rejection` relies on the separator test to decide
  whether a filesystem probe is meaningful at all; `resolve_binding` relies on
  them by *declining* to guess, anchoring a relative binding at `cwd` when one
  is supplied and otherwise returning it unchanged for the platform to resolve.
  Both are verified directly, and `resolve_binding`'s two branches are pinned
  by named tests (`test_resolve_anchors_a_bare_relative_name_in_the_same_way`
  and `test_resolve_leaves_a_bare_name_alone_when_there_is_no_cwd`).
- `ContextVar` isolation across threads and tasks is the mechanism every
  existing scoped policy already relies on; it is not re-derived here, but the
  new bindings are tested through it rather than assumed to inherit it.

## Plan of work

### Stage A — understand and propose

Complete. See `Context and orientation` and `Surprises & discoveries`.

### Stage B — red tests

For each milestone, add the failing test first, run it, and record the failure.
The exact commands are in `Concrete steps`.

### Stage C — implementation

EP-M1, `cuprum/executable_binding.py`. A pure module importing only
`collections.abc`, `dataclasses`, `enum`, `os`, `typing`, and `pathlib`. It
must not import `cuprum.context` at runtime. Define:

- `ExecutablePath = typ.NewType("ExecutablePath", str)`.
- `type ExecutableResolver = cabc.Callable[[], str]`.
- `PathBindingRejection(enum.Enum)` whose member values are the exact raised
  messages: `EMPTY`, `NUL`, `PARENT_SEGMENT`, `NOT_ABSOLUTE`. Declared in check
  order, mirroring `PathRejection` in `cuprum/builders/args.py`.
- `classify_executable_path(raw_value, *, allow_relative)`, returning a
  `PathBindingRejection | None`, mirroring `classify_path_string` including its
  Windows absolute-path regex.
- `executable_path(value, *, allow_relative=False) -> ExecutablePath`, which
  normalizes through `PurePath(...).as_posix()` like `safe_path` does.
- `advisory_rejection(path) -> PathBindingRejection | None`, the bounded
  filesystem check: returns a rejection when the path is absolute and either
  does not exist or carries no executable bit. Named `advisory_` so no caller
  can read it as a guarantee. (New member `NOT_FOUND` / `NOT_EXECUTABLE`.)
- `ExecutableBinding`, a frozen slots dataclass with
  `path: ExecutablePath | None` and `resolver: ExecutableResolver | None`,
  exactly one of which must be set; `__post_init__` rejects both-set and
  neither-set.
- `resolve_binding(binding, *, cwd) -> str`, the single resolution rule.
- `InvalidExecutableBindingError(ValueError)` carrying `program`, `path`, and
  `reason`, building its message once into `msg` and passing it to
  `super().__init__(msg)`, matching `DuplicateProgramError`.

EP-M2, context wiring. Add `cuprum/context/executable_overlay.py` mirroring
`env_overlay.py`: `_coerce_executable_bindings` and
`merge_executable_bindings(parent, child)` with `None` meaning *inherit
unchanged*, child winning on key collision, and a `MappingProxyType` result.
Extend `ScopeConfig` and `CuprumContext` with
`executable_bindings: cabc.Mapping[Program, ExecutableBinding] | None = None`,
coerced in `__post_init__` through `object.__setattr__`. Add
`CuprumContext.with_executable_binding(program, binding)` via `dc.replace`,
`CuprumContext.executable_binding(program)`, and
`CuprumContext.resolve_executable(program, *, cwd)` returning the resolved
string or `None`. Wire the overlay into `narrow`. Extend `registration.py` with
`ExecutableBindingRegistration(_TokenRegistration)` and the public factory
`bind_executable(program, path_or_resolver, *, allow_relative=False)`. Add a
`_resolve_executable_overlay` helper to `_policy.py` so `narrow` stays one
expression per field. Export from `cuprum/context/__init__.py` and
`cuprum/__init__.py`.

EP-M3, execution and telemetry. Add `resolved_path: str | None = None` to
`_StageObservation` after `env_mode`. Resolve it in
`_prepare_execution_observation` (single command) and in
`_build_pipeline_observations` (each stage), both after that path's
`_enforce_allowlist` call. Read it at both spawn sites via a small module-level
helper so the fallback rule exists once. Pass it to every `CommandResult`
construction site. Add `resolved_path: str | None = None` after `env_mode` on
`ExecEvent`, populate it in `_StageObservation.emit`, leave it `None` in
`emit_fail_fast`, and add it to `_verbatim_fields` in
`cuprum/adapters/_support.py`. Leave `cuprum/adapters/metrics_adapter.py`
untouched.

EP-M4, proof and documentation. Unit tests, property tests, isolation tests,
the stateful-machine extension, two behavioural scenarios and their steps in
`tests/features/catalogue.feature`, declared by
`tests/behaviour/test_catalogue_behaviour.py` and (after the module split that
brought the former within Pylint's 400-line ceiling) the scoped-catalogue
scenario by `tests/behaviour/test_catalogue_scope_behaviour.py`, a `### 5.1.2`
section in `docs/cuprum-design.md`, a section in `docs/users-guide.md`, a
`### Added` entry in `CHANGELOG.md`, a section in
`docs/v0-2-0-migration-guide.md`, and a note in `docs/roadmap.md` under item
3.3.1 that this is a separate concern.

### Stage D — refactor and validate

Re-run every gate through `scrutineer`, then request the CodeRabbit review.

## Milestones and plateaus

EP-M1 — the pure binding modules exist and are fully tested.

- Requirements and gaps: the issue's "explicit typed binding" and "specify
  validation authority" clauses, at the value-type level.
- Acceptance evidence: `make test-python` passes with
  `test_executable_paths.py`, `test_executable_binding.py`, and
  `test_executable_binding_property_based.py` present; all three failed before
  the modules existed. 99 tests, 25 named examples for the path vocabulary, 34
  for the binding types, and 15 properties.
- Conformance check: neither module imports a `cuprum` module at runtime other
  than their own pair — `Program` is referenced only under `TYPE_CHECKING` —
  and neither imports the context package at all; the `Program` type is
  untouched; no new dependency is added; both modules are under pylint's
  400-line ceiling.
- Recovery: the milestone is additive and self-contained; `git revert` of its
  single commit restores the prior state with no other file affected.
- Remaining gaps: nothing consumes the modules yet, which is deliberate — the
  value types must exist and be proven before they are wired in.
- Compatibility decision: none required. Both modules are new and unreleased.

EP-M2 — bindings are installable, inheritable, and isolated.

- Requirements and gaps: "specify … scope lifetime", "nested/concurrent
  bindings remain isolated", and "configured approved paths run exactly" (at
  the context level).
- Acceptance evidence: `make test-python` passes; the stateful machine covers
  `bind` factories; per-thread and per-task isolation examples pass.
- Conformance check: `resolve_executable` is not consulted by `is_allowed` or
  `check_allowed`, proven by a test that binds an unallowlisted program and
  still gets `ForbiddenProgramError`; `cuprum/context/core.py` stays within 400
  lines.
- Recovery: revertable independently; the context fields are additive with
  `None` defaults, so the previous behaviour is the absent-binding behaviour.
- Remaining gaps: bindings have no effect on what is executed.
- Compatibility decision: none required. The fields are defaulted and the
  factory is new.

EP-M3 — the bound executable is what runs, and the path is observable.

- Requirements and gaps: "configured approved paths run exactly", "keep the
  executed path inspectable", and "telemetry representation".
- Acceptance evidence: `make test-python` passes; a real subprocess spawned
  through a binding reports its own `sys.argv[0]`, which is the bound path; the
  observe stream carries `resolved_path` per event with `event.program` still
  the logical `Program`; the metrics labels are unchanged.
- Conformance check: on every path the resolution call site is textually after
  the enforcement call site for the same command, verified by reading the two
  functions and by the resolver-counting test; `ExecEvent` gains no field ahead
  of `exec_id`; `CommandResult` gains no positional field.
- Recovery: revertable independently; with no binding in scope,
  `resolve_executable` returns `None` and the spawn falls back to
  `str(cmd.program)`, which is the previous behaviour.
- Remaining gaps: pipelines do not gain per-stage *result* differentiation
  beyond what `resolved_path` already gives; documentation is not yet written.
- Compatibility decision: none required. Both new fields are trailing and
  defaulted.

EP-M4 — the acceptance list is discharged and documented.

- Requirements and gaps: all remaining issue clauses, including the TOCTOU
  documentation requirement and the roadmap disambiguation.
- Acceptance evidence: `make test-python`, `make markdownlint`, and the
  behavioural scenario pass; the doc sections exist and are linked.
- Conformance check: every issue acceptance bullet maps to a named test or a
  named documentation section; `docs/roadmap.md` still lists 3.3.1 unchanged.
- Recovery: revertable; documentation is additive.
- Remaining gaps: none.

EP-M5 — gates and review.

- Acceptance evidence: `scrutineer` reports every gate green at a single head
  commit; `coderabbit review --agent` returns no unresolved finding.

## Concrete steps

All commands run from the worktree root, the working directory this plan was
authored in.

Red stage, EP-M1 (run before creating the module):

```sh
env -u BASH_ENV make test-python 2>&1 | tee /tmp/test-python-cuprum-issue-440.out
```

Expect the new modules' tests to fail with a collection error naming
`cuprum.executable_binding`.

Green stage, EP-M1:

```sh
uv run pytest cuprum/unittests/test_executable_binding.py \
  cuprum/unittests/test_executable_binding_property_based.py -q
```

Expect a passing summary line with no failures.

Full Python suite after each milestone:

```sh
env -u BASH_ENV make test-python 2>&1 | tee /tmp/test-python-cuprum-issue-440.out
```

Commit gate before every commit, delegated to `scrutineer`:

```sh
make fmt && make check-fmt && make lint && make test && make markdownlint
```

`make lint` regenerates `typos.toml`; commit that file as its own commit, as
the repository convention requires.

## Validation and acceptance

Acceptance is behavioural. After EP-M4, each of the following must be true and
each is asserted by a named test:

1. An unapproved executable that shares a bound program's basename is rejected.
   Bind `Program("sccache")` to `/opt/.../sccache`, keep `sccache` off the
   allowlist, and expect `ForbiddenProgramError` with the resolver never
   called. This is the issue's explicit rejection requirement.
2. A configured approved path runs exactly. Bind an allowlisted logical
   program to a script whose catalogued name resolves to a different, working
   script, and assert the child's own `sys.argv[0]` report names the bound file
   and `result.resolved_path` equals it. The decoy is what makes this a witness
   rather than a tautology: the catalogued program is a working executable, so
   ignoring the binding produces a successful but wrong run rather than only a
   missing-file error, and the child reports the decoy's own different path, so
   the exact-equality assertion fails. Assert the exact approved marker and
   bound path in the child's report, and check `result.resolved_path`
   separately. The child's report detects execution of the decoy even if the
   parent reports the intended bound path. Asserted by
   `test_a_bound_program_runs_the_bound_file_not_the_catalogued_one`,
   `test_the_child_receives_the_bound_path_as_its_own_argv0`, and
   `test_a_relative_execution_cwd_anchors_a_relative_binding_once` for the
   relative-`cwd` spelling.
3. Nested and concurrent bindings are isolated. Assert a child scope overrides
   for the same program while leaving sibling bindings intact, and that a
   binding installed in one thread or task is invisible in another.
4. The executed path is inspectable and identity is preserved. Assert
   `result.resolved_path` is set, `result.program` is still the logical
   `Program`, the logging extra `cuprum_resolved_path` is present, the tracing
   attribute `cuprum.resolved_path` is present, and no metric label carries a
   path.
5. TOCTOU limits are documented. Assert `docs/cuprum-design.md` contains a
   section stating that the check is advisory and naming filesystem ownership,
   permissions, and read-only deployment as the operator's responsibility.
6. A resolver that breaks its contract is reported rather than obeyed. The
   error type depends on the boundary, and the acceptance evidence
   distinguishes them. A direct call to `resolve_binding` with a resolver
   returning a non-`str` raises `TypeError` naming the returned type — `None`
   in particular, because the execution layer reads it as *unbound* and would
   otherwise run the catalogued name instead. The context method
   `CuprumContext.resolve_executable` and execution through a command instead
   raise `ExecutableResolutionError` naming the logical program, with that
   `TypeError` chained as `__cause__`: the execution boundary keeps one handler
   for every resolver failure, and an unwrapped `TypeError` there would be
   indistinguishable from an unrelated programming error nearby. A resolver
   that raises surfaces as `ExecutableResolutionError` at both boundaries, with
   the original exception chained, and a resolver's own
   `ExecutableResolutionError` is propagated unwrapped. Asserted by
   `cuprum/unittests/test_executable_binding_failures.py`.

Quality criteria:

- Tests: `make test` passes, including the new unit, property, isolation,
  stateful, and behavioural tests.
- Verification: O1 through O5 are discharged with the evidence named in
  `Verification plan`.
- Lint and format: `make check-fmt` and `make lint` pass, including
  `interrogate`, Skylos, Ruff, Pylint, Whitaker, and spelling.
- Markdown: `make markdownlint` and `make nixie` pass.
- Typecheck: `make typecheck` passes.
- Security: no new dependency, no new network or filesystem privilege beyond
  the advisory `os.access` probe.

## Idempotence and recovery

Every step is additive and re-runnable. `make test-python` and the other gates
may be re-run at any time. If a gate fails, read the captured log under `/tmp/`
rather than re-running it blind; the repository convention is that a gate log
records the head commit it ran against.

Each milestone is a separate commit and can be reverted independently. If EP-M3
proves wrong — for example if the single-resolution-point design needs more
touch points than the tolerance allows — revert EP-M3's commit, keep EP-M1 and
EP-M2, and re-plan the resolution point.

## Artefacts and notes

The two spawn sites that construct `argv[0]`, before the change:

```python
# cuprum/_subprocess_execution.py:171
return await _wait4_process.spawn_direct_process(
    _wait4_process.DirectProcessConfig(
        argv=execution.cmd.argv_with_program,
        ...
    )
)
```

```python
# cuprum/_pipeline_spawn.py:97
process = await asyncio.create_subprocess_exec(*observation.cmd.argv_with_program, ...)
```

Both read a `SafeCmd` property that hard-codes `str(self.program)` as the first
element. After the change both read `_StageObservation.resolved_path`, falling
back to that same expression when no binding is in scope.

## Interfaces and dependencies

`cuprum/executable_paths.py` (shipped):

```python
ExecutablePath = typ.NewType("ExecutablePath", str)


class PathBindingRejection(enum.Enum):
    EMPTY = "ExecutablePath cannot be empty"
    NUL = "ExecutablePath cannot contain NUL characters"
    PARENT_SEGMENT = "ExecutablePath cannot contain '..' segments"
    NOT_ABSOLUTE = "ExecutablePath requires an absolute path by default"
    NOT_FOUND = "ExecutablePath does not exist"
    NOT_EXECUTABLE = "ExecutablePath is not executable"


class InvalidExecutableBindingError(ValueError):
    def __init__(
        self,
        program: Program | None,
        path: str,
        reason: PathBindingRejection,
    ) -> None: ...


def classify_executable_path(
    raw_value: str, *, allow_relative: bool
) -> PathBindingRejection | None: ...


def executable_path(
    value: str | Path, *, allow_relative: bool = False
) -> ExecutablePath: ...


def coerce_path_string(value: str | Path) -> str: ...


def advisory_path_rejection(
    value: str,
) -> PathBindingRejection | None: ...
```

`cuprum/executable_binding.py` (shipped), which re-exports the six public names
above:

```python
type ExecutableResolver = cabc.Callable[[], str]


@dc.dataclass(frozen=True, slots=True)
class ExecutableBinding:
    path: ExecutablePath | None = None
    resolver: ExecutableResolver | None = None

    @property
    def is_lazy(self) -> bool: ...


def executable_binding(
    program: Program,
    path_or_resolver: str | Path | ExecutableResolver,
    *,
    allow_relative: bool = False,
) -> ExecutableBinding: ...


def resolve_binding(binding: ExecutableBinding, *, cwd: str | None) -> str: ...
```

`cuprum/context/executable_overlay.py`:

```python
def merge_executable_bindings(
    parent: cabc.Mapping[Program, ExecutableBinding] | None,
    child: cabc.Mapping[Program, ExecutableBinding] | None,
) -> cabc.Mapping[Program, ExecutableBinding] | None: ...
```

`cuprum/context/core.py` additions:

```python
class CuprumContext:
    executable_bindings: cabc.Mapping[Program, ExecutableBinding] | None = None

    def with_executable_binding(
        self, program: Program, binding: ExecutableBinding
    ) -> CuprumContext: ...

    def executable_binding(self, program: Program) -> ExecutableBinding | None: ...

    def resolve_executable(
        self, program: Program, *, cwd: str | None = None
    ) -> str | None: ...
```

`cuprum/context/registration.py` addition:

```python
class ExecutableBindingRegistration(_TokenRegistration):
    __slots__ = ("_binding", "_program")


def bind_executable(
    program: Program,
    path_or_resolver: str | ExecutableResolver,
    *,
    allow_relative: bool = False,
) -> ExecutableBindingRegistration: ...
```

`cuprum/events.py` addition, declared after `env_mode` and ahead of main's
tail-pinned `terminal_outcome`:

```python
resolved_path: str | None = None
```

`cuprum/sh/results.py` addition, `kw_only` beside the other measurements:

```python
resolved_path: str | None = dc.field(default=None, kw_only=True)
```

Dependencies: none added. `os.access` and `pathlib.PurePath` are standard
library. No Rust component is touched.

[pr-571]: https://github.com/leynos/cuprum/pull/571
