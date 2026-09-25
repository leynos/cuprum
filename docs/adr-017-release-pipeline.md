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

The workflow also needed to run the PyPI upload from a job holding an OIDC
credential and nothing else: no GitHub token, no third-party Python dependency
resolution at the moment the credential is live, and no more privilege than
each phase strictly needs.

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
  `attest`, `draft-release`) holds no GitHub token. It stages only the
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
The metric is `release_phase_outcomes_total`, schema version 1, with labels
`operation` (`check_version`, `attest`, `draft_release`, `publish_pypi`,
`github_upload`, `publish_release`), `outcome` (`success`, `failure`,
`skipped`), `failure_category` (`none`, `setup`, `version_mismatch`,
`collection`, `attestation`, `index_http`, `github_api`, `upload`,
`digest_mismatch`), `retry_bucket` (`0`, `1-2`, `3+`), `elapsed_bucket`
(`under_1m`, `1m_5m`, `5m_15m`, `over_15m`, `unknown`), and `http_status_class`
(`none`, `network`, `2xx`, `3xx`, `4xx`, `5xx`). `run_id`, `run_attempt`,
`tag`, and `recorded_at` are metadata outside `labels`. Records are written
fail-open and, when the job was not cancelled, uploaded as the artefact
`release-telemetry-<job>-<attempt>` with 90-day retention. The PyPI index fetch
is an explicit bounded retry loop, up to six attempts on a network failure,
`429`, or a `5xx` status, because the runner's `curl` cannot report
`%{num_retries}`; the loop counts its own attempts and reports the final status
for `retry_bucket` and `http_status_class`.

Tracing spans were considered for the release phases and declined: unlike the
ecosystem this repository's CI otherwise runs in, these GitHub Actions
workflows have no tracing collector or backend to receive spans, and adding one
purely for a handful of sequential release jobs would be infrastructure without
a consumer.

### Stdlib-only release scripts

`scripts/release_assets_cli.py`, `scripts/release_telemetry.py`, and
`scripts/release_version.py` are the release workflow's `argparse` entry points;
`scripts/release_assets_cli.py` wraps the importable reconciliation library
`scripts/release_assets.py`, which declares no command line of its own. All
four modules use only the Python standard library, not Cyclopts or Cuprum, and
the entry points run under the runner's preinstalled `python3` rather than
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
- `publish-pypi` never holds a GitHub token, and no job other than
  `draft-release` and `publish-release` can write to the release.
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
