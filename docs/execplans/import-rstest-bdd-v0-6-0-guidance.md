# Migrate Cuprum to the published rstest-bdd v0.6.0 release

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE FOR THE DOCUMENTATION IMPORT; THE DEPENDENCY MIGRATION IS
BLOCKED ON A CROSS-REPOSITORY MSRV DECISION

## Purpose / big picture

The migration request asks for this repository to be moved onto the published
`rstest-bdd` v0.6.0 release, with the migration implemented and validated
rather than merely recommended.

The survey performed before planning, on 2026-09-26, established that this
repository did not depend on `rstest-bdd` in any Cargo manifest or lockfile at
that time. That finding has since been overtaken: commit `d79dded2a` (PR #432,
"Introduce RustStreamError at the PyO3 boundary (6.1.1)") added
`rstest-bdd = "0.5.0"` and `rstest-bdd-macros = "0.5.0"` to
`rust/cuprum-rust/Cargo.toml` on 2026-10-12, and that commit is an ancestor of
this plan's last rebase base `ed45afde4` (2026-10-19). The repository therefore
**does** have one live `rstest-bdd` consumer as published. See
[Premise correction](#premise-correction-2026-10-25) for the corrected finding
and [Blocked migration](#blocked-migration) for why the version move cannot be
completed inside this repository.

What this plan delivers is the part that is real and valuable and remains
correct: the authoritative documentation import, a recorded applicability
analysis, and the provenance trail that lets a future reader confirm both. The
dependency migration itself is recorded as blocked, with the specific external
decision named, rather than as done.

The user-visible outcome is a documentation set in `docs/` that carries the
upstream `rstest-bdd` v0.6.0 user guide and migration guides, matching the
upstream release except for cross-file links, which are absolute so that they
resolve from this repository and from any consumer the text is copied into. It
carries a provenance record and an applicability matrix stating precisely which
v0.6.0 breaking changes this repository is exposed to and which are
inapplicable. A reader can verify the import against the recorded checksums and
can see, stated in one place, why no dependency edit was correct.

## Constraints

Hard invariants that must hold throughout implementation.

- The imported upstream documents in `docs/` must match the blobs at upstream
  commit `72fb22635670e456545ca368805ba4c1c9d7bd69` in every respect **except**
  cross-file link targets, which are absolute GitHub URLs. **Superseded
  2026-10-15.** This constraint originally read "must remain byte-for-byte
  identical … No reformatting, no link rewriting, no local annotation inside
  those files." A later instruction required all relative links in the imported
  documentation to be converted to absolute, which overrides the
  no-link-rewriting clause. The import-time digests are retained in the
  provenance record alongside the current digests so both sides remain
  auditable. No other divergence was introduced, and no local annotation was
  added.
- No dependency may be added that the repository does not need. **Corrected
  2026-10-25.** This constraint originally justified introducing nothing on the
  grounds that the repository had no BDD consumers. It has one: `cuprum-rust`
  declares `rstest-bdd` 0.5.0 and uses it in
  `rust/cuprum-rust/src/stream_error_behaviour.rs`. The constraint still holds
  in its general form — no *unneeded* dependency is added — but it no longer
  establishes that no dependency edit was required.
- No existing behaviour, test coverage, gate, or manifest may be weakened.
- The v0.6.1 beta and v0.7.0 sections inside the imported text must not be
  implemented. The guard-based `StepContext` borrowing model is v0.7.0 and the
  `#[harness_context]` marker is v0.6.1 beta.
- The v0.6-compatible `#[from(rstest_bdd_harness_context)]` spelling must not be
  replaced.
- Unrelated work in the tree must be preserved. The worktree began clean.
- No merge and no release publication.

## Tolerances (exception triggers)

Thresholds that trigger escalation when breached.

- Scope: if implementation requires net changes to more than 8 tracked files or
  200 net lines outside `docs/`, stop and escalate.
- Dependencies: if any `Cargo.toml` or `Cargo.lock` edit is required, stop and
  escalate. The survey says none is correct; an apparent need means the survey
  is wrong and the premise must be re-examined.
- Interface: if any public API signature must change, stop and escalate.
- Iterations: if a documentation gate still fails after 3 attempts, stop and
  escalate.
- Ambiguity: if a reviewer would reasonably expect a dependency edit despite
  the survey, stop and present the evidence rather than editing.

## Risks

- Risk: the request presupposes a dependency that does not exist, and a naive
  reading produces an invented migration. Severity: high Likelihood: high
  (confirmed already) Mitigation: prove the absence exhaustively and with a
  positive control against repositories that *do* consume the framework, then
  implement only the applicable part.

- Risk: the imported documents must pass this repository's Markdown gates
  (`make check-fmt` runs `mdtablefix`, `make markdownlint` runs
  `markdownlint-cli2` plus the en-GB spelling gate) while staying
  byte-for-byte. A formatter that rewrites the imported prose would put the two
  constraints in direct conflict. Severity: high Likelihood: medium Mitigation:
  measure the formatter's effect on the exact imported bytes before committing;
  escalate if it changes them.

- Risk: the upstream `docs/` contains a `developers-guide.md` and ADR numbers
  that collide with this repository's own documents. Severity: medium
  Likelihood: high (confirmed already) Mitigation: record the upstream link
  mapping instead of copying link targets.

- Risk: the en-GB-oxendict spelling gate flags upstream prose.
  Severity: medium Likelihood: medium Mitigation: run the gate and inspect any
  finding before committing.

## Progress

- [x] (2026-09-26 20:00Z) Read `AGENTS.md`, branch state, and working tree;
      confirmed the tree is clean and the branch is a session branch, not
      `main`.
- [x] (2026-09-26 20:02Z) Located every `rstest-bdd` mention in the repository
      and established it is a policy statement and a CI comparison note, not a
      dependency. **Corrected 2026-10-25:** this held on the date taken and
      was overtaken on 2026-10-12 by `d79dded2a`; see
      [Premise correction](#premise-correction-2026-10-25).
- [x] (2026-09-26 20:05Z) Fetched upstream, resolved tag `v0.6.0`, and compared
      it against the commit named in the request.
- [x] (2026-09-26 20:08Z) Proved the absence of an `rstest-bdd` dependency
      across all manifests, all lockfiles, all history, and `cargo metadata`,
      with a positive control. **Corrected 2026-10-25:** the proof was valid
      only before 2026-10-12 and its citation is now retired; see
      [Premise correction](#premise-correction-2026-10-25).
- [x] (2026-09-26 20:10Z) Read the v0.6.0 and v0.5.0 migration guides in full.
- [x] (2026-09-26 20:12Z) Imported the three documents byte-for-byte and
      verified their checksums against the immutable commit.
- [x] (2026-09-26 20:14Z) Wrote the provenance record.
- [x] (2026-09-26 20:20Z) Wrote the applicability matrix, deriving every row
      from a concrete search with its pattern and match count recorded.
- [x] (2026-09-26 20:26Z) Ran the documentation gates; `make markdownlint` and
      `make check-fmt` both pass, and the imported files kept their exact
      digests through every formatting pass.
- [x] (2026-10-15) Rewrote all 16 cross-file relative links to absolute GitHub
      URLs, on a later instruction. Verified every target file and every anchor
      on `main` and at the pinned commit before adopting the base URL. Four
      definitions and one prose line were re-wrapped to satisfy `make check-fmt`
      after the longer URLs pushed them past 80 columns, and one footnote
      definition needed the angle-bracket form to satisfy MD034. Both gates
      re-run and pass. Recorded both sides' checksums in the provenance record
      and superseded the no-link-rewriting constraint above.
- [x] Commit the change.
- [x] (2026-10-19) Opened the pull request as a draft.
- [x] (2026-10-19) Rebased onto `origin/main` at
      `102951f4860a0a0a41e75abbebb8b2b7558f5d6e` from boundary `991dee642`.
      No conflicts arose. Three adaptations followed from the advanced base:
      `typos.toml` is no longer touched by this branch, because the spelling
      gate on `main` independently regenerated the same two entries plus
      others, so the rebased link-rewriting commit carries documentation
      changes only; the two line citations in this plan were re-anchored,
      because `main`'s `AGENTS.md` and `developers-guide.md` edits moved the
      quoted sentences without changing them; and the four imported documents
      were added to `docs/contents.md`, because that index covers every other
      top-level document in `docs/` and these were the only ones missing (see
      the decision log).
- [x] (2026-10-19) Rebased again onto `origin/main` at
      `87d38aff8fbfebb5f965f0df8fc267e9f03a678c`, from boundary `102951f48`.
      No conflicts arose, and all five commits replayed byte-identically:
      `main`'s single new commit touches `docs/developers-guide.md` and
      `scripts/tests/test_markdown_format_makefile.py`, neither of which this
      branch modifies. The advanced base required one further adaptation. The
      "Documentation gates" section still carried counts from the first import
      (82 Markdown files, 677 Python files) and recorded `make test`,
      `make typecheck` and `make lint` as deliberately not run. `main`'s new
      commit is itself a correction of stale Markdown-gate figures, so leaving
      the same defect in this plan would have contradicted the change the
      rebase had just imported. The counts were re-measured and the not-run
      claim replaced with the actual gate results (see the decision log).
- [x] (2026-10-19) Rebased a third time onto `origin/main` at
      `ed45afde4bde458da0677e635186b385570bcde2`, from boundary `87d38aff8`.
      No conflicts arose, and all six commits replayed as exact `range-diff`
      equals. `main` added two commits: one edits `docs/roadmap.md` only, and
      the other separates tar and rsync operands from options across the
      builders, their unit and behavioural tests, and both guides. Neither
      commit touches a path this branch modifies, and the tree delta between
      the old and new heads is byte-identical to `main`'s own delta. The four
      line citations in this plan were re-checked against the new base and all
      still resolve at the same line numbers. The one required adaptation was
      to the gate table: `main` added two Python source files, which moved the
      `ruff format` total from 1008 to 1010 and exposed that the figure had
      always carried the wrong label (see the decision log).

**Corrected 2026-10-25.** The original entry ended "Remaining: review and
merge, which this task explicitly excludes. No implementation work remains."
The first clause still holds for the documentation import. The second does not:
one `rstest-bdd` consumer exists and its version migration is blocked on the
MSRV floor, not finished. See [Blocked migration](#blocked-migration).

- [x] (2026-10-25) Rebased a fourth time onto `origin/main` at `40d56d69f`,
      from boundary `ed45afde4`. All eight commits replayed with no conflicts;
      `range-diff` showed seven identical and one differing only in a context
      line that `main` had reworded. The net diff against the new base is
      exactly the six documents this branch owns, and `main`'s new ADR-026
      survived the replay.
- [x] (2026-10-25) Re-measured the request's premise instead of re-reading it.
      The dependency claim was false as published, having expired on
      2026-10-12. Corrected the Status, Purpose, Constraints, Progress,
      Surprises, Decision log, Outcomes, Conformance basis, Verification plan,
      Retrospective, and three applicability-matrix rows; added
      [Premise correction](#premise-correction-2026-10-25) and
      [Blocked migration](#blocked-migration).

## Surprises & discoveries

- Observation: this repository had no `rstest-bdd` dependency on 2026-09-26,
  when the survey ran, but it acquired one three weeks later. **Corrected
  2026-10-25.** The original entry read "has no `rstest-bdd` dependency, and
  never had one", citing
  `git log --all -S "rstest-bdd" -- '*.toml' '**/Cargo.lock'` as returning no
  commit. Re-running that exact command today returns five commits, the first
  and relevant one being `d79dded2a` (2026-10-12). `cargo metadata` now resolves
  `rstest-bdd 0.5.0`, `rstest-bdd-macros 0.5.0`, `rstest-bdd-patterns 0.5.0`,
  and `rstest-bdd-policy 0.5.0`. Impact: the survey's negative result was
  accurate when taken and was overtaken by later work on `main`; the plan's
  later rebases re-measured its gate figures but did not re-check this claim,
  so the false version survived into publication. The lesson is that a rebase
  invalidates evidence about the repository as well as counts about the tree.

- Observation: the same string does appear in tracked files, which is what makes
  the premise look plausible at a glance. Evidence: `AGENTS.md:282` says new
  features are validated "using `rstest` and `rstest-bdd`", and
  `docs/developers-guide.md:394` describes an sccache A/B comparison against
  "Whitaker, rstest-bdd, and Netsuke". Both are statements about *other*
  repositories' practices. Impact: a keyword-only survey would have produced an
  invented adoption task.

- Observation: the repository's larger BDD layer is Python `pytest-bdd`, a
  different framework with a different release cadence. Evidence: `.feature`
  files under `tests/features` and `pytest-bdd` in `pyproject.toml`.
  **Corrected 2026-10-25.** The original entry counted "27 `.feature` files
  under `tests/features`, 105 `scenario(...)` call sites across 33 modules under
  `tests/behaviour`", and concluded that this "confirms the Rust BDD framework
  has no consumer here". Two problems. The counts were already stale and do not
  reproduce under any single definition today, so they are dropped rather than
  replaced with differently-scoped figures. More importantly, the inference
  does not follow: Python's `pytest-bdd` assets are unrelated to Rust's
  `rstest-bdd`, and one Rust consumer exists independently of them, in
  `rust/cuprum-rust/src/stream_error_behaviour.rs` with its own
  `tests/features/stream_errors.feature`. A large Python BDD surface is not
  evidence about the Rust framework's adoption either way.

- Observation: the crate named `rstest` that this repository does depend on is
  *not* `rstest-bdd`. Evidence: `rstest 0.27.0` from crates.io depends on
  `rstest_macros`, `futures-timer`, and `futures-util`. The 14
  `#[from(pipe)]`-style attributes under `rust/` are plain `rstest` fixture
  selection, and each such file imports `use rstest::{fixture, rstest}`.
  Impact: these sites are not migration targets and must not be edited.

- Observation: tag `v0.6.0` is a lightweight tag and resolves exactly to the
  commit named in the request. Evidence: `git rev-parse v0.6.0^{commit}` yields
  `72fb22635670e456545ca368805ba4c1c9d7bd69`, identical to the request. No
  annotated tag object exists to dereference differently. Impact: no
  substitution was needed and none was made.

- Observation: the repository's Markdown formatter does not perturb the imported
  documents. Evidence:
  `mdtablefix --wrap --renumber --breaks --ellipsis --fences` over copies of
  both documents left both byte-identical (SHA-256 unchanged, byte counts
  136866 and 43011 unchanged). Impact: byte-for-byte fidelity and the
  `make check-fmt` gate are compatible.

## Decision log

- Decision: treat this as a documentation-import and applicability-analysis
  task, and do not add an `rstest-bdd` dependency. Rationale: the request
  states that a repository with no live dependency should have that reported
  rather than being given an invented adoption task, and explicitly says not to
  introduce BDD where it is not already used. Adding the dependency would also
  be the only candidate for a genuine dependency edit, and it is not needed by
  any existing test. Date/Author: 2026-09-26, migration agent. **Corrected
  2026-10-25:** the last clause is no longer true. `cuprum-rust` already
  depends on `rstest-bdd` 0.5.0 and uses it in one behavioural module, so the
  dependency *is* needed by an existing test. The decision to ship the
  documentation import remains right; the decision to treat the version move as
  unnecessary does not, and is replaced by the blocked-migration record below.

- Decision: import `docs/v0-5-0-migration-guide.md` alongside the two required
  documents. Rationale: the request requires the intervening guides to be
  inspected when the starting version precedes 0.5.0, and the v0.5.0 guide is
  the one immediately below the target release. Importing it preserves that
  inspection for a future reader. Date/Author: 2026-09-26, migration agent.

- Decision: record the upstream relative-link mapping in the provenance document
  instead of copying link targets into `docs/`. Rationale: one target,
  `developers-guide.md`, collides with this repository's own
  `docs/developers-guide.md`, which documents unrelated Cuprum workflows. The
  upstream ADR numbers also occupy a numbering space independent of this
  repository's, which already defines `ADR-006` and `ADR-007` for unrelated
  decisions. Copying upstream files under colliding names would place two
  different documents at the same identity. Date/Author: 2026-09-26, migration
  agent. **Amended 2026-10-15:** the request later required every relative link
  to be made absolute, which removes the collision hazard at its source and
  supersedes the "do not rewrite links" half of this decision. The targets are
  still not vendored.

- Decision: keep local migration notes outside the imported copies.
  Rationale: explicitly required, and it is what allows the imported text to
  stay pristine. Date/Author: 2026-09-26, migration agent.

- Decision: index the four imported documents in `docs/contents.md` under a
  dedicated "Vendored upstream documentation" heading, rather than under the
  project's own guidance. Rationale: after the rebase onto `origin/main`,
  `docs/contents.md` was the canonical index for every other top-level document
  in `docs/`, and these four were the only ones absent from it. A separate
  heading keeps the upstream status and the absence of any dependency visible,
  so a reader does not mistake imported text for Cuprum's own guidance.
  Date/Author: 2026-10-19, migration agent.

- Decision: re-measure the "Documentation gates" evidence at each rebase
  boundary instead of carrying forward the counts captured at import time.
  Rationale: the counts in that table describe a tree that no longer exists once
  `main` advances, and a plan that states 82 Markdown files while the gate
  reports 101 is inaccurate in exactly the way `main`'s commit `87d38aff8`
  corrects elsewhere. The same reasoning retired the "recorded as not run"
  paragraph: the rebase instruction requires `make test`, `make typecheck` and
  `make lint` for every merge, so the plan now records their results and
  describes them as confirming the rebase rather than as evidence about the
  imported text. Date/Author: 2026-10-19, migration agent.

- Decision: describe the `ruff format` total as "files already formatted"
  rather than "Python files already formatted", and state how it is derived.
  Rationale: the third rebase moved the figure from 1008 to 1010, and asking
  why it moved exposed the wrong noun. `ruff format --check` over `.md` alone
  reports 101 files and over `.py` alone reports 909, and 909 + 101 = 1010,
  because the formatter also formats the Python fenced code blocks inside
  Markdown. The earlier figures decompose the same way at their own bases: 595
  and 82 gave 677, and 907 and 101 gave 1008. The arithmetic was therefore
  sound and only the label was wrong. The digits are corrected for the current
  tree and the derivation is recorded, so a later reader can re-check the total
  against a tree that has changed again, which a bare count cannot.
  Date/Author: 2026-10-19, migration agent.

## Outcomes & retrospective

The repository is left holding the authoritative `rstest-bdd` v0.6.0
documentation with a provenance record and a search-backed applicability
matrix. The imported prose is upstream text verbatim; the only divergence is
the cross-file link rewriting recorded in the provenance document. No
dependency, manifest, lockfile, source file, test, or gate was changed.
**Corrected 2026-10-25:** the original text justified that by asserting the
repository has no `rstest-bdd` consumer. It has one, so the absence of a
manifest change is not a finding of "nothing to do" but an unfinished migration
whose blocker lies outside this repository. See
[Blocked migration](#blocked-migration).

Compared against the original purpose, the delivered change is smaller than the
request's framing implies and smaller than a version edit: the central
migration action — raising the dependency to 0.6.0 and proving the consumer
still passes — is not performed here. The consumer uses no API that 0.6.0
breaks, so the work it would need is small; the version literal and the
resulting proof run are what is missing. What the change does deliver is a
documentation set, an applicability analysis, and the evidence for why the
version move cannot land in this repository at present.

What would be done differently: the decision-relevant facts here are cheap to
obtain (`cargo metadata`, a lockfile grep, and `git log -S`) and should be
re-gathered, not just re-read, at every rebase. The original survey took them
correctly on 2026-09-26 and they were overtaken on 2026-10-12; three subsequent
rebases refreshed the plan's gate counts but carried the dependency claim
forward unexamined, which is how a true finding became a false published one.
Re-measuring a claim is not the same as re-reading it.

Residual gaps, recorded rather than hidden:

1. The version migration is not performed. One consumer exists and is listed in
   [Blocked migration](#blocked-migration); the blocker is the MSRV floor that
   `rstest-bdd` 0.6.0 requires.
2. The imported documents' cross-file links point at upstream files that are
   deliberately not vendored here, so the targets resolve on GitHub but not
   inside this checkout. The links are absolute, so they cannot misdirect a
   reader to a same-named local document; the
   [provenance record](rstest-bdd-v0-6-0-import-provenance.md#files-not-vendored)
   states why each omitted file is omitted.
3. Consumers that *do* need this migration are `leynos/whitaker` and
   `leynos/netsuke`, both pinned at `rstest-bdd = "0.5.0"`. They are outside
   this repository's scope and were not modified.

## Premise correction (2026-10-25)

The request's premise was that this repository has existing `rstest-bdd`
consumers. The survey of 2026-09-26 found none, and that finding was correct at
the time. It was overtaken on 2026-10-12 by `d79dded2a`, and the plan was
republished on a base that already contained that commit without the claim
being re-checked.

The corrected position, re-measured at `40d56d69f`:

| Claim as originally published                                   | Status now                                                                                                                                                                   |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No `rstest-bdd` package in the dependency graph                 | False. `cargo metadata` resolves `rstest-bdd`, `rstest-bdd-macros`, `rstest-bdd-patterns`, and `rstest-bdd-policy`, all 0.5.0.                                               |
| No manifest or lockfile in any commit ever contained the string | False. `git log --all -S "rstest-bdd" -- '*.toml' '**/Cargo.lock'` returns five commits, the earliest relevant one being `d79dded2a`.                                        |
| The only tracked mentions are prose about other repositories    | Partly false. `rust/cuprum-rust/Cargo.toml:31-32` declare the two crates, `stream_error_behaviour.rs` imports them, and a `.feature` file is bound by them.                  |
| No consumers of the framework to migrate                        | False. `rust/cuprum-rust/src/stream_error_behaviour.rs` is one consumer, with `rust/cuprum-rust/tests/features/stream_errors.feature` bound by six `#[scenario]` attributes. |
| The detection method is sound (positive control on other repos) | Still true, and now also demonstrated positively here. The method was never broken; the observation simply expired.                                                          |

The documentation import, the provenance record, and the link rewriting are
unaffected by this correction and remain as delivered. In the applicability
matrix, the MSRV and feature-file rows are now blocking or applicable, and the
`StepResult` row now demands an audit, because a consumer exists. The remaining
rows still hold, because the 0.6.0 breaking changes they search for are absent
from this tree. The consumer uses `Slot`, `StepResult`, and the
`#[given]`/`#[when]`/`#[then]`/`#[scenario]` attributes. `Slot` is not touched
by 0.6.0 at all, and the `StepResult` exposure is a bare alias rather than the
local `Result<T, E>` alias the change targets.

## Blocked migration

The version move cannot be completed in this repository without an external
decision, and the reason is a hard MSRV floor rather than effort or priority.

- `rstest-bdd` 0.6.0 declares `rust-version = "1.88"`. Verified with
  `cargo info rstest-bdd@0.6.0`, which reports `rust-version: 1.88`; the
  upstream migration guide's "MSRV is now 1.88" entry says the same, and
  attributes it to the `gherkin 0.16` parser dependency, which independently
  declares `rust-version = 1.88`.
- This workspace pins `rust-version = "1.85.0"` in `rust/Cargo.toml` and
  `channel = "1.85.0"` in `rust/rust-toolchain.toml`, and `make msrv-check`
  compiles every target on that toolchain. `msrv-check` is a standalone target
  rather than a prerequisite of `make lint`; it runs as its own step in the
  `.github/workflows/ci.yml` `lint-test` job, and `lint-test` is a required
  status context on `main`. The gate is therefore still a merge blocker, but it
  arrives through the job step, not through the lint aggregate.
- Raising the floor is therefore not a local edit. It changes the published
  minimum for every consumer of all three crates and is recorded in
  `rust/Cargo.toml`, `rust/rust-toolchain.toml`, the `MSRV_TOOLCHAIN` variable
  in the `Makefile`, and the prose in `AGENTS.md`,
  `docs/adr-011-audited-rust-boundaries.md`, and `docs/developers-guide.md`
  that states 1.85.0 is the published floor.

The code-level migration is small: the single consumer uses no API touched by
the 0.6.0 breaking changes, so the likely edit is the two version literals plus
whatever the resolver reports. The gating question is policy, not code, and it
belongs to the repository owner. Until it is answered, the correct state of
this plan is "documentation delivered, dependency migration blocked", which is
what the status line above records.

## Retrospective on the request's premise

The request describes a repository with existing `rstest-bdd` consumers.
**Corrected 2026-10-25:** it is that repository after all, from `d79dded2a`
onwards. The three lines of evidence in the original text — no `bdd`-named
package, no manifest ever containing the string, only prose mentions — each
held on 2026-09-26 and each is now false; see
[Premise correction](#premise-correction-2026-10-25) for the re-measurement
against `40d56d69f`.

The durable lesson is not about the survey method, which was sound and had a
positive control. It is that a negative result about the *repository* decays as
the repository changes, exactly like a count about the tree, and this plan
treated it as a fixed finding across three rebases that re-measured everything
else. A rebase onto a new base re-opens every factual claim, not only the
numeric ones.

## Applicability matrix

Every breaking change listed in the v0.6.0 migration guide is assessed below.
Each row names the search that produced its verdict, so a reader can re-run the
search rather than trust the count. The searches cover `*.rs` and `*.toml`
across the tracked tree.

| Breaking change                                                                                          | Search pattern                                                                       | Matches | Verdict                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Implicit fixture names normalize one leading underscore                                                  | `#[from(`                                                                            | 15      | Not applicable. All 15 are plain `rstest` fixture selection in `rust/cuprum-native-io/src/ownership_tests.rs`, `rust/cuprum-streams/src/io_utils/tests.rs`, and `rust/cuprum-streams/src/splice/tests.rs`; each file imports `rstest`, not `rstest_bdd`. Not an `rstest-bdd` macro argument.                                                                                                                                                                                                                                                                                                    |
| `runtime = "tokio-current-thread"` is a deprecated alias                                                 | `tokio-current-thread`                                                               | 0       | Not applicable.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Custom `HarnessAdapter` must return `HarnessResult<T>`                                                   | `HarnessAdapter`                                                                     | 0       | Not applicable. No harness is implemented or consumed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `StepContext::insert_value` returns `InsertOutcome`                                                      | `insert_value`                                                                       | 0       | Not applicable. No direct runtime API caller.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Step return aliases of `Result<T, E>` now propagate `Err`                                                | `StepResult`                                                                         | 13      | **Audit required on adoption.** `stream_error_behaviour.rs` defines 14 step functions, of which the 3 `#[when]` and 7 `#[then]` steps return `StepResult<(), String>`; the 4 `#[given]` steps return `()`. The 13 matches are those 10 signatures, the `recorded` helper's signature, the `use` line, and one doc-comment mention. 0.5.0 already recognizes a bare `StepResult` path, so these classify as `ResultUnit` and propagate today. The change targets *local aliases* of `Result<T, E>`, which this module does not define; the audit is to confirm that still holds after upgrading. |
| Feature paths are manifest-relative                                                                      | `ScenarioMetadata`                                                                   | 0       | Not applicable. No report, JSON, JUnit, or `cargo bdd` consumer.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Rust indexing returns `RustStepIndexResult`; `index_feature_file` and `publish_rust_diagnostics` removed | `RustStepIndex`, `index_feature_file`, `index_rust_file`, `publish_rust_diagnostics` | 0       | Not applicable. No language-server integration.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `record_bypassed_steps` takes a `BypassedScenario`                                                       | `record_bypassed_steps`, `BypassedScenario`                                          | 0       | Not applicable. No direct bypass-reporting caller.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `find_feature_files` returns `Result<Vec<PathBuf>, ServerError>`                                         | `find_feature_files`                                                                 | 0       | Not applicable. No discovery caller.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| MSRV is now 1.88                                                                                         | `rust-version`                                                                       | 4       | **Blocking.** The four hits are this workspace's own `rust-version = "1.85.0"` declarations, but `rstest-bdd` 0.6.0 itself requires 1.88, so adopting it raises this repository's published floor. See [Blocked migration](#blocked-migration).                                                                                                                                                                                                                                                                                                                                                 |
| Feature-file rebuild invalidation; `compile_error!` on a path sharing no filesystem root                 | `*.feature` under `rust/`                                                            | 1       | **Applicable on adoption.** The Python `pytest-bdd` assets under `tests/features` are unbound, but `rust/cuprum-rust/tests/features/stream_errors.feature` is bound by six `#[scenario]` attributes, so the manifest-relative path rule and rebuild-invalidation change apply to it.                                                                                                                                                                                                                                                                                                            |
| Harness dependency matrix (which crates to add)                                                          | `compile-time-validation`, `strict-compile-time-validation`                          | 0       | Not applicable. No `rstest-bdd` feature selection exists to preserve. The workspace enables no `rstest-bdd` features; `cuprum-rust` declares only its own `loom-defect-fixture`.                                                                                                                                                                                                                                                                                                                                                                                                                |

The `v0.6.1 beta` (`#[harness_context]`) and `v0.7.0` (guard-based
`StepContext` borrowing) sections in the imported text are out of scope by the
version boundary recorded in the provenance document. Neither is implemented.

## Context and orientation

Cuprum is a Python package with an optional Rust extension. All Rust lives in a
single Cargo workspace rooted at `rust/Cargo.toml` with three members:
`rust/cuprum-rust`, `rust/cuprum-streams`, and `rust/cuprum-native-io`. There
is one lockfile, `rust/Cargo.lock`. There are no Cargo manifests outside
`rust/`, no submodules, no vendored Rust, and no `[patch]` sections.

The workspace declares `rust-version = "1.85.0"`, pinned in
`rust/rust-toolchain.toml` at channel `1.85.0`. Formatting uses a separate
nightly pin (`RUSTFMT_TOOLCHAIN ?= nightly-2026-05-28`), and Linux debug work
uses a third pin (`DEV_FAST_TOOLCHAIN ?= nightly-2026-08-23`). The Rust
dependency surface is `pyo3`, `thiserror`, `tracing`, `libc`, `cap-std`, and
`windows-sys` in production, with `proptest`, `rstest`, `insta`, `trybuild`,
`loom`, and `tempfile` for tests.

`AGENTS.md` is the repository instructions file. It requires the documentation
style guide, the ExecPlan process, and the gate commands `make check-fmt`,
`make lint`, `make test`, `make markdownlint`, and `make nixie`. Relevant
targets are defined in the root `Makefile`.

Terms used in this plan. *Applicability matrix* means a table mapping each
breaking change in an upstream release to the evidence of whether this
repository is exposed to it. *Byte-for-byte* means the file's SHA-256 digest
equals the digest of the freshly written text, so no trailing newline,
whitespace, or encoding change crept in.

Conformance basis is recorded below.

## Conformance basis

No Terms of Reference, technical design, or ADR in this repository governs a
`rstest-bdd` migration; on 2026-09-26 the repository had no `rstest-bdd`
dependency. There is therefore no upstream artefact to trace to and none is
invented. **Corrected 2026-10-25:** the dependency now exists, so this
conformance basis describes the import alone and does not discharge the
migration, which is blocked on the MSRV decision recorded in
[Blocked migration](#blocked-migration).

The governing documents for this work are the repository's own `AGENTS.md` and
`docs/documentation-style-guide.md`, plus the migration request itself. The
external authority is upstream commit
`72fb22635670e456545ca368805ba4c1c9d7bd69` of `leynos/rstest-bdd`.

## Verification plan

This change alters documentation only. It introduces no runtime behaviour, no
public interface, no persistent format, and no concurrency. It therefore
introduces no non-trivial invariant or lemma, and no property, model-checking,
or proof obligation arises. Stating that explicitly is required; the
verification below is nonetheless substantive and is aimed at the claims that
this plan actually makes.

The claims requiring evidence are:

1. The imported files are byte-identical to the upstream release.
2. The repository's `rstest-bdd` exposure is known and the migration status is
   recorded. **Corrected 2026-10-25:** the original claim read "the repository
   has no `rstest-bdd` consumer to migrate", which is false. There is one
   consumer, at `rust/cuprum-rust/src/stream_error_behaviour.rs`, and the
   migration is blocked on the MSRV floor recorded in
   [Blocked migration](#blocked-migration).
3. The imported files pass this repository's documentation gates.
4. The applicability matrix is accurate.

For claim 1, the method is a checksum comparison against the blob read from the
immutable commit, not against a working tree or a branch. The artefact is the
SHA-256 table in the provenance record. The discharge condition is exact digest
equality for all three files. Non-vacuity is addressed by the digests having
been computed from a freshly fetched clone at the resolved tag and compared
against an independently written copy; a stale or wrong source would produce a
different digest.

For claim 2, the method is exhaustive search over every manifest, every
lockfile, the full commit graph, and the resolved dependency graph. The
negative result is corroborated by a positive control: the identical search
procedure applied to `leynos/whitaker` and `leynos/netsuke` returns
`rstest-bdd = "0.5.0"` in both, proving the method detects the dependency when
it is present. A negative result without a positive control would be
uninformative, so this is the non-vacuity check for claim 2.

For claim 3, the method is to run the repository's own gates over the imported
files and capture the output. The commands are `make markdownlint`,
`make check-fmt`, and `make spelling`. The discharge condition is a clean run.
Non-vacuity is addressed by the formatter measurement recorded under Surprises,
which shows the formatter is capable of being detected as a no-op rather than
merely assumed to be one.

For claim 4, the method is to derive every row from a concrete search over the
repository for the API named by the upstream change, with the search pattern
and the match count recorded. A row claiming "not applicable" is only credible
if the search that produced it is stated.

Axioms. The import assumes that upstream commit
`72fb22635670e456545ca368805ba4c1c9d7bd69` is the authoritative v0.6.0 source,
and that the tag will not be moved. The first was verified by resolving the tag
directly; the second is outside this repository's control and is why the commit
SHA, not the tag, is the recorded identity. The gates assume the toolchain pins
in the `Makefile` behave as they do on `origin/main`, which is unchanged by
this work.

### Verification plan revision

- (2026-09-26) Initial version.

## Documentation gates

The following commands are run over the finished change, sequentially, with
output captured under `/tmp`:

    make markdownlint
    make check-fmt
    make spelling

`make markdownlint` runs `markdownlint-cli2` and then `make spelling`.
`make check-fmt` runs `ruff format --check`,
`cargo +nightly-2026-05-28 fmt --all -- --check`, and `mdtablefix --check`
again.

Results, all captured after the final formatting pass:

| Gate                | Exit | Evidence                                                                                                        |
| ------------------- | ---- | --------------------------------------------------------------------------------------------------------------- |
| `make markdownlint` | 0    | 101 files linted, 0 issues; spelling gate clean.                                                                |
| `make check-fmt`    | 0    | 1010 files already formatted; `cargo fmt --check` clean; `mdtablefix --check` reports 101 files left unchanged. |

Both totals are the gates' own units, not a count of files of one type. The
`markdownlint` total equals the tree's 101 tracked Markdown files. The
`ruff format` total is the number of *Python and Markdown* files, because the
formatter also formats the Python fenced code blocks inside Markdown. The tree
holds 909 of the former and 101 of the latter, which is the observed 1010.
Earlier revisions labelled this total "Python files"; the number was right and
the noun was wrong. That mislabel is corrected here, and the derivation is
recorded so a later reader can re-check the total against a tree that has
changed again.

The same decomposition holds at every base this plan has recorded. At the
original base `991dee642` the tree held 595 Python files and 77 Markdown files,
plus the 5 imported documents: 595 plus 82 gives 677, the figure first
recorded. At `87d38aff8` the tree held 907 Python files and 101 Markdown files:
907 plus 101 gives 1008. Both earlier numbers were therefore correct, and the
only defect was the label placed on them.

Two en-GB spelling corrections were applied to this plan's own prose because
the gate's correction table flags them: an Oxford "-ise" verb stem, and a noun
spelled the United States way. Quoting the offending tokens here would itself
trip the gate, so they are described rather than repeated. The imported
upstream documents required no correction at all: they passed the spelling gate
on their first run, which is further evidence that the upstream prose already
conforms to this repository's spelling policy.

**Amended 2026-10-19, after the rebase onto `origin/main` at `87d38aff8`.** The
original text here recorded `make test`, `make typecheck` and `make lint` as
not run, on the grounds that a documentation-only change has no behavioural
surface. That reasoning was overridden by the rebase instruction, which
requires all four gates to validate the merge, so they were run and are
recorded rather than claimed.

`make test`, `make typecheck` and `make lint` all exit 0. The counts are
deliberately not reproduced here: they belong to the run's log, and `main`'s
Markdown-gate changes show how quickly a copied figure drifts from the tree it
describes. The gate that matters for this branch's correctness is the Markdown
set above; the rest confirm that the rebase did not disturb code the branch
never touched.

The dependency facts are unchanged: no `Cargo.toml`, `Cargo.lock`, Python
module, or gate definition is modified by this branch.

**Amended 2026-10-25, after the fourth rebase onto `origin/main` at `40d56d69f`
and the premise correction.** All six gates were re-run against the corrected
tree, in order: `make fmt`, `make markdownlint` (104 files, 0 issues, spelling
clean), `make check-fmt` (1094 files formatted, `cargo fmt --check` clean,
`mdtablefix --check` reports 104 unchanged), `make test` (4255 passed, 96
skipped, 0 failed; nextest 167/167; doctests clean), `make typecheck`, and
`make lint`. Each gate exits 0.

One host condition required care and is recorded rather than passed over. An
unrelated `/tmp/pyproject.toml` on this machine belongs to another project and
is not part of this repository. `ty` walks upward from its working directory,
and `cuprum/unittests/test_sh_typing_contract.py` runs it with `cwd` set to a
pytest `tmp_path` under `/tmp`, so the stray file became the inferred project
root and `ty` scanned the whole of `/tmp`. Ten tests in that one module timed
out at the pytest 30-second limit on the first run. Re-running the same module
with `TMPDIR` set outside `/tmp` passed all 11 in 0.69 seconds, and the full
suite then passed with `TMPDIR` likewise redirected. The failure is a property
of the host's `/tmp`, not of this branch, whose diff is six Markdown files; the
redirect isolates the artefact without changing the repository or the gate.
