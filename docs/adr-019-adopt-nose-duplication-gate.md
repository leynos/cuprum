# Architectural decision record (ADR) 019: Adopt a nose code-duplication gate

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
optional `::name` suffix, and that the gate adjudicates a ranked surface of 30
families rather than every reported one.

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
  off its dashboard are still adjudicated; and `top = 30` ranked families. At
  introduction the full surface above the floor was 26 families, falling to 25
  once the first extraction landed, so `top = 30` was not yet binding — that is
  a measurement, not a guarantee, and a future scan may saturate it.
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
