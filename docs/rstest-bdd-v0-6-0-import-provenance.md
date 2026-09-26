# Imported rstest-bdd documentation provenance

This file records the origin of the upstream `rstest-bdd` documents vendored
into this repository. It is a local provenance record: the imported documents
themselves are upstream text and remain byte-for-byte unmodified.

## Upstream identity

- Repository: <https://github.com/leynos/rstest-bdd>
- Tag: `v0.6.0`
- Resolved commit: `72fb22635670e456545ca368805ba4c1c9d7bd69`
- Tag kind: lightweight (the tag reference points directly at the commit;
  there is no separate annotated-tag object to dereference)

The tag resolution was compared against the commit named in the migration
request. They are identical, so no substitution of `main`, `HEAD`, a
development branch, or a latest-documentation URL was made. The investigation
and its result are recorded in the ExecPlan.

## Imported files

The checksums below are SHA-256 over the file contents. Every destination file
was verified byte-for-byte against the same blob read from the immutable commit
above.

| Upstream path                    | Destination path                            | SHA-256                                                            |
| -------------------------------- | ------------------------------------------- | ------------------------------------------------------------------ |
| `docs/users-guide.md`            | `docs/rstest-bdd-users-guide.md`            | `1e9f4d1b6607fdf83df979676f1a682218cd6751381d374cb98b701778b798b2` |
| `docs/v0-6-0-migration-guide.md` | `docs/rstest-bdd-v0-6-0-migration-guide.md` | `6e76c10028f9962134f6158732cf1645ae1b0beba3018bccd3bc3e88b2c38985` |
| `docs/v0-5-0-migration-guide.md` | `docs/rstest-bdd-v0-5-0-migration-guide.md` | `508d5b55cb2025915ced366b917a2327e572a6aa920fd290684b858b7afa2abc` |

`docs/v0-5-0-migration-guide.md` is included because the request requires the
intervening guides to be inspected when the starting version precedes 0.5.0,
and because it is the guide immediately below the target release.

## Relative-link mapping

The imported documents contain five relative Markdown links. The destination
repository cannot host their targets in every case, so the mapping is recorded
here rather than by copying upstream files. Local migration notes are kept
outside the imported copies, as required.

| Link target in imported text                                | Resolution                                                                                                                                           |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adr-006-fallible-scenario-functions.md`                    | Not imported. Read at `docs/adr-006-fallible-scenario-functions.md` in the upstream repository at commit `72fb22635670e456545ca368805ba4c1c9d7bd69`. |
| `adr-007-harness-context-injection.md`                      | Not imported. Read at `docs/adr-007-harness-context-injection.md` upstream at the same commit.                                                       |
| `adr-009-consistent-implicit-fixture-name-normalization.md` | Not imported. Read at `docs/adr-009-consistent-implicit-fixture-name-normalization.md` upstream at the same commit.                                  |
| `developers-guide.md`                                       | **Collides** with this repository's own `docs/developers-guide.md`. Must resolve to the upstream document, not the local one.                        |
| `rstest-bdd-design.md`                                      | Not imported. Read at `docs/rstest-bdd-design.md` upstream at the same commit.                                                                       |

Two further hazards make local copies of these targets actively misleading
rather than merely redundant:

1. The `developers-guide.md` link would resolve to this repository's own
   developers' guide, which documents Cuprum's workflows and has nothing to do
   with `rstest-bdd`. A local copy cannot fix this without rewriting the
   imported text.
2. Upstream and local ADR numbers occupy independent numbering spaces. This
   repository already has `docs/adr-006-context-package-split.md` and
   `docs/adr-007-subprocess-execution-module-boundaries.md`, which are
   unrelated decisions. Importing the upstream ADRs under their own names would
   place two different "ADR-006" and "ADR-007" documents side by side.

Because the imported text must stay byte-for-byte intact, the collision is
resolved by this mapping and not by editing the imported files. A local reader
following one of these links should substitute the upstream repository at commit
`72fb22635670e456545ca368805ba4c1c9d7bd69`.

## Content boundary

The retrieved text contains sections explicitly labelled as `v0.6.1 beta` and
`v0.7.0`. Those sections describe later releases and are not part of the v0.6.0
migration surface. They are preserved verbatim because the imported documents
must remain unmodified, and they are excluded from the applicability analysis
recorded in the ExecPlan.

Specifically, the guard-based `StepContext` borrowing model is a v0.7.0 change
and the `#[harness_context]` marker is a v0.6.1 beta change. Neither is
implemented or relied upon by this repository's migration.
