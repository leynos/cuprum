# Architectural decision record (ADR) 017: Attested, reconciled release

pipeline

## Status

Accepted on 2026-09-25.

## Date

2026-09-25.

## Context and problem statement

`release.yml` builds every wheel and the sdist, then must place the same bytes
on PyPI and on the tag's GitHub Release without ever losing an artefact or
overwriting one that already exists. Two constraints made a single linear job
insufficient:

- rebuilt wheels are rarely byte-identical across runs, so a re-pushed tag
  cannot assume that a second build of `cuprum-<version>-<platform>.whl`
  matches the first;
- PyPI refuses a second upload of an existing filename, and the pipeline must
  never overwrite a GitHub Release asset either, because both destinations are
  meant to hold exactly the bytes an external consumer may already have fetched
  and verified.

An earlier version of this workflow uploaded to the GitHub Release with
`--clobber`, replacing assets on every re-run. That made a re-run cheap but
broke the invariant that a downloaded artefact's digest never changes under a
fixed filename, and it could silently replace an asset a consumer had already
verified against an attestation from an earlier run.

The workflow also needed to run the PyPI upload from a job using an OIDC
credential, with no `GH_TOKEN` passed to its release scripts and no third-party
Python dependency resolution while the credential is live. Its read-only
`contents` permission supports the sparse checkout; checkout credentials are
not persisted. Each phase should receive no more privilege than it needs.

## Decision drivers

- No artefact may be silently replaced once published.
- A re-pushed tag must be safe to run again without manual cleanup, except
  when the destinations already disagree, which must fail loudly.
- The job holding the PyPI Trusted Publishing credential must resolve no
  third-party code and hold no other token.
- Each job's permissions must be the minimum needed for its own steps.
- Publication must be observable after the fact without adding an external
  telemetry service.
- Tracing spans were considered and declined, because GitHub Actions
  workflows here have no supporting tracing infrastructure to receive them.

## Options considered

1. **Keep `--clobber` uploads and a single `uv publish` step.** Simple, but
   an interrupted or re-run workflow can replace a previously published asset
   with different bytes under the same filename, defeating the provenance an
   earlier run's attestation described.
2. **Fail the whole workflow on any re-run of a tag that has already
   published anything.** Safe, but a partially completed run (for example, PyPI
   succeeded but the GitHub Release upload did not) would need manual recovery
   for every artefact, not only the ones that actually disagree.
3. **Reconcile per-filename, with a fixed precedence and no overwrites**
   (chosen). Each filename's canonical bytes are decided once, from whichever
   destination already holds it; each destination is sent only the names it
   lacks; and the workflow fails only when the destinations already disagree on
   a name, which needs a human to remove the offending asset before the tag can
   be published.

## Decision outcome

Adopt per-filename reconciliation with least-privilege jobs, split as follows,
with `permissions: {}` at the workflow's top level so no job inherits a scope
it did not declare:

- `check-version` (`contents: read`) compares the tag with
  `rust/cuprum-rust/Cargo.toml` and `pyproject.toml` and reports whether the
  version is a pre-release.
- `build-wheels` (`contents: read`) builds the sdist and every wheel.
- `attest` (`contents: read`, `id-token: write`, `attestations: write`;
  needs `check-version`, `build-wheels`) collects the built files, requires
  exactly one sdist, and generates a build provenance attestation with
  `actions/attest-build-provenance` for all of them. It also generates a
  run-unique Sigstore bundle,
  `cuprum-<tag>-run<run_id>-<attempt>.sigstore.json`, so a later run's bundle
  never collides with or replaces an earlier run's. Bytes a later job carries
  over from an earlier run were attested by that earlier run, and every run's
  bundle is retained.
- `draft-release` (`contents: write`; needs `check-version`, `attest`)
  creates the tag's GitHub Release as a draft, or reuses it if one already
  exists, and snapshots its current assets. It downloads those bytes and hands
  them on as the `release-github-state` artefact, because a draft release is
  visible only to a token that can write contents, and `publish-pypi` must not
  hold one.
- `publish-pypi` (the `pypi` environment; `contents: read` for a sparse
  checkout of the reconciliation scripts, plus `id-token: write`; needs
  `attest`, `draft-release`) has a read-only job `GITHUB_TOKEN` for checkout,
  but does not pass `GH_TOKEN` to its release scripts and has no attestation
  permission. The OIDC `id-token` permission is separate. It stages only the
  filenames PyPI does not already list in its JSON simple index, using GitHub's
  carried-over bytes (checked against the asset digest) when GitHub already has
  a name this run rebuilt. It uploads the remainder with
  `pypa/gh-action-pypi-publish` and `skip-existing: true`.
- `publish-release` (`contents: write`; needs `check-version`,
  `draft-release`, `publish-pypi`) uploads to the GitHub Release the names it
  lacks, taking PyPI's bytes through the JSON simple index's `files[].url` and
  checking them against `files[].hashes.sha256`; uploads the run's Sigstore
  bundle; verifies that every name carries the same SHA-256 on PyPI and on
  GitHub and that the bundle is attached; and only then un-drafts the release.

### Reconciliation and precedence

`scripts/release_assets.py` decides, once per filename, which destination's
bytes are canonical: PyPI's, if PyPI already has the name; otherwise the GitHub
Release's, if it already has the name; otherwise this run's freshly built and
attested file. Each destination then receives only the names it still lacks.
There is no `--clobber` anywhere in the workflow: nothing is ever overwritten.
`scripts/release_assets_cli.py` wraps this library in the `argparse` interface
`release.yml` calls; the library itself declares no command line.

### No-overwrite verification and manual recovery

Before `publish-release` un-drafts the release, it re-reads both the PyPI index
and the release's asset list and confirms every artefact name has an identical
SHA-256 digest on both sides, and that the run's Sigstore bundle is attached. A
name that already differs between the destinations — for example, one left over
from the earlier `--clobber` behaviour, or a stray asset uploaded outside this
workflow — makes `publish-release` fail and the release stays a draft. Recovery
is deliberately manual: an administrator must delete the offending asset from
the GitHub Release (PyPI's published files can never be deleted) so that a
later run's reconciliation can proceed without ambiguity. This is a one-time
cost paid only for a name that predates this reconciliation scheme.

### Least privilege and the `pypi` environment

`publish-pypi` runs under the `pypi` GitHub environment, restricted to tags
matching `v*.*.*`, and holds only `contents: read` (for a sparse checkout of
`scripts/` and the telemetry action) and `id-token: write` for Trusted
Publishing. It never receives `contents: write`, so it cannot itself create,
edit, or upload to the GitHub Release; that stays entirely in `draft-release`
and `publish-release`.

### Queued concurrency

Runs for one tag share the concurrency group `release-${{ github.ref }}` with
`cancel-in-progress: false`. A re-pushed tag queues behind the running release
rather than cancelling it mid-upload, which would otherwise leave a destination
holding only some of the run's names. The queued run then starts from a
complete, reconciliable state.

### Attestations

`attest` generates a build provenance attestation
(`actions/attest-build-provenance`) for every wheel and the sdist. PyPI's
Trusted Publishing flow, through `pypa/gh-action-pypi-publish`, separately
signs and uploads a PEP 740 attestation for each file it accepts. Both are
kept: the build provenance attestation covers the run that produced the bytes,
wherever they end up, while the PEP 740 attestation is PyPI's own record for
its index.

### Telemetry

Each phase writes one bounded JSONL record through
`.github/actions/release-telemetry`, following the benchmark-gate telemetry
precedent
([ADR-014](adr-014-benchmark-gate-telemetry-sink.md), [CI benchmark-gate telemetry](ci-benchmark-gate-telemetry.md)).
`release_phase_outcomes_total` was first written as schema version 1, with
labels `operation` (`check_version`, `attest`, `draft_release`, `publish_pypi`,
`github_upload`, `publish_release`), `outcome` (`success`, `failure`,
`skipped`), `failure_category` (`none`, `setup`, `version_mismatch`,
`collection`, `attestation`, `index_http`, `github_api`, `upload`,
`digest_mismatch`), `retry_bucket` (`0`, `1-2`, `3+`), `elapsed_bucket`
(`under_1m`, `1m_5m`, `5m_15m`, `over_15m`, `unknown`), and `http_status_class`
(`none`, `network`, `2xx`, `3xx`, `4xx`, `5xx`). `run_id`, `run_attempt`,
`tag`, and `recorded_at` are metadata fields outside `labels`. The current
schema is version 2 and adds the bounded `verification_failure_bucket` label
with values `0`, `1`, `2-5`, and `6+`. Records are written fail-open and, when
the job was not cancelled, uploaded as the artefact
`release-telemetry-<job>-<attempt>` with 90-day retention. The PyPI index fetch
uses an explicitly bounded retry loop with up to six attempts on a network
failure, `429`, or a `5xx` status. The runner's `curl` cannot report
`%{num_retries}`, so the loop counts its own attempts and reports the final
status for `retry_bucket` and `http_status_class`.

Tracing spans were considered for the release phases and declined: unlike the
ecosystem this repository's CI otherwise runs in, these GitHub Actions
workflows have no tracing collector or backend to receive spans, and adding one
purely for a handful of sequential release jobs would be infrastructure without
a consumer.

### Stdlib-only release scripts

`scripts/release_assets_cli.py`, `scripts/release_telemetry.py`, and
`scripts/release_version.py` are the release workflow's `argparse` entry
points, and `scripts/release_gh_version.py` is a standard-input filter with no
options. `scripts/release_assets_cli.py` wraps the importable reconciliation
libraries `scripts/release_assets.py`, `scripts/release_github_upload.py`, and
`scripts/release_provenance.py`, which declare no command line of their own.
All seven modules use only the Python standard library, not Cyclopts or Cuprum,
and the entry points run under the runner's preinstalled `python3` rather than
through `uv run`. This is a deliberate, documented exception to
[scripting standards](scripting-standards.md): `publish-pypi` holds the PyPI
OIDC credential and must not resolve any third-party package, from PyPI or
otherwise, while that credential is live. Every network call these scripts
might otherwise make instead stays in the workflow's own `curl` and `gh` steps,
which the scripts only plan for.

## Consequences

- A re-pushed tag is safe to run again: every destination converges on the
  same bytes per filename, and nothing already published is ever overwritten.
- A name that already differs between PyPI and the GitHub Release — for
  example, a leftover from the retired `--clobber` flow — blocks
  `publish-release` and requires a manual GitHub Release asset deletion. This
  is intentional friction rather than an oversight.
- `publish-pypi` has only read-only `contents` permission for checkout and
  `id-token: write`; its scripts receive no `GH_TOKEN`, and it has no
  attestation permission. No job other than `draft-release` and
  `publish-release` can write to the release.
- Publication history is durably observable through the `release-telemetry-*`
  artefacts without adding an external telemetry service, secret, or dashboard,
  mirroring [ADR-014](adr-014-benchmark-gate-telemetry-sink.md).
- The release library and its three entry points intentionally diverge from
  the repository's Cyclopts-and-Cuprum scripting baseline; that divergence is
  confined to modules that run only inside `release.yml`, immediately before or
  alongside the PyPI credential's use.
- No tracing spans exist for release phases; the telemetry records are the
  sole durable, cross-run observability surface until a tracing backend for
  GitHub Actions exists.

## Known risks

- The reconciliation logic is a single point of failure for correctness:
  a defect in `scripts/release_assets.py`'s precedence could, in principle,
  choose the wrong canonical source. This is mitigated by
  `cuprum/unittests/test_release_publish_steps.py`,
  `cuprum/unittests/test_release_github_steps.py`, and
  `cuprum/unittests/test_release_workflow_contract.py`, which exercise the
  scripts against scratch directories and a recording `gh` stand-in, and pin
  the job scopes, environment, concurrency, ordering, and pinned actions.
- The PyPI simple index is treated as the source of truth for what PyPI
  already holds; an index outage that returns a non-404, non-2xx status fails
  the phase rather than guessing, which is deliberate but means a PyPI-side
  incident can block a release until it resolves.
- Windows arm64 wheels are out of scope for this pipeline; see
  [ADR-016](adr-016-stable-abi-native-wheels.md) and roadmap item 8.5.1.

## Addendum (2026-10-26): verify provenance before carrying assets

Earlier sections were clarified to specify token scope and telemetry schema.
This addendum records the provenance change to the reconciliation policy above.

### Reconciliation

When PyPI lacks a name, `stage_pypi()` keeps this run's attested file if its
SHA-256 already matches the GitHub Release asset digest. Otherwise, it carries
the GitHub bytes only when GitHub reports that digest and a verified provenance
record binds the exact filename and digest. It validates every carried asset
before changing `dist/publish`; PyPI-held names still skip provenance checks.

The reconciliation API takes the downloaded GitHub asset directory and the
directory of verifier records; `stage-pypi` receives the latter through its
required `--provenance PATH` option. The standard-library-only
`scripts/release_provenance.py` module's `has_matching_subject` function checks
the GitHub CLI JSON subject shape. It must not import third-party packages while
`publish-pypi` holds its OIDC credential; identity policy is established by
the workflow's verifier flags. Missing, malformed, or mismatched provenance
fails closed and names the asset. When staging needs the carried GitHub bytes,
they must also match GitHub's reported digest; a mismatch blocks staging even
if the provenance record verifies. The error directs maintainers to rerun once
to recheck verification and the reported digest. If the mismatch persists while
staging needs those bytes and PyPI lacks the filename, delete the named GitHub
Release asset and rerun. The same recovery applies when provenance remains
unverified.

### Least privilege

`draft-release` adds `attestations: read` because it verifies preexisting
assets against GitHub's attestation store. When carried assets need
verification, it requires GitHub CLI `2.102.0` or newer and applies the
repository, release workflow, `GITHUB_REF` tag, and
`--deny-self-hosted-runners` policy. An empty carryover list records zero
verification failures without invoking the attestation CLI. Successful JSON
records are transferred with `release-github-state`. `publish-pypi` receives no
`GH_TOKEN` in its staging or publishing steps and has no attestation
permission. Its job's read-only `contents` token is used for checkout with
credentials not persisted; `id-token: write` is the separate PyPI OIDC
permission. The publisher decides whether a missing record matters after it
knows PyPI's holdings. The `2.102.0` minimum includes fixes for
[GHSA-wjmr-j3rp-mh2g](https://github.com/cli/cli/security/advisories/GHSA-wjmr-j3rp-mh2g),
which prevents affected CLI versions from accepting a different signer
workflow whose path merely shares the configured prefix, and
[GHSA-4mq3-hpgx-9cx8](https://github.com/cli/cli/security/advisories/GHSA-4mq3-hpgx-9cx8),
which makes `--source-ref` comparison case-sensitive so verification binds to
the exact release tag.

### Attestation identity for carried assets

An existing asset can be carried from an interrupted run only when GitHub
verifies the repository and signer workflow for the release tag, denies
self-hosted runners, and returns a statement subject with that asset's exact
filename and SHA-256. The workflow binds verification to `GITHUB_REF`, such as
`refs/tags/v1.2.3`, rather than `GITHUB_SHA`, so an interrupted attempt for the
same tag remains eligible. The tag ref is the selected identity; maintainers
must not retarget a tag as a recovery method.

The draft job retries each verifier call up to three times, with two- and
four-second backoffs. If all attempts fail, it removes partial JSON, warns for
the asset, and continues so the publisher scripts, which receive no `GH_TOKEN`,
can decide whether PyPI needs those bytes. A differing, unverified asset fails
staging before any publish-directory mutation or PyPI upload.

### Telemetry and verifier retry outcomes

The `draft-release` telemetry record maps the verification step to the existing
`attestation` failure category. A per-asset verifier warning is not itself a
job failure because `draft-release` cannot know whether PyPI already holds that
name. If PyPI needs differing bytes and provenance is absent, `stage-pypi`
fails before publication and its existing `digest_mismatch` telemetry mapping
records the blocking outcome. Schema version 2 adds the bounded
`verification_failure_bucket` label (`0`, `1`, `2-5`, or `6+`) to count failed
per-asset verification outcomes, including warnings that do not require
recovery. Recovery is needed only when PyPI lacks the filename and staging
needs the carried GitHub bytes: retry the tag workflow to recheck the reported
digest and provenance, then delete the named asset and rerun if downloaded
bytes still disagree with GitHub's reported digest or provenance remains
untrusted. The release phases still have no tracing spans: GitHub Actions has
no tracing collector or backend to receive them.

## Addendum (2026-10-30): verify provenance before publishing PyPI bytes

The 2026-10-26 addendum closed the GitHub-to-PyPI direction: a carried GitHub
asset needs approved provenance before PyPI may receive it. The reverse
direction remained open. When PyPI already held a filename the GitHub Release
lacked, `publish-release` downloaded the URL named by PyPI's index, checked it
against the digest in that same index, and uploaded it. The index digest
establishes only that the download arrived intact; it says nothing about who
built the bytes, and an index record that agrees with malicious bytes would
agree just as readily. A PyPI file could therefore become a GitHub Release
asset without any approved build provenance.

### Verification before upload

`publish-release` now runs an unconditional step between `check-fetched` and
`gh release upload`. It first requires GitHub CLI `2.102.0` or newer through
`scripts/release_gh_version.py`, then verifies every name in `pypi-fetch.tsv`
into `pypi-provenance/<name>.json`, and finally runs
`release_assets_cli check-provenance`. That command hashes the staged bytes
rather than trusting the index digest, and requires a verifier record binding
that exact filename and digest in one statement subject. Missing, malformed,
split-subject, or mismatched evidence fails the step, names every failing file,
and leaves the release a draft.

The evidence directory sits outside `github-upload/`, so verifier output never
becomes a release asset. The step has no `if:` condition: an empty fetch list
still succeeds, but a fetch list the step cannot read stops the job rather than
silently skipping verification. `check-provenance` refuses to run without
`--index`, because without the index the plan holds no PyPI names and the gate
would approve nothing while appearing to run. The `publish-release` telemetry
record maps the step to the existing `attestation` failure category, so a
failure here is counted as an attestation failure rather than as `setup`.

### Attestation identity and a moved tag

The verifier binds the repository, the signer workflow
`.github/workflows/release.yml`, the full tag ref from `GITHUB_REF`, the
triggering commit from `GITHUB_SHA`, the SLSA v1 predicate type, and denial of
self-hosted runners. It also requires the asset's exact filename and SHA-256 in
the statement subject.

Pinning the source digest is what separates this gate from the carried-asset
one. `draft-release` deliberately omits `--source-digest` so an interrupted
attempt for the same tag remains eligible. `publish-release` must not inherit
that latitude: bytes it publishes claim to be this release's build, so it pins
the commit the tag names now. A moved tag cannot carry PyPI's bytes over from a
different commit: PyPI stays canonical for a filename it holds, so a rerun
keeps those bytes and discards its own rebuild, and the gate fails until the
tag names the attesting commit again.

### Token scopes for the new gate

`publish-release` adds `attestations: read`; it already holds `contents: write`
for the upload and the un-drafting edit. It reads `GITHUB_REPOSITORY`,
`GITHUB_REF`, and `GITHUB_SHA` from the runner environment rather than
interpolating them into the script, keeping the workflow-wide rule that no
`run` body expands a `${{ }}` expression.

### Manual remediation

Bytes already on PyPI cannot be deleted or replaced. When the check fails,
first confirm the tag names the intended commit; if it was moved, restore it to
the commit that built PyPI's files and rerun. If PyPI holds bytes no run
attested, no rerun can approve them unless the rebuild is byte-identical, so
publish a new version. Deleting the GitHub asset is not a remedy here: the
blocking bytes are on PyPI, and the release simply stays a draft.
