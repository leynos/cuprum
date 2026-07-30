# Enforce Oxford spelling in source identifiers

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`, and
`Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: IN PROGRESS

## Purpose / big picture

Cuprum currently enforces en-GB-oxendict spelling only in Markdown, although
its policy requires Oxford `-ize` and `-yse` forms. After this change, Python
and Rust source will pass through the same spelling gate, all existing
non-external `-ise` source spellings will use the Oxford form, and contributors
will receive a deterministic failure if identifiers, comments, docstrings, or
fixtures drift. The decision and exception process will be documented in an
accepted ADR and the developers' guide.

## Constraints

- Preserve public Python and Rust APIs. The twelve requested identifier
  renames must remain private, test-local, or function-local.
- Preserve spellings required by external APIs. Any unavoidable exception must
  be narrowly anchored in `typos.local.toml` and documented beside the pattern.
- Do not globally accept `normalise`, `serialise`, `summarise`, or related
  `-ise` terms because that would disable identifier enforcement.
- Generate `typos.toml` through `scripts/generate_typos_config.py`; never edit
  the generated file by hand.
- Run gates sequentially and capture full outputs under `/tmp`. Run all
  applicable deterministic gates before each CodeRabbit review.
- Commit each independently gated milestone before starting the next.
- Finish by renaming the branch exactly as requested, publishing it to the
  matching remote branch, and creating a draft PR with the required issue and
  Lody references.

## Tolerances

- Stop if an identifier rename affects a public export, cross-file consumer,
  or public signature.
- Stop if source spelling enforcement requires a new dependency or a change to
  the upstream shared spelling dictionary.
- Stop if a legitimate external spelling cannot be expressed as a narrow,
  contract-specific ignore pattern.
- Stop after three unsuccessful correction cycles for the same deterministic
  gate or CodeRabbit concern and report the evidence.
- Stop if unrelated generated changes cannot be separated from this issue.

## Risks

- Risk: whole-file source scanning will expose prose and fixture debt beyond
  the twelve identifiers. Severity: high. Likelihood: certain. Mitigation:
  inventory every source occurrence before widening the gate, classify it as
  repository-owned or external-contract text, and correct all repository-owned
  cases in the same milestone.
- Risk: `typos` may flag American spellings required by Python, Rust, or
  third-party APIs. Severity: medium. Likelihood: high. Mitigation: retain the
  upstream spelling at call sites and add only narrowly anchored patterns with
  comments naming the upstream contract.
- Risk: generator or Makefile changes could weaken the existing Markdown gate.
  Severity: medium. Likelihood: low. Mitigation: first add a focused regression
  test proving the three source pathspecs, observe it fail, then widen the
  recipe and run generator tests plus the full gates.
- Risk: broad formatting commands can create unrelated documentation churn.
  Severity: medium. Likelihood: medium. Mitigation: inspect every diff after
  formatting and retain only issue-scoped changes.

## Orientation

`Makefile` owns the `spelling` recipe used by both `lint` and `markdownlint`.
`typos.local.toml` is the repository-owned overlay, while
`scripts/generate_typos_config.py` renders tracked `typos.toml`. The generator
contract is tested in `scripts/tests/test_typos_rollout.py`. Policy lives in
`AGENTS.md` and `docs/developers-guide.md`; long-lived decisions are indexed by
`docs/contents.md` and the developers' guide.

## Milestones

### Milestone 1: normalize internal identifiers

Use Leta definitions, references, and rename operations to confirm and rename
the twelve stated Python identifiers. Check `__all__`, imports, and references
before each rename. Rename local variables within their declaring functions and
private/test symbols within their declaring files. Search the full Python AST
afterwards to prove no `-ise` definition, assignment target, or argument
remains. Run focused affected tests, then all applicable commit gates. Commit
the milestone and run `coderabbit review --agent`; resolve every concern before
continuing.

### Milestone 2: clean the complete scanned source corpus and widen the gate

Before editing `Makefile`, inventory `-ise` occurrences in every tracked `*.py`
and `*.rs` file, including comments, docstrings, and string fixtures. Classify
each occurrence. Change every repository-owned occurrence to its Oxford form.
Preserve only confirmed external-contract spellings.

Add a focused test that parses the `spelling` recipe and requires `*.md`,
`*.py`, and `*.rs`; run it first and record its expected failure. Then update
the recipe and help text. Run the widened spelling target to find other
American/Oxford conflicts. Correct genuine drift. For an unavoidable external
API term, add a narrowly anchored and documented pattern to `typos.local.toml`,
regenerate `typos.toml`, and update generator tests if the rendered shape
changes. Run the focused tests, then all applicable commit gates. Commit the
milestone and run `coderabbit review --agent`; resolve every concern before
continuing.

### Milestone 3: document the policy decision

Update `AGENTS.md` so identifiers, comments, docstrings, and source prose are
explicitly governed while external APIs remain exempt. Update the generated
configuration note and the developers' guide with the source scope and narrow
exception workflow. Add ADR-008 using the established ADR structure and index
it in `docs/contents.md` and the developers' guide. Add an Unreleased Changed
entry to `CHANGELOG.md` with the issue/PR convention used by the file. Run
formatting, Markdown spelling/lint, Nixie, and the complete applicable gate
stack sequentially. Commit the milestone and run `coderabbit review --agent`;
resolve every concern.

### Milestone 4: final verification and publication

Inspect the complete `origin/main...HEAD` diff and re-run the full gate stack.
Run a final CodeRabbit review if the preceding review required material fixes.
Rename the local branch to
`issue-249-decide-and-enforce-en-gb-oxendict-spelling-for-code-identifiers-not-just-prose`,
push while setting the matching upstream, and create a draft PR. The title
must contain `(#249)`. The summary must state `Closes #249`. End the body with a
`## References` section containing the issue, PR #240 context, and the literal
Lody session URL using session ID `c8d59abb-fb35-461e-b433-dde693206e0f`.

## Validation

Run focused tests introduced or affected by each milestone, then use the
repository gates in this order, sequentially and with logs under `/tmp`:

```plaintext
make check-fmt
make test
make typecheck
make lint
make markdownlint
make nixie
mbake validate Makefile
```

The expected result is exit status zero for every applicable command. The
source-scope regression test must fail before the Makefile change because the
recipe contains only `*.md`, then pass after it contains all three pathspecs.
The final widened `spelling` target must report no unapproved source spelling.

## Progress

- [x] 2026-07-30: Loaded Leta, Python Router, Rust Router, and ExecPlans skills.
- [x] 2026-07-30: Registered the current worktree as a Leta workspace and
  restarted its Python and Rust language servers.
- [x] 2026-07-30: Confirmed the clean feature branch starts exactly at
  `origin/main` and captured the Lody session ID.
- [x] 2026-07-30: Milestone 1 renamed all twelve internal identifiers with
  Leta, repaired non-syntactic CrossHair/assertion references, passed 69
  focused tests, and passed the full repository gate stack.
- [x] 2026-07-30: Milestone 2 recorded the expected red Makefile-contract
  test, widened the gate to Markdown/Python/Rust, corrected the complete source
  corpus, preserved GitHub wire/CLI names through anchored exceptions, and
  passed the full repository gate stack.
- [x] 2026-07-30: Milestone 3 made the source/identifier policy explicit,
  documented narrow exception handling, added and indexed ADR-008, added the
  Unreleased changelog entry, and passed the full repository gate stack.
- [ ] Milestone 4: complete final review and publish the draft PR.

## Surprises & Discoveries

- Leta workspace registration succeeded inside the sandbox, but starting its
  daemon required permission to write daemon state outside the worktree. After
  approval, both `basedpyright` and `rust-analyzer` restarted and symbol search
  succeeded.
- Leta rename operations preserve syntactic references but removed final
  newlines from seven edited files and did not update names embedded in a
  CrossHair contract or assertion strings. `make fmt` restored the newlines;
  the embedded references required explicit edits.
- `make fmt` reformatted three unrelated existing documentation files. Those
  changes were restored before validation and are not part of this issue.
- Widening the gate exposed 211 findings rather than only the anticipated
  `-ise` prose. Most were repository-owned Oxford/British spelling drift. A
  large cluster used GitHub Actions' external `artifact` vocabulary: internal
  names moved to `artefact`, while the `"artifacts"` wire key, `/artifacts` URL
  path, and established `--artifact-name` CLI option remain unchanged via
  documented anchored patterns.
- Four test fixtures intentionally contain misspellings or word fragments to
  exercise the spelling renderer and stream splitting. They require exact
  pattern ignores; accepting their words globally would weaken the gate.
- Common Changelog repeats `Changed` headings across releases, while the
  repository's Markdown configuration rejects duplicate headings globally.
  The existing released `Changed` heading now carries a one-line MD024
  exception so the new Unreleased section can use the required conventional
  heading.

## Decision Log

- Decision: treat the user's detailed coding plan and explicit implementation
  and publication request as approval to execute this plan. Rationale: the
  requested revision is incorporated here before implementation, satisfying the
  approval gate without a redundant pause.
- Decision: make source-corpus cleanup part of Milestone 2 before the Makefile
  change. Rationale: widening first would knowingly introduce failures from
  comments, docstrings, and fixtures that are independent of identifier drift.
- Decision: use a Makefile-contract regression test as the red stage. Rationale:
  identifier renames are behaviour-preserving, while the missing source glob is
  the observable enforcement behaviour that can fail before implementation.

## Outcomes & Retrospective

Implementation is in progress. This section will record the final commits,
validation evidence, CodeRabbit outcomes, branch publication, and draft PR.
