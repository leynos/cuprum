# Adopt the nose duplication gate in Cuprum

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: IN PROGRESS

## Purpose / big picture

Cuprum has no automated defence against duplicated logic: two modules can
acquire the same helper, or the same `__exit__` body can be copied seven times,
and nothing fails until a reviewer notices or a fix is applied to only one
copy. After this change `make lint` runs a pinned duplication detector over the
production Python package and fails on any duplicated family that has not been
explicitly and individually reasoned into an exception list. Contributors get a
one-command path to see the finding, judge it, and either extract the shared
implementation or record why the parallel structure is deliberate. The
detector, its pin, its provisioning and its exception mechanism are all
inherited by reference from an already-accepted decision in a sibling
repository rather than re-derived here.

## Constraints

- Port the gate and its focused tests only. Do **not** carry over the
  detector-selection benchmark corpus, scorers, tuning sweeps or head-to-head
  report; the tool-selection decision is reused by reference, and its
  precision/recall/timings are not Cuprum's to claim.
- Do not adopt PyChase, its Python 3.13 pin and `PYTHONHASHSEED` workaround, or
  pyscn.
- Do not import episodic's application fixes, exception entries, package names,
  fixtures, or unrelated dependency and coverage migrations.
- Keep Cuprum's Python floor (`requires-python = ">=3.12"`), interpreter matrix
  and runtime dependencies unchanged. The gate runs as isolated CPython 3.14
  tooling and must not require the application to be installed or built.
- Never reduce the ranking budget or raise the size floor to obtain green CI,
  and never describe lower-ranked duplication as enforced when it is not.
- Project the binary through a checksum-verified or pin-verified path with
  compilation fallback prohibited. A missing trusted binary is a provisioning
  failure, not permission to start a source build.
- `make duplication-allow` must take `FIRST`, `SECOND` and `REASON` from the
  Make command line only, and must pass them literally.
- Every full commit gate run is delegated to `scrutineer`; gates run
  sequentially, never in parallel.
- Delivery is a draft pull request only. It is not to be merged.

## Tolerances

- Stop if a genuine duplication family can only be removed by manufacturing a
  generic utility, an inheritance hierarchy, a boolean-mode helper, or a
  cross-layer dependency.
- Stop if removing a family would change observable API behaviour, validation
  order, side effects, resource lifetimes or async-cancellation semantics.
- Stop if the pinned detector version cannot be provisioned from a trusted
  prebuilt source on this platform.
- Stop and report if a gate fails for a reason that is pre-existing or
  environmental rather than caused by this change.
- Stop after three failed correction cycles on the same deterministic gate.

## Risks

- Risk: an `--exclude` glob that silently matches nothing leaves the scan
  wider or narrower than intended while still reporting a clean run. Severity:
  high. Likelihood: certain, and already observed. Mitigation: pin the verified
  root-relative glob in `[tool.nose]`, assert the scanned file set in the
  ported tests, and record the trap in the ADR and developer guide.
- Risk: `top = 30` silently caps the enforced surface, so a genuine clone below
  the cutoff is never adjudicated and a stale exception is never re-observed.
  Severity: medium. Likelihood: certain. Mitigation: choose the root scope so
  the ranked surface is not saturated, report the cap in the gate's own output
  and documentation, and keep stale-entry reporting while stating plainly that
  absence from a capped report is not proof.
- Risk: the ported tests import Python 3.14-only syntax and are collected by an
  older application interpreter. Severity: medium. Likelihood: medium.
  Mitigation: keep the tooling tests out of the default `PYTEST_TARGETS` globs
  and run them from the dedicated `make duplication-test` recipe under an
  explicit 3.14 interpreter.
- Risk: adjudication degenerates into suppression. Severity: high. Likelihood:
  medium. Mitigation: adjudicate each blocking family with a reasoned record,
  prefer extraction where the duplication is real, and refuse any
  repository-wide wildcard or generated reason.

## Progress

- [x] (2026-09-26) Recovered the authoritative reference at the immutable
      revision and read the ADR, the five gate modules and the focused tests.
- [x] (2026-09-26) Provisioned nose 0.20.0 into `.tools/nose` with compilation
      fallback prohibited, and verified `nose 0.20.0`.
- [x] (2026-09-26) Established Cuprum's production Python root, size floor and
      ranking budget by measurement rather than assumption.
- [x] (2026-09-26) Characterized the production duplication cohort.
- [x] (2026-09-26) Ported the five gate modules with `scripts.`-qualified
      sibling imports and PEP 723 headers declaring `>=3.14`.
- [x] (2026-09-26) Added `[tool.nose]` and `[tool.duplication_gate]`
      configuration, with the root-relative exclusion glob the detector
      actually honours.
- [x] (2026-09-26) Added the Make targets and wired the gate into `lint`.
- [x] (2026-09-26) Ported and adapted the focused tests, and split the two
      over-long modules to satisfy the repository's 400-line ceiling.
- [x] (2026-09-27) Adjudicated the production cohort: extracted genuine shared
      implementations and recorded the remaining families as reasoned
      exceptions. `check` reports 25 allowed, no stale entries.
- [x] (2026-09-27) Corrected `_stale_line` after real gate output falsified it,
      added its regression test, removed the entry the extraction invalidated,
      and relocated the close-then-drain helper to `cuprum/_sink_lifecycle.py`
      to clear the 400-line module ceiling. `check` now reports 23 entries
      covering 25 families.
- [x] (2026-09-27) Ran the smoke test in a disposable workspace with the real
      pinned binary, including the grown-family leg; nothing was left behind.
- [x] (2026-09-27) Added the CI cache, install and test steps (commit
      `0cc065c8`).
- [x] (2026-09-27) Added ADR-018 and updated the developer guide, `AGENTS.md`,
      `docs/contents.md`, `docs/repository-layout.md` and the ADR-003 addendum.
- [x] (2026-10-02) Rebasing onto `main` found that number taken by
      [ADR-018: Typed environment policies](../../docs/adr-018-typed-environment-policies.md),
      so this branch's record was renumbered to ADR-019 across
      `docs/contents.md`, the developer guide and the ADR-003 addendum.
- [ ] Run the gates and open the draft pull request.

### Gate remediation (2026-09-27)

The first full gate run failed four checks. Each is fixed; the fixes are commits
`a3405c28` (code) and `a497eb8b` (docs and manifest).

- **`ty` invalid-argument-type, twice.** `_lookup_active_span` was annotated
  `ExecEvent`, but the line-stream and native-pump-cleanup mixins pass their
  own event families. The helper only ever read `event.exec_id`, so the
  parameter is now the token and all four call sites pass `event.exec_id`. This
  is the fourth fix rather than the first because the obvious repair — a
  `runtime_checkable` protocol for the single shared attribute — type-checked
  but pushed the module to 416 lines against the enforced 400 ceiling.
- **`pylint` C0302 (`too-many-lines`) on `tracing_adapter.py`.** Not reported
  by the first gate run and not caused by this branch's own edits: the Protocol
  fix above is what breached the ceiling. Passing the token removed the
  construct entirely and took the module to 396. Three docstring trims along
  the way recovered only nine of sixteen lines, which is why the fix is
  structural. `pylint` is silent at 407, which reads as a pass; it is not one,
  because `too-many-lines` fires only *above* the ceiling. A forced probe
  (`--max-module-lines=1`) reports `407/1` and is what exposes the asymmetry.
- **`pylint` C1803** in `test_duplication_gate.py` (comparison to an empty
  tuple) and **R1710** in `test_gate_entrypoint_binding.py` (a `return` in
  `try` beside a `skip` in `except`). The latter is resolved with the
  `-> typ.NoReturn` helper idiom `test_duplication_gate_blocking.py` already
  documents for exactly this case.
- **Spelling.** Two non-Oxford spellings in `docs/adr-018` were reported and
  corrected. Four more in `pyproject.toml` allow reasons were *not* reported,
  because the spelling gate never reads a file named `pyproject.toml`. They
  were corrected anyway, as genuine ADR-009 violations that the gate has a
  blind spot for, and the gap is now documented where allow reasons are written.

## Surprises & Discoveries

- **`--exclude` globs are matched relative to each configured root, not to the
  repository root.** Measured on the pinned binary with an unbounded surface:
  `--root cuprum --exclude 'unittests/**'` reports 39 families, while
  `--root cuprum --exclude 'cuprum/unittests/**'` reports 449 — byte-identical
  to passing no exclusion at all. The repository-root-relative spelling
  silently matches nothing, so a "production-only" scan that used it would have
  been an unfiltered whole-package scan wearing a narrower label. The
  production root-relative spelling is what `[tool.nose]` pins, and the trap is
  captured by a ported test and by the documentation.
- **The ranked surface saturates.** Without an exclusion the production root
  carries 449 families, so `top = 30` would enforce an arbitrarily chosen 6.7%
  slice. Scoping production Python to `cuprum` with `cuprum/unittests` excluded
  leaves 39 families, so the same budget is a non-degenerate but
  complete-enough surface whose first adjudication pass is tractable.
- **The detector's default include surface never reaches the test tree.**
  `nose` respects `.gitignore` and, without an explicit `--exclude`, still
  reported only 449 families for `cuprum/`, all `scope=prod`, so the exclusion
  is about narrowing the package's own unit tests rather than about the
  repository checkout at large.
- **A seven-member `__exit__` family dominates the ranking.** Seven observation
  modules share a 158.4-value exact family over their context-manager exit
  bodies. It is the single largest finding and the first adjudication decision.
- **A PEP 723 header suppresses type checker resolution of sibling modules.**
  `ty` 0.0.74 stops resolving `scripts/`-sibling imports inside any file
  carrying inline script metadata: a byte-identical copy of
  `scripts/duplication_gate.py` with the four header lines removed resolves
  cleanly, while the copy that keeps them reports three `unresolved-import`
  errors. The header is what distinguishes the failing module from
  `scripts/nose_detector.py`, which bare-imports the same siblings and passes.
  Qualifying the imports as `scripts.nose_detector` restores resolution with
  the header intact, at both 3.12 and 3.14.
- **`[tool.ty.src] exclude` does not neuter an explicit command-line path.**
  Probed because a global exclusion that also silenced the targeted pass would
  have made "excluded" indistinguishable from "never checked". With the config
  in its real location, the excluded module stays checked when named directly
  but vanishes from the project-wide pass. An earlier probe that appeared to
  show silent skipping used a config file in `/tmp`, whose directory `ty`
  treats as the project root — the exclusion then matched nothing and the
  apparent skip was a root-discovery artefact, not the option's semantics.
- **`uv run -m <module>` ignores the PEP 723 header; only a script path is
  honoured.** The first Makefile recipe used
  `uv run --no-project --python 3.14 -m scripts.duplication_gate`, on the
  reasoning that `-m` matches the `scripts.`-qualified imports. Measured:
  `uv run` reads inline script metadata only from a *script named on the
  command line*, so `-m` silently falls back to the ambient environment. At the
  repository root that resolved `cyclopts`/`tomlkit` from the application
  `.venv`, so `make duplication` passed for the wrong reason while genuinely
  depending on the application venv being built. Reproduced in a copied
  workspace with no `.venv`: `ModuleNotFoundError: No module named 'cyclopts'`.
  The working form is the script path plus `PYTHONPATH=.`:

      PYTHONPATH=. NOSE_BIN=<abs> uv run --no-project --python 3.14 scripts/duplication_gate.py

  Verified by probe: the run materializes an ephemeral environment under
  `.uv-cache/environments-v2/`, `cyclopts`/`tomlkit` resolve from *that* env,
  the native `_cuprum` extension is absent, and no `.venv` path appears. This
  is what makes the "must not require the application to be installed or built"
  constraint true rather than merely asserted. `scripts/__init__.py` is not
  required: `scripts/` resolves as a namespace package, so the `scripts.<name>`
  imports work with only the root on `sys.path`.
- **A test suite wrote fixture entries into the repository's real manifest,
  and the leak was committed.** The ported `make duplication-allow` tests
  expand the checkout's own `Makefile` while running against a copied
  workspace. When the gate modules still resolved through the ambient
  environment, the `duplication-allow` target's writes landed in
  `pyproject.toml` at the repository root instead of the workspace's copy: the
  committed manifest briefly carried `cuprum/a.py`, `cuprum/b.py::beta` and
  `cuprum/b.py` allow entries, one with a quoted `$(touch /tmp/pytest-of-...)`
  command-injection string as its reason. It was found by noticing the gate
  reporting three *stale* allow entries for paths that do not exist. The
  `PYTHONPATH` fix above removes the cause, and
  `test_make_duplication_allow_never_writes_to_the_checkout` now compares the
  checkout's manifest byte-for-byte around a real target run. That guard was
  falsified by restoring the old resolution on purpose: it failed with its
  intended message and the manifest was polluted again, so it is load-bearing
  rather than vacuous. The lesson generalizes — a test that drives a real build
  target must assert its writes stayed in its sandbox.
- **The 400-line ceiling is genuinely enforced on `scripts/tests`, and by
  measurement rather than assumption.** A stale memory note held that pylint
  never descends into test directories. That is true of `cuprum/unittests` —
  main carries files up to 993 lines there and pylint reports nothing — but
  false of `scripts/tests`, where main's largest file is 392 lines. Two ported
  modules landed over the ceiling (515 and 518) and pylint 4.0.9 reported C0302
  on both. The fix is splitting, not suppression: no inline `# pylint: disable`
  exists anywhere in the estate, and `max-module-lines` is an enabled check.
  Splitting by subject also improved the tests — the real-detector tests now
  sit together in `test_duplication_gate_blocking.py`, and the injected-seam
  tests in `test_duplication_gate_seams.py`, so each module states one contract.
- **Lint findings must be re-measured after a split, not carried over.** The
  split moved code between modules and changed which lines the remaining
  findings sat on. Beyond that, several findings were resolved by *relocation*
  rather than editing: two `R1732` sites and one `R1710` disappeared from the
  modules that remained. Fixing the stale line numbers would have been wasted
  work, and trusting them would have left real findings behind. Re-run the gate
  and read its output rather than acting on a remembered list.
- **An adjacent refactor deadlocked a concurrency test, and the probe caught
  the shape but not the semantics.** Converting the `R1732` pair to an
  `ExitStack` helper was correct, but the first attempt also moved the `wait()`
  calls inside the `with allowlist._locked_file(...)` block. A writer blocked
  on the advisory lock cannot exit, so the test waited on processes that were
  waiting on the lock it still held: `duplication-test` went from 132/1 to
  131/2 with a `TimeoutExpired`. The lint probe had already confirmed the
  *shape* was clean, which is exactly what made it misleading — the question
  was never syntactic. The writers must be started under the lock and waited
  for after it releases, with the `ExitStack` surviving the release via
  `pop_all()` so a failed assertion still reaps them.
- **A namespace package made the gate adjudicate the wrong tree, and only an
  out-of-tree run could see it.** `scripts/` has no `__init__.py`, so CPython
  builds its `_NamespacePath` from *every* `sys.path` entry holding a `scripts`
  directory. A development install of the application writes `cuprum.pth` into
  the virtualenv — a one-line file whose entire content is the checkout root —
  and that appends the checkout to `sys.path`. Invoked as
  `python scripts/duplication_gate.py`, the entry file's own directory is
  `sys.path[0]`, not its parent, so the *only* entry contributing a `scripts`
  child is the development install. Every `from scripts.<name> import ...` then
  resolved to the **application checkout**: `PYPROJECT`, the allow list, the
  detector version and the binary all came from the repository, while the
  working directory, the configured roots and the file walk came from the
  workspace. The gate printed
  `duplication gate passed; 25 allowed by reasoned exceptions` for a tree it
  had never scanned. The smoke test found it because it is the first thing in
  this work that runs the gate from outside the checkout; the ported tests all
  set `PYTHONPATH` to the workspace root, which lands at `sys.path[1]` — ahead
  of the `.pth` entry — so the correct tree won on path order and the defect
  could not reproduce. The fix is a
  `sys.path.insert(0, <entry point's parent's parent>)` inlined in
  `scripts/duplication_gate.py` before the sibling imports. It cannot be a
  helper module: a helper is *itself* imported through the same namespace
  lookup, so it would be loaded from the wrong tree and pin nothing. It also
  cannot be replaced by `-m`, because `uv run` reads the PEP 723 header only
  from a script path (see above) — the two constraints meet here.
  `test_gate_entrypoint_binding.py` reproduces the arrangement by *dropping*
  `PYTHONPATH` from the environment, which is exactly what a developer running
  the script directly gets; it was falsified by removing the pin, failing with
  `returncode=0` and the checkout's own passing report. The general lesson:
  when the test harness arranges a search path the production entry point does
  not have, the harness has stopped testing the entry point.

## Decision Log

- **Reference revision.** Adopt from
  `leynos/episodic@d9e5ac0d254f375e2986f52d91a3b88c117c833b`, the merged
  revision of PR #276, not the PR head and not unpinned upstream `main`.
- **ADR identifier.** Cuprum uses flat `docs/adr-NNN-kebab-title.md` files and
  currently holds 001–017, so this repository's record is ADR-018. The
  reference's number is not reused.
- **ADR identifier amended at rebase (2026-10-02).** The premise above was
  true when written, but `main` then landed
  [ADR-018: Typed environment policies](../../docs/adr-018-typed-environment-policies.md)
  independently, so two records claimed 018. `main`'s is merged and older, so
  it keeps the number and this branch's record was renumbered to
  [ADR-019](../../docs/adr-019-adopt-nose-duplication-gate.md). The reasoning
  above is retained as the record of what was decided at the time; only the
  number changed.
- **Detector scope.** `roots = ["cuprum"]` with an exclusion for the package's
  own unit tests, following Cuprum's existing production-Python idiom
  (`SKYLOS_PRODUCTION_TARGETS ?= cuprum`,
  `SKYLOS_EXCLUDE_FOLDERS ?= cuprum/unittests`). `tests/`, `benchmarks/` and
  `scripts/` are maintained but not shipped, and are deliberately outside this
  first gate.
- **Tooling interpreter.** The gate runs under `uv run` as isolated CPython
  3.14 tooling with its inline pins intact, selected explicitly for both the
  gate and its tests. The application floor stays at 3.12.
- **Atomic write.** Cuprum has no reusable atomic-write helper — the only
  sibling, `benchmarks/ratchet_history_persistence.write_history`, is
  JSON-scoped and does not offer mode preservation, directory syncing or
  cleanup — so the reference's small helper is ported rather than duplicated
  around.
- **Sibling imports use the `scripts.` package form.** Cuprum already imports
  its own tooling that way (`scripts/check_boundary_contract.py`) and documents
  `python3 -m scripts.release_assets_cli` as the entry point, so the ported
  modules follow the house style instead of the reference's bare-name imports.
  This is a deliberate downstream deviation, and it is what makes the gate
  modules type-checkable at all: see the PEP 723 discovery above. Make targets
  and tests therefore invoke the gate by script path with `PYTHONPATH=.` set to
  the repository root, which also removes the `PYTHONPATH=scripts` requirement.
  Note that this must be a *script path* and not `-m`: `uv run` reads the PEP
  723 header only from a script named on the command line, so `-m` would
  silently resolve the gate's dependencies from the ambient environment.
- **Tooling modules are type-checked in their own pass.** `make typecheck` runs
  the application pass at Cuprum's 3.12 floor and then re-checks
  `$(DUPLICATION_SOURCES)` at `$(DUPLICATION_PYTHON)`. The modules use
  `typing.TypeIs` and `PurePosixPath.full_match`, neither of which exists at
  3.12, so a single pass can only be satisfied by weakening them or by lying
  about the floor. Two passes keep the application's declared support honest
  and still check the tooling, rather than excluding it from type checking
  altogether.

## Outcomes & Retrospective

To be completed. Exact validation outcomes, and any deviation from the
reference, belong here at the end.
