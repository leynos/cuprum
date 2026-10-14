# Architectural decision record (ADR) 022: Adopt a nose code-duplication gate

## Status

Accepted on 2026-09-27. Cuprum adopts nose 0.20.0 behind
`scripts/duplication_gate.py` as a blocking code-duplication gate in
`make lint`, with reasoned location-keyed exceptions in
`[tool.duplication_gate]`.

## Date

2026-09-27.

## Context and Problem Statement

Cuprum has no automated defence against duplicated logic. Two modules can
acquire the same helper, or the same context-manager exit body can be copied
seven times, and nothing fails until a reviewer notices or a fix lands in only
one copy. The repository already runs a blocking dead-code scan (Skylos) over
the production package, so the gap is specifically the opposite failure: code
that exists in several places at once.

A sibling repository, `leynos/episodic`, selected a detector for exactly this
problem and recorded the decision in its ADR-021. Re-deriving that choice here
would repeat a benchmark that has already been run, and Cuprum has no evidence
that would change the outcome on a different corpus. What Cuprum does need is
its own adjudication: which families in *this* package are genuine duplication
to extract, and which are intentional parallels that should stay reviewable.

## Decision Drivers

- Enforce duplication detection automatically rather than by reviewer
  attention, so a regression fails the build instead of surviving review.
- Reuse an already-benchmarked tool decision by reference instead of rerunning
  a detector competition, and do not claim the reference's precision, recall or
  timing measurements as Cuprum's own.
- Keep every exception reasoned, per-location, reviewable in a diff, and
  reportable when it stops covering anything.
- Keep the gate's tooling isolated from the application: it must not require
  the application to be installed, the native extension to be built, or any
  application service to be running.
- Keep Cuprum's Python floor, interpreter matrix, and runtime dependencies
  unchanged.
- Never obtain a green build by lowering the enforced surface.

## Options Considered

1. **Adopt nose 0.20.0 behind a wrapper, with reasoned allow entries.** The
   option taken. Deterministic across runs with no hash-seed or interpreter
   pin, fast enough to sit inside `make lint`, and it reports span families
   that key naturally onto locations rather than line numbers.

2. **Adopt PyChase.** The reference's incumbent. It matches nose on
   Types 1–3 and reports qualified unit names that make allow keys more
   precise, but it costs a separate Python 3.13 environment and a fixed
   `PYTHONHASHSEED` to keep its locality-sensitive hashing stable, and it is
   roughly two orders of magnitude slower on the reference's package. Name
   precision is the only capability lost, and the `::name` suffix recovers most
   of it.

3. **Adopt pyscn for Type-4 (semantic) clone analysis, even behind an
   allowlist.** Rejected in the reference after measurement: at Episodic scale
   its semantic lane reported 692 Type-4 pairs at permissive settings, and the
   18 that survived at gate strength were adjudicated as intentional idiom
   parallels. Its ranking is inverted where it matters — the corpus's only true
   Type-4 pair scored below control pairs — so no threshold admits the true
   clone while excluding the false parallels.

4. **Use nose's native baseline and ignore files instead of reasoned
   entries.** A baseline records *what* was duplicated, not *why* it may stay.
   Reasoned entries keep every exception attached to a justification,
   reviewable in a diff, and removable when the gate reports it stale.

5. **Extract every reported family.** Rejected in principle: some families are
   deliberate parallels (independent wire-format declarations, per-channel
   observer recorders), and collapsing them would manufacture a generic
   utility, an inheritance hierarchy, or a cross-layer dependency that the
   architecture does not want.

## Decision

In the context of keeping copy-paste duplication out of Cuprum, adopting the
tool-selection decision of `leynos/episodic` ADR-021 at revision
`d9e5ac0d254f375e2986f52d91a3b88c117c833b` (the merged revision of PR #276)
rather than rerunning a detector comparison, the decision is to run nose 0.20.0
behind `scripts/duplication_gate.py` in `make lint`, provisioned by
`make install-nose` into `.tools/nose`, with the version pinned identically in
the Makefile, the CI workflow, and `[tool.nose]` and asserted by
`cuprum/unittests/test_toolchain_pins.py`, and with reasoned location-keyed
exceptions in `[tool.duplication_gate]`, against retaining PyChase, adopting
pyscn's Type-4 analysis even behind an allowlist, replacing reasoned entries
with nose's native baseline, or treating every reported family as duplication
to extract, to achieve deterministic duplication enforcement whose exceptions
stay reviewable in version control, accepting that the gate depends on a
pre-1.0 platform-specific binary published through GitHub releases, that nose
reports spans without qualified unit names so allow keys are path globs with an
optional `::name` suffix, and that the gate adjudicates every reported family
rather than a ranked prefix of them.

## Consequences

- `make lint` and the standalone `make duplication` target depend on
  `make install-nose`, which installs the `nose-cli` release binary at
  `NOSE_VERSION` (0.20.0) into `.tools/nose` through `cargo-binstall`'s git
  mode against <https://github.com/corca-ai/nose>, because the crate is not
  published on crates.io and `cargo-binstall` must resolve the GitHub release
  artefact instead. The target is a no-op when the installed binary already
  reports the pinned version, and CI restores `.tools/nose` from a cache keyed
  on the runner operating system, architecture, base-image release, and that
  version.
- Compilation fallback is prohibited: the installer runs
  `cargo-binstall --disable-strategies compile,quick-install`, so a missing or
  unverifiable release asset is a visible provisioning failure rather than the
  start of a costly source build. The CI installer additionally verifies the
  `cargo-binstall` archive itself against a pinned SHA-256 digest before
  running it, because `cargo-binstall` clones the upstream repository before it
  downloads the release artefact.
- The gate resolves `NOSE_BIN` (an explicit override, resolved against the
  repository root when relative), otherwise the repository-local binary, and
  refuses to run when `nose --version` differs from `[tool.nose] version`,
  directing the maintainer to `make install-nose`. A drifted local install
  cannot quietly change what blocks the build.
- `[tool.nose]` pins the scan: the `cuprum` package as the root, with
  `cuprum/unittests` excluded; `mode = "syntax,semantic,near"` so a change to
  nose's defaults cannot widen or narrow the gate silently; a floor of 24
  intermediate-language (IL) tokens; `surface = "all"` so families nose keeps
  off its dashboard are still adjudicated; and `top = 0`, nose's unbounded
  spelling, so every family above the floor is adjudicated. The bound the ADR
  was first accepted with was 30, which was not binding at introduction — the
  full surface above the floor was 26 families, falling to 25 once the first
  extraction landed — but saturated as `main` grew, and has since been removed
  rather than raised; see the accepted trade-off below.
- The semantic channel reports only exact intermediate-language equivalence,
  so the gate blocks on a narrow, witness-backed subset of semantic
  duplication. Broader Type-4 duplication remains a human review concern.
- Findings print as `path:start-end ~ path:start-end` families, with the unit
  name appended to each location nose named, alongside the witness kind and
  refactoring value that ordered the family.
- Exceptions key on locations rather than line spans, which churn whenever
  code above them moves. A key is a repository-relative path glob matched with
  `PurePosixPath.full_match`, optionally suffixed `::name` to require nose's
  unit name. An entry names one key (`unit`) or several (`members`) and
  silences a family only when *every* location in it matches one of the entry's
  keys, so a new copy in an unlisted file still blocks. Entries that cover no
  finding are reported as stale. Fragment-level findings carry no unit name, so
  `::name` keys never match them and a bare path glob is the correct form for
  those.
- Stale-entry reporting distinguishes an entry whose family has *grown* a
  location (widen the entry) from one that no family matches (remove the
  entry). Growth is only claimed when the entry named more than one location
  and the surviving family matches every one of its keys: a one-key entry's key
  is a path glob, so any family it still touches may equally be one the entry
  never described, and reporting that as growth would confidently instruct the
  maintainer to re-authorize duplication the entry never covered. A partial
  overlap on a multi-key entry is reported as a path-glob coincidence for the
  same reason. Because the gate adjudicates a ranked surface, absence from the
  report means "unmatched in this scan", not proof that the duplication is
  gone; an entry is not deleted merely because its family fell below the
  ranking cutoff.
- Adjudicating Cuprum's own cohort extracted genuine shared implementations
  and left 24 reasoned entries covering the remaining 26 families. The
  extracted units include the shared post-initialization normalization of the
  scope dataclasses (`cuprum/context/_policy.py`), which had been retyped per
  type; the shared scope-registration handle base
  (`cuprum/_scope_registration.py`), which had been retyped per registration
  channel; and the shared close-then-drain failure finalization
  (`cuprum/_sink_lifecycle.py`), which five separate failure branches had each
  restated. Removing one entry is part of that count: the extraction that
  collapsed those five branches also shortened an import prologue below the
  configured floor, so the entry covering it stopped matching anything and the
  gate reported it stale. A later refactor added one back — see the
  window-matching trade-off above — for a net 24 entries over 26 families.
- `scripts/atomic_write.py` provides `atomic_write` for replacing generated
  files through a temporary sibling and `Path.replace`. It is a neutral
  persistence helper belonging to neither caller's domain; today its live
  consumer is the duplication allowlist writer in
  `scripts/duplication_allowlist.py`. The allowlist writer disables parent
  creation, preserves the destination mode, and syncs temporary file contents
  before replacement and parent-directory metadata after replacement, because
  it updates the existing repository `pyproject.toml` under its own lock.
- Allowlist updates take an advisory cross-process lock, so a concurrent
  writer waits until the current update completes. The lock coordinates
  processes that participate in its protocol; it does not coordinate unrelated
  editors of the same file.
- The gate is isolated tooling. `scripts/duplication_gate.py` and its four
  sibling modules carry PEP 723 headers declaring Python 3.14 and their own
  pinned dependencies, and run under `uv run --no-project`, so the gate needs
  neither the application virtualenv nor the compiled extension. Cuprum's
  application floor stays at 3.12, and `make typecheck` therefore re-checks the
  tooling modules in a second pass at their real floor rather than weakening
  them or misdeclaring Cuprum's support.
- An extraction can dissolve a reported family without dissolving the overlap
  that produced it, because the window that matched was anchored to one
  module's statement ordering. Extracting the observation builders out of
  `cuprum/_pipeline_internals.py` into `cuprum/_pipeline_observations.py`
  removed the `cuprum._observability` import window the module had shared with
  `cuprum/_command_internals.py`, and the gate reported that entry stale. The
  overlap itself persists at its new address: the new module imports four names
  from `cuprum._observability`, and its first three are the same names in the
  same order as the command module's, so the two blocks share a four-line
  identical run and diverge only where the command module binds
  `_wait_for_exec_hook_tasks` in its sorted position. No allow entry names that
  pair at this revision. The entry was removed anyway — the window it named
  really had dissolved, and keeping it would have left a key matching families
  the entry never described — but the two facts are recorded together so a
  later maintainer does not read the removal as proof that the shared import
  surface went away. Should the gate report the pair, the response is a new
  reasoned entry or an argument that four import lines justify extraction;
  restoring the removed entry is not one, because one of the two locations it
  named no longer holds the window. A stale report is a prompt to check which
  of the two happened; it is not itself the verdict.
- The scan covers production Python only. `tests/`, `benchmarks/`, and
  `scripts/` are maintained but not shipped, and are outside this first gate;
  the scope follows Cuprum's existing production-Python idiom
  (`SKYLOS_PRODUCTION_TARGETS`, `SKYLOS_EXCLUDE_FOLDERS`) rather than
  introducing a second convention.
- An unbounded surface makes re-adjudication a recurring cost, not a
  one-off: `main`'s own growth re-enters the branch's scan, and the entry count
  is a snapshot rather than a fixed budget. The gate was re-adjudicated a
  second time when this ADR's branch was rebased onto a `main` that had added
  families the original 24 entries never described. Four extractions and four
  exception changes followed, and the surface settled at 63 families under 51
  entries. The extractions were:
  - the whitelist-field rendering shared by the two drill-down log records
    in `cuprum/adapters/logging_adapter.py`;
  - the enum coercion behind `cuprum.echo_events._parse_broken_pipe_policy`
    and `cuprum.sh.execution._parse_process_group_policy`, extracted to the
    new leaf module `cuprum/_enum_parse.py` because the two callers sit on
    opposite sides of the package's layering and neither may import the
    other;
  - the probe-report-veto sequence shared by the grace period's first signal
    and the escalation after it, extracted as
    `cuprum._process_signal._members_remain_to_signal` beside the probe and
    reporter it already owned; and
  - the shielded reconciliation `cuprum._subprocess_stream_run` owed on both
    of the exit routes that failed before its gather.
  Four entries cover families whose duplication is main's own: a six-location
  `None` guard, a two-module import prologue, the 26-location
  `if typ.TYPE_CHECKING:` family, and a two-line `None` guard. The
  `cuprum/_process_handle/control.py::_terminate_shielded ~
  cuprum/_process_lifecycle.py::_terminate_process`
  entry was removed rather than widened: `main` had changed
  `_terminate_process_with_wait` to take a `_SettlementTarget` value, which
  moved the two inlined predicate pairs into separate functions and dissolved
  the window they shared. The same three-way check the previous consequence
  describes applies — the removal records that the window went away, not that
  the call sites stopped resembling each other.

## Rejected alternatives

- **Rerunning the detector comparison.** The tool choice is inherited by
  reference from an already-accepted decision on a benchmarked corpus.
  Rerunning it would add cost and a second set of tuning artefacts without
  adding evidence, and the reference's precision, recall, and timings are not
  Cuprum's measurements to claim.
- **A repository-wide wildcard or a generated exception per family.** Both
  discard the reason, which is the only part of an exception that survives
  review. Every entry names specific locations and carries a justification
  written for that family.
- **Auto-allowlisting the initial scan.** It converts "we have not looked at
  this yet" into permanent configuration. The initial cohort was adjudicated
  family by family instead: genuine duplication was extracted, and the rest was
  reasoned explicitly.
- **Manufacturing abstractions to empty the report.** Extracting a generic
  utility, an inheritance hierarchy, a boolean-mode helper, or a cross-layer
  dependency purely to satisfy the detector trades duplicated logic for a worse
  architecture. Where the parallel structure is intentional, the entry says so.
- **A shared multi-repository tooling framework.** The gate is a downstream
  port of a specific implementation at a pinned revision, not the first module
  of a cross-repository platform. Cuprum vendors what it needs and records the
  deviations.

## Accepted trade-offs

- The gate depends on a pre-1.0 tool distributed as a platform-specific binary
  from GitHub releases rather than as a Python package, so provisioning needs
  `cargo-binstall` and a cached `.tools/nose` in CI. The cached-install
  `curl | sha256sum | tar | run` path in CI is Linux x86-64 only, and its
  pinned digest is valid only for that archive name; another platform needs its
  own artefact and its own digest.
- nose reports spans without qualified unit names, so allow keys are path globs
  with optional unit names rather than `path::qualname` pairs. A key that
  covers several locations at once is therefore coarser than a name-keyed
  exception would be, and the `members` form is what keeps a multi-location
  entry honest.
- Gating on the top 30 ranked families bounds what the gate adjudicates.
  Duplication valuable enough to enter that ranking blocks the build;
  duplication below it does not, and the bound must be revisited if the ranked
  surface saturates. Evidence that a family is no longer reported is not
  evidence that it is gone.

  That condition was met, and the bound has been removed rather than raised.
  The surface at the original adjudication was 26 families; measured on
  2026-10-14 it is 72 on `main` at `8c78a5b77` and 63 over this branch's
  adjudicated tree, so a bound of 30 would have emitted 30 of 63 and silently
  excluded the tail. That exclusion is not benign, and the reason is structural
  rather than a matter of how large the tail happened to be. The stale-entry
  report is computed from the families the scan returned, and
  `partition_findings` marks an entry stale when no returned family matches it,
  so a family hidden below the bound leaves its reason matching nothing and the
  gate instructs the maintainer to *delete* an exception whose duplication is
  still present. Re-adjudicating this branch's own unbounded report at
  `top = 30` falsely reports 23 of its 51 entries as stale — nearly half the
  list, and each one a deletion instruction for duplication that is there.
  `top = 0` is the unbounded spelling nose accepts — omitting the term falls
  back to nose's default view of 30, and `top = all` and `top = -1` are both
  rejected. The schema module now refuses a report whose `summary.shown` is
  below `summary.families`, so any truncating bound fails the gate loudly
  instead of being adjudicated as the whole surface. Verified on this branch:
  `top=0` reports 63 families shown of 63; `top=30` reports 30 shown of 63 and
  is refused. Re-measured after the extraction that removed the last blocking
  families, and again after the re-adjudication recorded above: 63 families,
  all 63 allowed by reasoned exceptions, 0 blocking, 0 stale — and at
  `top = 30`, 30 families with 23 stale entries, which is the false-deletion
  count the check exists to refuse.
- A gate launched as a script path resolves its sibling modules through the
  `scripts` namespace package, which a development install of the application
  also contributes. The entry point pins its own tree to `sys.path` before
  importing them, so an out-of-tree run reads its own configuration or fails
  loudly, rather than silently adjudicating the checkout.
- The benchmark corpus, tuning sweeps, and adjudication evidence behind the
  tool choice remain in `leynos/episodic` and are not carried here. Cuprum
  keeps only small synthetic report fixtures and its own gate tests.
- An empty scope is indistinguishable from a clean tree in nose's own output.
  A root holding no supported source file yields exit 0 and a summary of
  `families: 0, shown: 0` — the same summary a genuinely clean scan produces.
  Only the stderr warning `no supported source files found under: <root>`
  separates them, so the detector wrapper reads stderr as well as stdout and
  raises a configuration error, which the CLI maps to exit 2. Without that, a
  mistyped `roots` entry would report success while scanning nothing. The check
  rejects only the empty scope: a root that does contain sources still returns
  its findings.
- A family can appear or vanish on a refactor that does not itself duplicate
  anything, because the detector matches windows of tokens rather than
  intentional units. Removing a three-line preamble from one function left a
  two-line None guard byte-identical to an unrelated guard in a module the same
  change never touched, and the gate reported the pair. The exception records
  that comparison rather than hiding it: the guard is the None-tolerance idiom
  the neighbouring entries already adjudicate, and it is load-bearing for
  typing there, so neither extraction nor deletion is available. The general
  lesson is that a passing gate says a tree is adjudicated, not that a change
  introduced no new reported pair.
- Wiring the gate into an existing aggregate target can break contract tests
  that execute that target's graph. `install-nose` became a prerequisite of
  `python-lint`, so `test_the_lint_target_runs_the_workflow_linters` began
  running the real installer: stubbing a Make target replaces its recipe but
  not its prerequisites, and the test's hermetic tool directory offers no
  `cargo-binstall`. The failure is easy to miss locally, because the test
  inherits `os.environ` and the detector lives under the gitignored `.tools/`,
  so only a clean checkout or CI exercises the missing dependency. When a new
  prerequisite is added to a target that a contract test drives, the test needs
  a stub for the prerequisite as well as for the target, unless the test exists
  precisely to prove the prerequisite ran.

## Addendum: pre-review re-adjudication (2026-10-14)

An assessment of this branch against its ExecPlan found that one entry in
`[tool.duplication_gate]` recorded an extraction as *outstanding* rather than
done. That entry named `cuprum/_process_handle/settle.py`, where
`_settle_teardown` and `_weigh` shared a six-line error arbitration differing
by one clause, and its reason described the honest fix as "a shared helper
taking the pending exception as an argument rather than a flag".

The reason was accurate about the fix and wrong about the state. Recording a
known-good extraction as outstanding is not adjudication — it defers the work
while taking the green result — so the extraction was performed in this
revision instead of the reason being reworded. The arbitration is now
`cuprum._process_handle.settle._arbitrate`, which both callers invoke; the one
difference between them is whether an ordinary failure is suppressed, which a
teardown does when the block already failed and a public stop never does. That
argument is passed as the presence of a pending exception rather than as a
strategy flag, because the deferred case is expressing a fact about the
caller's own state. Cancellation still outranks an ordinary failure and is
still never suppressible, which is the rule the module docstring states and
which the extraction leaves intact. The entry was removed, since it no longer
describes anything.

The removal moved the measured surface. Re-derived on this revision: 62
families, all 62 allowed by reasoned exceptions, 0 blocking, 0 stale, under 50
entries. Re-adjudicating the same report at `top = 30` emits 30 of the 62 and
falsely reports 23 of the 50 entries as stale.

The same assessment found a second scope-contract gap. `[tool.nose] min-size`
was enforced as a floor (`min_size >= MINIMUM_SIZE_FLOOR`), which accepts any
increase; raising it hides families below the new floor exactly as lowering it
admits noise above it, and neither direction is visible in the gate's output
because a family under the floor is never reported at all. The contract now
requires equality, so changing the floor is a reviewed edit to the constant
rather than a config change that passes the check unchanged.

Two further consequences were recorded at the same time, both about the failure
modes this gate can produce rather than about the detector:

- A report whose `summary` disagrees with its own `families` array is now
  refused. `summary.families` and `summary.shown` were previously compared only
  against each other, so a report claiming one family while emitting none
  satisfied the ordering check and handed the gate an empty surface to
  adjudicate. All three counts must now agree.
- The installer target refuses to report success unless the detector it leaves
  in place reports the pinned version. The recipe previously ended on the
  version print, so a failed or wrong-version install could exit zero over a
  detector still at the old version, and the gate would then run a binary the
  pin does not describe.

One accepted statement in this ADR needs a correction it cannot carry in place.
The Consequences section says `scripts/duplication_gate.py` "and its four
sibling modules carry PEP 723 headers". Only the entry point does. The other
four — `nose_schema.py`, `nose_detector.py`, `duplication_allowlist.py`, and
`atomic_write.py` — are plain modules with no inline metadata, and they run in
the environment the entry point's header declares, reached through
`PYTHONPATH=.` and the `scripts` namespace package. The distinction matters for
exactly the reason the original sentence was reaching for: the interpreter and
dependency pins live in one file, so bumping them is a single edit, and a
sibling cannot silently declare a different floor. The original sentence
implied five declarations instead of one, which would suggest a sibling could
drift.
