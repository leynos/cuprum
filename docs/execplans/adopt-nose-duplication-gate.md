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
  high. Likelihood: certain, and already observed. Mitigation: pin the
  verified root-relative glob in `[tool.nose]`, assert the scanned file set in
  the ported tests, and record the trap in the ADR and developer guide.
- Risk: `top = 30` silently caps the enforced surface, so a genuine clone below
  the cutoff is never adjudicated and a stale exception is never re-observed.
  Severity: medium. Likelihood: certain. Mitigation: choose the root scope so
  the ranked surface is not saturated, report the cap in the gate's own output
  and documentation, and keep stale-entry reporting while stating plainly that
  absence from a capped report is not proof.
- Risk: the ported tests import Python 3.14-only syntax and are collected by an
  older application interpreter. Severity: medium. Likelihood: medium.
  Mitigation: keep the tooling tests out of the default `PYTEST_TARGETS`
  globs and run them from the dedicated `make duplication-test` recipe under an
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
- [x] (2026-09-26) Characterised the production duplication cohort.
- [ ] Write the plan and port the five gate modules.
- [ ] Add `[tool.nose]` and `[tool.duplication_gate]` configuration.
- [ ] Add the Make targets and wire the gate into `lint`.
- [ ] Port and adapt the focused tests.
- [ ] Adjudicate the production cohort.
- [ ] Add the CI cache, install and test steps.
- [ ] Add the ADR and update the developer guide.
- [ ] Run the smoke test in a disposable workspace with the real binary.
- [ ] Run the gates and open the draft pull request.

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
  slice. Scoping production Python to `cuprum` with `cuprum/unittests`
  excluded leaves 39 families, so the same budget is a non-degenerate but
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
  apparent skip was a root-discovery artifact, not the option's semantics.

## Decision Log

- **Reference revision.** Adopt from
  `leynos/episodic@d9e5ac0d254f375e2986f52d91a3b88c117c833b`, the merged
  revision of PR #276, not the PR head and not unpinned upstream `main`.
- **ADR identifier.** Cuprum uses flat `docs/adr-NNN-kebab-title.md` files and
  currently holds 001–017, so this repository's record is ADR-018. The
  reference's number is not reused.
- **Detector scope.** `roots = ["cuprum"]` with an exclusion for the package's
  own unit tests, following Cuprum's existing production-Python idiom
  (`SKYLOS_PRODUCTION_TARGETS ?= cuprum`, `SKYLOS_EXCLUDE_FOLDERS ?=
  cuprum/unittests`). `tests/`, `benchmarks/` and `scripts/` are maintained but
  not shipped, and are deliberately outside this first gate.
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
  and tests therefore invoke the gate as `python -m scripts.duplication_gate`,
  which also removes the `PYTHONPATH=scripts` requirement.
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
