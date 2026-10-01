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
- [x] (2026-10-01 16:05Z) EP-M1 complete. The module split into
  `cuprum/executable_paths.py` (path vocabulary) and
  `cuprum/executable_binding.py` (binding + resolution) after the single module
  reached 426 lines. 99 tests pass.
- [x] (2026-10-01 17:52Z) EP-M2 complete.
  `cuprum/context/executable_overlay.py` (95 lines) carries
  `merge_executable_bindings`; `cuprum/context/_executable.py` (148 lines)
  carries the bindings field, coercion, `with_executable_binding`, and
  `resolve_executable`; `ExecutableBindingRegistration` and `bind_executable`
  live in `cuprum/context/registration.py`, re-exported through
  `cuprum/context/__init__.py`. `resolve_executable` is pinned as independent
  of the allowlist. 96 focused tests pass.
- [x] (2026-10-01 18:20Z) EP-M2 gate sweep. Five defects sat behind the
  environmental abort: two spelling errors (`concretised`, `hand-written`), the
  R9110 executable-overlay delegate, four `ty` diagnostics in the
  deliberate-wrong-type tests, and a ruff PT012 trip introduced while fixing the
  `ty` findings. At `0e177d29`, `make check-fmt lint typecheck` exited clean
  (`/tmp/make-code-cuprum-issue-440.out`). That target chain includes the Rust
  gates, so `cargo +nightly-2026-05-28 fmt --check`, rustdoc, clippy, whitaker,
  the typos gate, `yamllint`, and `actionlint` all passed as well;
  `make markdownlint` and `make nixie` passed too. `make test` has not been run
  at any commit on this branch.
- [ ] EP-M3: spawn-time resolution, `ExecEvent.resolved_path`, adapter
  projection, `CommandResult.resolved_path`.
- [ ] EP-M4: behavioural scenario, isolation and stateful tests, docs,
  changelog, migration guide, roadmap note.
- [ ] EP-M5: gates green, push, draft pull request, CodeRabbit review.

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

Not yet complete. To be filled in at EP-M5, comparing the shipped surface
against the issue's acceptance list: unapproved same-basename path rejected,
configured approved path run exactly, nested and concurrent bindings isolated,
TOCTOU limits documented.

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
  `cuprum/unittests/test_executable_binding_context.py`.
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
- Artefact: `cuprum/unittests/test_executable_binding_telemetry.py`.
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

All commands run from the worktree root,
`/home/leynos/.lody/repos/github---leynos--cuprum/worktrees/cd050cee-fb9a-4172-8a2b-0c20d30fd3d1`.

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
