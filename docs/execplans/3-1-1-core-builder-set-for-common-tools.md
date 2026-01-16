# Publish core builder set for git, rsync, and tar (3.1.1)

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: DRAFT

PLANS.md: Not present in the repository root at plan time, so no additional
plan governance applies.

## Purpose / big picture

Deliver a core builder library for common tools (git, rsync, tar) that uses
typed argument helpers (SafePath and GitRef) with runtime validation. Success
is observable when callers can import the new builders, create SafeCmd
instances with validated arguments, and see the expected argv output in unit
and Behaviour-Driven Development (BDD) tests. All required quality gates pass,
and documentation reflects the new API, with the roadmap item 3.1.1 marked
as done.

## Constraints

- Preserve the existing `sh.make`, `SafeCmd`, and catalogue behaviour; only
  additive changes are allowed.
- Keep builder APIs explicit and typed; do not add implicit PATH discovery or
  dynamic command resolution.
- Use only the standard library; adding new external dependencies requires
  escalation.
- Documentation must follow `docs/documentation-style-guide.md` (British
  spelling, 80-column wrap, and markdownlint compliance).

## Tolerances (exception triggers)

- Scope: if the implementation needs changes to more than 12 files or more
  than 700 net lines, stop and escalate.
- Interface: if an existing public API signature must change or a default
  behaviour needs alteration beyond adding new exports, stop and escalate.
- Dependencies: if a new external dependency is required, stop and escalate.
- Tests: if the new tests still fail after two full fix attempts, stop and
  escalate with logs.
- Ambiguity: if "core builder set" requires broader tool coverage than the
  plan proposes, stop and ask for confirmation.

## Risks

- Risk: GitRef validation may reject legitimate refs for some users.
  Severity: medium. Likelihood: medium. Mitigation: document a conservative
  subset of accepted ref formats, test common refs, and provide clear error
  messages.
- Risk: SafePath validation might block valid rsync or tar paths (for example,
  relative paths or traversal segments needed by callers).
  Severity: medium. Likelihood: medium. Mitigation: allow explicit opt-in for
  relative paths via a helper flag and document this behaviour.
- Risk: Users may expect remote rsync targets, which SafePath should not
  accept.
  Severity: low. Likelihood: medium. Mitigation: document that remote targets
  are out of scope for the core builders and require custom builders.

## Progress

- [x] (2026-01-16 00:00Z) Drafted ExecPlan for roadmap item 3.1.1.
- [ ] Define typed helper validation rules and builder API surface.
- [ ] Add unit tests and BDD scenarios that initially fail.
- [ ] Implement typed helpers and builder functions for git, rsync, and tar.
- [ ] Update catalogue defaults and public exports.
- [ ] Update docs and mark roadmap item done.
- [ ] Run formatting, lint, typecheck, and tests.

## Surprises & discoveries

None yet.

## Decision log

- Decision: Implement a new `cuprum/builders` package with submodules per tool
  and a shared `args.py` for typed helpers.
  Rationale: Keeps builder APIs grouped and discoverable without bloating
  `cuprum/sh.py`.
  Date/Author: 2026-01-16 (Codex).
- Decision: Use conservative runtime validation for SafePath and GitRef and
  call validators inside builder functions, not only in helper constructors.
  Rationale: Avoids bypassing validation when callers pass raw strings.
  Date/Author: 2026-01-16 (Codex).

## Outcomes & retrospective

Pending.

## Context and orientation

`cuprum/sh.py` defines `sh.make`, `SafeCmd`, and argv coercion rules. The
default allowlist and program constants live in `cuprum/catalogue.py`. Tests
are split into unit tests under `cuprum/unittests/` and BDD scenarios in
`tests/features/` with step implementations in `tests/behaviour/`. The users'
guide (`docs/users-guide.md`) documents builder patterns, and
`docs/cuprum-design.md` captures design decisions. The new core builders should
be implemented as a dedicated package so they are easy to import and document.

The plan introduces two typed argument helpers:

- SafePath: a validated filesystem path represented as a `NewType` over `str`.
- GitRef: a validated git ref (branch, tag, or SHA) represented as a
  `NewType` over `str`.

Validation rules must be explicit, documented, and enforced at runtime.

## Plan of work

Stage A: understanding and scope confirmation. Review `docs/roadmap.md`,
existing builder guidance in `docs/users-guide.md`, and relevant sections of
`docs/cuprum-design.md`. Decide the exact builder functions to ship for git,
rsync, and tar, along with validation rules for SafePath and GitRef. If any
part of the scope is unclear, pause and ask before writing tests.

Stage B: scaffolding and tests. Create the `cuprum/builders/` package and
define stub APIs so tests can import them. Add unit tests in
`cuprum/unittests/test_builder_library.py` to cover validation rules and argv
construction. Add BDD scenarios in `tests/features/builder_library.feature`
with step implementations in `tests/behaviour/test_builder_library.py` that
exercise the public builder surface. These tests should fail before the real
implementation is added.

Stage C: implementation. Fill in `SafePath` and `GitRef` helpers (runtime
validation plus conversion to string), then implement builder functions for
git, rsync, and tar. Update `cuprum/catalogue.py` to add `GIT`, `RSYNC`, and
`TAR` to the default catalogue and document which project owns them. Update
`cuprum/__init__.py` to export the new program constants and the builder
package or functions.

Stage D: hardening, documentation, and cleanup. Update `docs/users-guide.md`
with a new section describing the core builder library, examples, and
validation behaviour. Record design decisions and validation rules in
`docs/cuprum-design.md`. Mark roadmap item 3.1.1 as done in
`docs/roadmap.md`. Run formatting, linting, typechecking, and tests, fixing any
issues before completion.

## Concrete steps

1. Inspect existing builder guidance and catalogue.

   - Read `docs/users-guide.md`, `docs/cuprum-design.md`, and
     `cuprum/catalogue.py` to align validation rules and exports.
   - Confirm how `sh.make` serialises argv to match builder outputs.

2. Create scaffolding and tests.

   - Add `cuprum/builders/__init__.py`, `cuprum/builders/args.py`,
     `cuprum/builders/git.py`, `cuprum/builders/rsync.py`, and
     `cuprum/builders/tar.py` with minimal stubs.
   - Write unit tests in `cuprum/unittests/test_builder_library.py`.
   - Write BDD scenarios in `tests/features/builder_library.feature` and step
     implementations in `tests/behaviour/test_builder_library.py`.
   - Run `pytest` for the new tests and confirm they fail for the right
     reasons.

3. Implement typed helpers and builders.

   - Implement `SafePath` and `GitRef` validation in
     `cuprum/builders/args.py`.
   - Implement the builder functions, ensuring they call validators internally
     and return `SafeCmd` instances built via `sh.make`.
   - Ensure argv order and flags match tests.

4. Update catalogue and exports.

   - Add `GIT`, `RSYNC`, and `TAR` program constants and include them in the
     default catalogue.
   - Update `cuprum/__init__.py` and relevant unit tests (for example,
     `cuprum/unittests/test_public_api.py`) to reflect new exports.

5. Update documentation and roadmap.

   - Add a users' guide section covering the core builders and their typed
     helpers.
   - Record design decisions in `docs/cuprum-design.md`.
   - Mark 3.1.1 done in `docs/roadmap.md`.

6. Validate and fix issues.

   - Run formatting and linting.
   - Run typechecking and tests.
   - Resolve any failures and re-run until clean.

## Validation and acceptance

Behaviour is accepted when:

- The new unit tests for SafePath, GitRef, and builder argv pass, and they
  fail before implementation.
- The BDD scenarios in `tests/features/builder_library.feature` pass and
  demonstrate user-facing behaviour.
- The default catalogue includes `GIT`, `RSYNC`, and `TAR`.
- Documentation in `docs/users-guide.md` and `docs/cuprum-design.md` describes
  the new builders and validation rules.
- `docs/roadmap.md` shows item 3.1.1 marked as done.

Quality criteria:

- Tests: run `make test` and expect all tests to pass.
- Lint/typecheck/format: run `make check-fmt`, `make lint`, and
  `make typecheck` with no failures.
- Documentation checks: run `make markdownlint` and `make nixie` after docs
  updates.

Use `tee` and `set -o pipefail` for long-running commands. Example:

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/make-check-fmt.log
    make lint 2>&1 | tee /tmp/make-lint.log
    make typecheck 2>&1 | tee /tmp/make-typecheck.log
    make test 2>&1 | tee /tmp/make-test.log
    make markdownlint 2>&1 | tee /tmp/make-markdownlint.log
    make nixie 2>&1 | tee /tmp/make-nixie.log

## Idempotence and recovery

All steps are additive and safe to rerun. If a test or lint step fails, fix the
underlying issue and rerun the same command. If a change must be reverted, use
`git checkout -- <path>` for the specific files you changed, then reapply the
step carefully.

## Artifacts and notes

Capture key outputs (for example, failing test excerpts) in the work log when
iterating, but keep the repository clean.

## Interfaces and dependencies

New modules and interfaces to provide:

    cuprum/builders/args.py
      SafePath = NewType("SafePath", str)
      GitRef = NewType("GitRef", str)
      def safe_path(
          value: str | Path,
          *,
          allow_relative: bool = False,
      ) -> SafePath
      def git_ref(value: str) -> GitRef

Validation rules (documented and tested):

- SafePath rejects empty strings, embedded NUL characters, and any `..` path
  segments. By default it requires absolute paths; callers may opt in to
  relative paths with `allow_relative=True`.
- GitRef rejects empty strings, leading `-`, whitespace, and characters outside
  the conservative set `[A-Za-z0-9._/-]`. It also rejects `..`, `//`, `@{`,
  trailing `.lock`, leading or trailing `/`, and references ending with `.`.

Builders (all return `SafeCmd` and call validators internally):

    cuprum/builders/git.py
      def git_status(*, short: bool = False, branch: bool = False) -> SafeCmd
      def git_checkout(ref: str, *, create_branch: bool = False,
          detach: bool = False, force: bool = False) -> SafeCmd
      def git_rev_parse(ref: str) -> SafeCmd

    cuprum/builders/rsync.py
      def rsync_sync(source: str, destination: str, *, archive: bool = False,
          delete: bool = False, dry_run: bool = False, verbose: bool = False,
          compress: bool = False) -> SafeCmd

    cuprum/builders/tar.py
      def tar_create(archive: str, sources: Sequence[str], *,
          gzip: bool = False, bzip2: bool = False, xz: bool = False) -> SafeCmd
      def tar_extract(
          archive: str,
          *,
          destination: str | None = None,
      ) -> SafeCmd

Exports and catalogue updates:

- Add `GIT`, `RSYNC`, and `TAR` program constants in `cuprum/catalogue.py` and
  include them in `DEFAULT_PROJECTS`.
- Re-export the program constants (and optionally the builders package) from
  `cuprum/__init__.py`.

No new third-party dependencies are permitted without escalation.
