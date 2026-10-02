# Bind catalogue identity to a validated executable path

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS

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
  declared after `env_mode`, at the end of the field list.
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
- [ ] EP-M4: behavioural scenario, isolation and stateful tests, docs,
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
- [ ] EP-M5: gates green, push, draft pull request, CodeRabbit review.
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
  - [ ] GitHub Actions green at head.
  - [ ] `coderabbit review --agent` returns no unresolved finding at head.

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
  ordering. The lesson generalizes to every plan in this repository: a
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
records.

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

Trace:

```plaintext
issue-440 (bindings + validation)      -> EP-M1 -> tests::executable_binding
issue-440 (scope lifetime + isolation) -> EP-M2 -> tests::context::executable_bindings
issue-440 (exact execution + telemetry)-> EP-M3 -> tests::execution::resolved_path
issue-440 (acceptance + docs)          -> EP-M4 -> tests::behaviour::catalogue,
                                                   docs::cuprum-design §5.1.2
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

O2 — Binding isolation across nested scopes, threads, and tasks.

- Statement: a binding installed in one scope is visible inside that scope and
  absent outside it; a child scope overrides a parent binding for the same
  program and leaves the parent's other bindings intact; two threads, and two
  asyncio tasks, never observe each other's bindings.
- Method: named pytest examples for the nested case; state-machine model
  checking for the register/detach sequences; named examples with a
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
- Non-vacuity: the state machine's `_FACTORIES` tuple gains `bind` and
  `bind-nested` entries, and a check asserts that at least one generated
  sequence actually installed a binding (a generator that never samples the new
  entry would otherwise pass vacuously). The concurrency examples deliberately
  assert the *other* context's binding is absent, so a test that leaked a
  binding would fail rather than pass.

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
  `calls == []`, and the happy-path test asserts the child observed its own
  `sys.executable` while `result.program` is still the logical name.
- Non-vacuity: the same test runs the allowlisted case first and asserts
  `calls == [1]`, proving the counter works; the denial case then asserts
  `calls == []`, proving the ordering. Without the paired positive case the
  empty list would be indistinguishable from a broken counter.

O4 — Telemetry projection: identity preserved, path added, metrics untouched.

- Statement: every `ExecEvent` for an execution whose binding resolved carries
  `resolved_path` set to that string and leaves `program` as the logical
  `Program`; the sanitized `pipeline_fail_fast` event carries no
  `resolved_path`; the logging adapter emits `cuprum_resolved_path`, the
  tracing adapter emits `cuprum.resolved_path`, and the metrics label mapping
  stays exactly `{"program", "project"}`.
- Method: named pytest examples, one per adapter, driving a real subprocess.
- Rationale: this is a finite set of surface-by-surface assertions over a
  record shape, which examples express more readably than a generator.
- Artefact: `cuprum/unittests/test_executable_binding_execution.py` (the
  per-phase event assertions driven by a real spawn), with the adapter surface
  locked in `cuprum/unittests/test_adapter_projection.py`.
- Evidence: `make test-python` passes; the metrics test asserts the label set
  equals `{"program", "project"}` and would fail if a path were added.
- Non-vacuity: the same test asserts the *unbound* case carries
  `resolved_path is None` and that no `cuprum_resolved_path` key is present, so
  an adapter that unconditionally emitted the key would fail.

O5 — Resolver invocation count and relative-path resolution.

- Statement: `resolve_binding` calls a resolver at most once per execution and
  returns its result verbatim; a relative binding that contains a path
  separator is resolved against the supplied `cwd`, while a bare name is
  returned unchanged for the platform to `PATH`-resolve.
- Method: property test over generated binding/cwd/relative-path combinations,
  plus one end-to-end named example.
- Rationale: the relative-path rule is a total function over three small
  domains, so a property states it once and covers the cross product; the
  invocation count needs one real spawn.
- Artefact: `cuprum/unittests/test_executable_binding_property_based.py`,
  `cuprum/unittests/test_executable_binding_execution.py`.
- Evidence: `make test-python` passes; the end-to-end case asserts the child's
  own `sys.executable` and the recorded `resolved_path` are equal.
- Non-vacuity: the property's strategy is constructed so that at least one
  generated case has `cwd` set and a relative path with a separator, and the
  test asserts that combination was exercised. The invocation counter shares
  O3's paired positive/negative control.

Axioms relied on, and why they are not verified here:

- CPython's `asyncio.create_subprocess_exec` uses its first argument as
  `argv[0]` and as the program to execute, and honours `cwd` by changing
  directory in the child before `exec`. This is the documented interface of a
  third-party runtime; the end-to-end tests exercise it against the real
  interface rather than a stub.
- `PurePath.parts` splits on the platform separator, and a POSIX path
  containing no separator is `PATH`-searched rather than looked up relative to
  the working directory. Repository-owned logic that depends on these facts is
  `resolve_binding`, which is verified directly against both.
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
the stateful-machine extension, one behavioural scenario and its steps in
`tests/features/catalogue.feature` and
`tests/behaviour/test_catalogue_behaviour.py`, a `### 5.1.2` section in
`docs/cuprum-design.md`, a section in `docs/users-guide.md`, a `### Added`
entry in `CHANGELOG.md`, a section in `docs/v0-2-0-migration-guide.md`, and a
note in `docs/roadmap.md` under item 3.3.1 that this is a separate concern.

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
  through a binding reports its own `sys.executable`; the observe stream carries
  `resolved_path`; the metrics labels are unchanged.
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
   program to `sys.executable` with a distinct basename, run
   `["-c", "import sys; print(sys.executable)"]`, and assert the child printed
   exactly the bound path and `result.resolved_path` equals it.
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
6. A resolver that breaks its contract is reported rather than obeyed. Assert
   that a resolver returning a non-`str` raises `TypeError` — `None` in
   particular, because the execution layer reads it as *unbound* and would
   otherwise run the catalogued name instead — and that a resolver which raises
   surfaces as `ExecutableResolutionError` naming the logical program, with the
   original exception chained and a resolver's own `ExecutableResolutionError`
   propagated unwrapped. Asserted by
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

`cuprum/events.py` addition, declared after `env_mode`:

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
