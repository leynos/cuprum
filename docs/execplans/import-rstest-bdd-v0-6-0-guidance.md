# Migrate Cuprum to the published rstest-bdd v0.6.0 release

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE

## Purpose / big picture

The migration request asks for this repository to be moved onto the published
`rstest-bdd` v0.6.0 release, with the migration implemented and validated
rather than merely recommended.

The survey performed before planning establishes that **this repository does
not depend on `rstest-bdd` at all**, in any Cargo manifest or lockfile, at any
point in its recorded history. It therefore has no consumers of the framework
to migrate. What remains of the request is the part that is real and valuable:
the authoritative documentation import, a recorded applicability analysis, and
the provenance trail that lets a future reader confirm both.

The user-visible outcome is a documentation set in `docs/` that carries the
upstream `rstest-bdd` v0.6.0 user guide and migration guides, byte-for-byte
identical to the upstream release, with a provenance record and an
applicability matrix stating precisely which v0.6.0 breaking changes this
repository is exposed to and which are inapplicable. A reader can verify the
import with the recorded checksums and can see, stated in one place, why no
dependency edit was correct.

## Constraints

Hard invariants that must hold throughout implementation.

- The imported upstream documents in `docs/` must remain byte-for-byte
  identical to the blobs at upstream commit
  `72fb22635670e456545ca368805ba4c1c9d7bd69`. No reformatting, no link
  rewriting, no local annotation inside those files.
- No dependency may be added that the repository does not need. Introducing an
  `rstest-bdd` dependency into a repository that has no BDD consumers would be
  an invented adoption task, not a migration.
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
      dependency.
- [x] (2026-09-26 20:05Z) Fetched upstream, resolved tag `v0.6.0`, and compared
      it against the commit named in the request.
- [x] (2026-09-26 20:08Z) Proved the absence of an `rstest-bdd` dependency
      across all manifests, all lockfiles, all history, and `cargo metadata`,
      with a positive control.
- [x] (2026-09-26 20:10Z) Read the v0.6.0 and v0.5.0 migration guides in full.
- [x] (2026-09-26 20:12Z) Imported the three documents byte-for-byte and
      verified their checksums against the immutable commit.
- [x] (2026-09-26 20:14Z) Wrote the provenance record.
- [x] (2026-09-26 20:20Z) Wrote the applicability matrix, deriving every row
      from a concrete search with its pattern and match count recorded.
- [x] (2026-09-26 20:26Z) Ran the documentation gates; `make markdownlint` and
      `make check-fmt` both pass, and the imported files kept their exact
      digests through every formatting pass.
- [ ] Commit the change.
- [ ] (Optional, not started) Open a pull request.

Remaining after the commit: the pull request, if the session's workflow asks
for one. No implementation work remains.

## Surprises & discoveries

- Observation: this repository has no `rstest-bdd` dependency, and never had
  one. Evidence: `git log --all -S "rstest-bdd" -- '*.toml' '**/Cargo.lock'`
  returns no commit that ever introduced the string into a manifest or lockfile;
  `cargo metadata` lists no package or dependency whose name contains `bdd`;
  the two `bdd` substrings in `rust/Cargo.lock` are coincidental hex inside
  checksums. Impact: the request's premise is false for this repository, so the
  migration reduces to the documentation import plus applicability analysis.

- Observation: the same string does appear in tracked files, which is what makes
  the premise look plausible at a glance. Evidence: `AGENTS.md:260` says new
  features are validated "using `rstest` and `rstest-bdd`", and
  `docs/developers-guide.md:358` describes an sccache A/B comparison against
  "Whitaker, rstest-bdd, and Netsuke". Both are statements about *other*
  repositories' practices. Impact: a keyword-only survey would have produced an
  invented adoption task.

- Observation: the repository's actual BDD layer is Python `pytest-bdd`, a
  different framework with a different release cadence. Evidence: 27 `.feature`
  files under `tests/features`, 105 `scenario(...)` call sites across 33
  modules under `tests/behaviour`, and `pytest-bdd` in `pyproject.toml`.
  Impact: confirms the Rust BDD framework has no consumer here.

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
  any existing test. Date/Author: 2026-09-26, migration agent.

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
  different documents at the same identity, and rewriting the imported links
  would violate byte-for-byte fidelity. Date/Author: 2026-09-26, migration
  agent.

- Decision: keep local migration notes outside the imported copies.
  Rationale: explicitly required, and it is what allows the imported text to
  stay pristine. Date/Author: 2026-09-26, migration agent.

## Outcomes & retrospective

The repository is left holding the authoritative `rstest-bdd` v0.6.0
documentation, byte-for-byte, with a provenance record and a search-backed
applicability matrix. No dependency, manifest, lockfile, source file, test, or
gate was changed, because none needed to be: the repository has no `rstest-bdd`
consumer, and the request explicitly excludes inventing one.

Compared against the original purpose, the delivered change is smaller than the
request's framing implies and larger than a version edit. It is smaller because
the central migration action — moving consumers off a breaking API — has no
consumers to move. It is larger because the request's real risk was a false
premise, and the durable value is the evidence that the premise was tested
rather than assumed.

What would be done differently: little, at the level of process. The one
efficiency lesson is that the decision-relevant facts here are cheap to obtain
(`cargo metadata`, a lockfile grep, and `git log -S`) and should be gathered
before anything else. A survey that starts from a grep for the framework name
will find the policy statements in `AGENTS.md` and `docs/developers-guide.md`
first and may mistake them for a dependency; starting from the dependency graph
avoids that trap entirely.

Residual gaps, recorded rather than hidden:

1. There is no live migration to validate, so the request's validation clauses
   (feature-file inventories, scenario-count comparisons, async suspension
   checks, harness-adoption checks) have no subject in this repository. They
   are not satisfied; they are inapplicable, and the applicability matrix
   states why for each.
2. The imported documents' five relative links are not locally resolvable, as
   documented. This is a deliberate consequence of byte-for-byte fidelity.
3. Consumers that *do* need this migration are `leynos/whitaker` and
   `leynos/netsuke`, both pinned at `rstest-bdd = "0.5.0"`. They are outside
   this repository's scope and were not modified.

## Retrospective on the request's premise

The request describes a repository with existing `rstest-bdd` consumers. This
repository is not that repository. Three independent lines of evidence agree:
the dependency graph contains no `bdd`-named package, no manifest or lockfile
in any commit ever contained the string, and the only tracked mentions are
prose about other projects' practices. The same detection procedure returns a
positive result against `leynos/whitaker` and `leynos/netsuke`, which is what
rules out a broken method as the explanation.

The request anticipates this outcome: it directs that a repository with no live
dependency be reported rather than given an invented adoption task. The
documentation import, the applicability matrix, and this record are the parts
of the request that have real subject matter here, and they are done.

## Applicability matrix

Every breaking change listed in the v0.6.0 migration guide is assessed below.
Each row names the search that produced its verdict, so a reader can re-run the
search rather than trust the count. The searches cover `*.rs` and `*.toml`
across the tracked tree.

| Breaking change                                                                                          | Search pattern                                                                       | Matches | Verdict                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Implicit fixture names normalize one leading underscore                                                  | `#[from(`                                                                            | 14      | Not applicable. All 14 are plain `rstest` fixture selection in `rust/cuprum-native-io/src/ownership_tests.rs`, `rust/cuprum-streams/src/io_utils/tests.rs`, and `rust/cuprum-streams/src/splice/tests.rs`; each file imports `use rstest::{fixture, rstest}`. Not an `rstest-bdd` macro argument. |
| `runtime = "tokio-current-thread"` is a deprecated alias                                                 | `tokio-current-thread`                                                               | 0       | Not applicable.                                                                                                                                                                                                                                                                                   |
| Custom `HarnessAdapter` must return `HarnessResult<T>`                                                   | `HarnessAdapter`                                                                     | 0       | Not applicable. No harness is implemented or consumed.                                                                                                                                                                                                                                            |
| `StepContext::insert_value` returns `InsertOutcome`                                                      | `insert_value`                                                                       | 0       | Not applicable. No direct runtime API caller.                                                                                                                                                                                                                                                     |
| Step return aliases of `Result<T, E>` now propagate `Err`                                                | `StepResult`                                                                         | 0       | Not applicable. No `rstest-bdd` step functions exist.                                                                                                                                                                                                                                             |
| Feature paths are manifest-relative                                                                      | `ScenarioMetadata`                                                                   | 0       | Not applicable. No report, JSON, JUnit, or `cargo bdd` consumer.                                                                                                                                                                                                                                  |
| Rust indexing returns `RustStepIndexResult`; `index_feature_file` and `publish_rust_diagnostics` removed | `RustStepIndex`, `index_feature_file`, `index_rust_file`, `publish_rust_diagnostics` | 0       | Not applicable. No language-server integration.                                                                                                                                                                                                                                                   |
| `record_bypassed_steps` takes a `BypassedScenario`                                                       | `record_bypassed_steps`, `BypassedScenario`                                          | 0       | Not applicable. No direct bypass-reporting caller.                                                                                                                                                                                                                                                |
| `find_feature_files` returns `Result<Vec<PathBuf>, ServerError>`                                         | `find_feature_files`                                                                 | 0       | Not applicable. No discovery caller.                                                                                                                                                                                                                                                              |
| MSRV is now 1.88                                                                                         | `rust-version`                                                                       | 4       | Not applicable. The four hits are this workspace's own `rust-version = "1.85.0"` declarations. Nothing in the graph requires 1.88, because no `rstest-bdd` package is present, so the MSRV stays at 1.85.0. Raising it would advertise a floor no consumer needs.                                 |
| Feature-file rebuild invalidation; `compile_error!` on a path sharing no filesystem root                 | `*.feature` under `rust/`                                                            | 0       | Not applicable. All 27 `.feature` files are Python `pytest-bdd` assets under `tests/features` and are not bound by any Rust macro.                                                                                                                                                                |
| Harness dependency matrix (which crates to add)                                                          | `compile-time-validation`, `strict-compile-time-validation`                          | 0       | Not applicable. No feature selection to preserve, because none exists.                                                                                                                                                                                                                            |

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
`rstest-bdd` migration, because the repository has no `rstest-bdd` dependency.
There is therefore no upstream artefact to trace to and none is invented.

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
2. The repository has no `rstest-bdd` consumer to migrate.
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

| Gate                | Exit | Evidence                                                                                                             |
| ------------------- | ---- | -------------------------------------------------------------------------------------------------------------------- |
| `make markdownlint` | 0    | 82 files linted, 0 errors; spelling gate clean.                                                                      |
| `make check-fmt`    | 0    | 677 Python files already formatted; `cargo fmt --check` clean; `mdtablefix --check` reports 82 files left unchanged. |
| `make spelling`     | 0    | `typos-config-builder` gate reports no findings and no `typos.toml` churn.                                           |

Two en-GB spelling corrections were applied to this plan's own prose because
the gate's correction table flags them: an Oxford "-ise" verb stem, and a noun
spelled the United States way. Quoting the offending tokens here would itself
trip the gate, so they are described rather than repeated. The imported
upstream documents required no correction at all: they passed the spelling gate
on their first run, which is further evidence that the upstream prose already
conforms to this repository's spelling policy.

`make test` and the Rust gates are not affected by a documentation-only change
and are recorded as not run, rather than claimed as passing. No `Cargo.toml`,
`Cargo.lock`, Python module, or gate definition was modified, so there is no
behavioural surface for those suites to exercise.
