# Imported rstest-bdd documentation provenance

This file records the origin of the upstream `rstest-bdd` documents vendored
into this repository. It is a local provenance record.

The imported documents are upstream text with one deliberate, bounded
divergence: their cross-file relative links were rewritten to absolute GitHub
URLs. The [relative-link rewriting](#relative-link-rewriting) section records
that change, why it was made, and the exact checksums on both sides of it.

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
above at import time, and each digest below was independently re-read from the
upstream commit to confirm it.

| Upstream path                    | Destination path                            | SHA-256 (as imported)                                              |
| -------------------------------- | ------------------------------------------- | ------------------------------------------------------------------ |
| `docs/users-guide.md`            | `docs/rstest-bdd-users-guide.md`            | `1e9f4d1b6607fdf83df979676f1a682218cd6751381d374cb98b701778b798b2` |
| `docs/v0-6-0-migration-guide.md` | `docs/rstest-bdd-v0-6-0-migration-guide.md` | `6e76c10028f9962134f6158732cf1645ae1b0beba3018bccd3bc3e88b2c38985` |
| `docs/v0-5-0-migration-guide.md` | `docs/rstest-bdd-v0-5-0-migration-guide.md` | `508d5b55cb2025915ced366b917a2327e572a6aa920fd290684b858b7afa2abc` |

`docs/v0-5-0-migration-guide.md` is included because the request requires the
intervening guides to be inspected when the starting version precedes 0.5.0,
and because it is the guide immediately below the target release.

## Relative-link rewriting

The imported documents originally contained cross-file relative Markdown links
whose targets are not vendored here. Those links are rewritten to absolute
GitHub URLs so that they resolve for a reader of this repository and for any
consumer into which this text is copied.

This follows the policy upstream already applies to `docs/users-guide.md`.
ADR-021 records the reasoning: the user's guide "is vendored into consumer
projects, so it cannot rely on relative paths for cross-references into this
repository", and issue #499 moved those cross-references to absolute GitHub
URLs. The 15 absolute definitions already present in the imported user's guide
are the result, 11 of them pointing at `blob/main`. One of the 15 wraps its
destination onto the following line, so a single-line scan of the file
undercounts by one. This change extends the same treatment to the links that
upstream had left relative, so the vendored copies no longer contain a mixture
of the two forms.

The base URL is `https://github.com/leynos/rstest-bdd/blob/main/`, matching the
definitions upstream already emits. Before adopting it, every target and every
anchor was confirmed present on `main` *and* at the pinned commit, so the links
do not merely appear plausible. Verified fragments include
`rstest-bdd-design.md#2761-borrow-constraint-exposed-by-gpui-adoption`,
`#2762-interim-gpui-state-pattern`, `#2763-v060-beta2-quick-wins`,
`#2765-v070-pre-100-redesign`, `users-guide.md`'s
`#stateful-gpui-scenarios-with-durable-handles` and
`#published-gpui-022-stateful-step-variants`, and `v0-6-0-migration-guide.md`'s
`#migrate-a-stateful-gpui-test`.

### Sites rewritten

Sixteen cross-file link sites were rewritten across the three documents.

| Document                               | Sites | Form                                            |
| -------------------------------------- | ----- | ----------------------------------------------- |
| `rstest-bdd-users-guide.md`            | 1     | One reference definition.                       |
| `rstest-bdd-v0-6-0-migration-guide.md` | 13    | Five inline links; eight reference definitions. |
| `rstest-bdd-v0-5-0-migration-guide.md` | 2     | One inline link; one footnote definition.       |

Four of the rewritten definitions, and one prose line that a rewritten inline
link pushed past 80 columns, were re-wrapped to satisfy `make check-fmt`. The
wrapping is textual only: no URL, label, or destination changed.

### Links deliberately left relative

Fourteen sites are intra-document anchors of the form `](#some-section)`. They
resolve within the file that contains them, so an absolute URL would be wrong
rather than merely verbose; every one was confirmed to match a heading in its
own document.

The bare-URL lint (MD034) requires the CommonMark angle-bracket form
`<https://…>` for a definition that is nothing but a URL, so the single
footnote definition rewritten in the v0.5.0 guide uses that form.

### Checksums on both sides of the rewriting

The left column is the digest as imported from the pinned commit. The right
column is the digest after link rewriting, which is what this repository
commits. A reader can reproduce either side independently.

| Destination path                            | SHA-256 (as imported from upstream)                                | SHA-256 (current, links absolute)                                  |
| ------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `docs/rstest-bdd-users-guide.md`            | `1e9f4d1b6607fdf83df979676f1a682218cd6751381d374cb98b701778b798b2` | `0530944a4794d300136c4ac0c039d198006fc2e5bcc7e4e8e9bae5a349542445` |
| `docs/rstest-bdd-v0-6-0-migration-guide.md` | `6e76c10028f9962134f6158732cf1645ae1b0beba3018bccd3bc3e88b2c38985` | `6914e3f3285aa6962bd74fcb26cd3060f228ca1b0e29c8cd9ca753306e2c8dd2` |
| `docs/rstest-bdd-v0-5-0-migration-guide.md` | `508d5b55cb2025915ced366b917a2327e572a6aa920fd290684b858b7afa2abc` | `02d6ea45cf41c05ccc2ec9b089da7abe190ce68def02550d83f078747092b998` |

The upstream digests were reproduced by reading the blobs back from the pinned
commit, not carried over from the first import. The byte growth is small and
entirely accounted for by the longer URLs: +52, +676, and +101 bytes
respectively, from 16 rewritten links and the five lines they re-wrapped.

### Files not vendored

The rewritten links point at upstream documents that are deliberately not
copied here. Two hazards make local copies actively misleading rather than
merely redundant:

1. `developers-guide.md` collides with this repository's own
   `docs/developers-guide.md`, which documents Cuprum's workflows and has
   nothing to do with `rstest-bdd`. A relative link would silently resolve to
   the wrong document; the absolute link cannot.
2. Upstream and local ADR numbers occupy independent numbering spaces. This
   repository already has `docs/adr-006-context-package-split.md` and
   `docs/adr-007-subprocess-execution-module-boundaries.md`, which are
   unrelated decisions. Importing the upstream ADRs under their own names would
   place two different "ADR-006" and "ADR-007" documents side by side.

Because the links are now absolute, neither hazard can misdirect a reader, and
no upstream file has to be copied to make a reference resolve.

## Content boundary

The retrieved text contains sections explicitly labelled as `v0.6.1 beta` and
`v0.7.0`. Those sections describe later releases and are not part of the v0.6.0
migration surface. Their prose is preserved verbatim; the link rewriting
described above is the only change made anywhere in the imported text, and it
did not touch these sections. They are excluded from the applicability analysis
recorded in the ExecPlan.

Specifically, the guard-based `StepContext` borrowing model is a v0.7.0 change
and the `#[harness_context]` marker is a v0.6.1 beta change. Neither is
implemented or relied upon by this repository's migration.
