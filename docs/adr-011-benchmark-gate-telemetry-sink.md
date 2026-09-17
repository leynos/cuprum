# Architectural decision record (ADR) 011: Durable benchmark-gate telemetry

______________________________________________________________________

## Status

Accepted, superseding the Grafana Cloud sink proposal on 2026-09-17. The
repository does not provision an external service, credential, or new
application. A hosted receipt was verified for commit
[bbe408f](https://github.com/leynos/cuprum/commit/bbe408f011d4a4c08d7c0e9f4ff3bf83c2b817a8)
in
[run 35248836322](https://github.com/leynos/cuprum/actions/runs/35248836322):
artefact 10508771038 was created and contained the schema version 1 record
described here. This verifies receipt for that run only. The separate harness
was tested locally; broader CI results are recorded on PR #418.

## Date

2026-09-17

______________________________________________________________________

## Context and problem statement

The `changes` job decides, on every Continuous Integration (CI) run, whether
the metered `benchmark-ratchet` job should execute. Its `$GITHUB_STEP_SUMMARY`
table and `::notice::` annotation explain an individual run, but they cannot
answer cross-run questions about benchmark runs, skips, or detector failures. A
durable record in existing GitHub Actions storage supplies that history without
introducing another service or secret.

The decision step already computes the bounded values needed for analysis. The
telemetry contract must preserve those values, retain run identity as metadata,
and ensure that no changed path, command text, or credential is published.

## Decision

For every non-cancelled `changes` execution, write one line to
`decisions.jsonl` and upload it as a GitHub Actions artefact named
`benchmark-gate-decision-${run_attempt}`. Request 90 days of artefact
retention. The existing `benchmark-ratchet` JSON and Markdown reports also
declare 90-day retention explicitly. GitHub repository and organization policy,
and later artefact deletion, remain authoritative; maintainers should download
archives before expiry when longer analysis is needed.

Each JSONL line has schema version 1 and this shape:

```json
{
  "schema_version": 1,
  "metric": "benchmark_gate_decisions_total",
  "value": 1,
  "labels": {
    "event_class": "pull_request",
    "detector_status": "success",
    "decision": "run"
  },
  "run_id": "123456789",
  "run_attempt": "1",
  "recorded_at": "2026-09-17T12:34:56Z"
}
```

The label set is exactly `event_class`, `detector_status`, and `decision`, with
the closed vocabularies `pull_request|other`, `success|failure|unknown`, and
`run|skip|skip-detector-failed`, respectively. `run` means that the gate
permits benchmark admission if quality prerequisites succeed; it does not prove
that `benchmark-ratchet` actually executed. `run_id`, `run_attempt`, and UTC
`recorded_at` are metadata outside `labels`; the two numeric identifiers are
decimal strings. The value is always the user-specified observation value `1`,
so analysis counts records rather than pretending that separate runs share an
exactly-once counter.

The record write and artefact upload are fail-open. A failure emits a bounded
warning and does not fail `changes` or alter benchmark admission. The
`!cancelled()` guard keeps detector-failure decisions recordable. Cancelled
runs, missing context, and expired or deleted artefacts are unknown; they must
not be represented as zero decisions.

The upload is skipped when `ACT=true`, because local `act` runs do not need a
local artefact service. Local tests verify the record and gate outputs. Receipt
from GitHub-hosted Actions remains a post-push operational check.

## Consequences

- Maintainers can download and analyse durable records using GitHub's existing
  Actions storage and command-line tooling.
- The contract adds no external service, secret, credential, or application.
- Retention is bounded by the requested 90 days and the repository or
  organization policy. Downloaded archives provide longer retention under the
  maintainer's control.
- A missing record cannot distinguish cancellation, a failed fail-open upload,
  missing context, or expiry. Workflow failure notifications and manual trend
  analysis remain the operational alerting surface.
- The finite label vocabulary keeps analysis bounded. Run metadata is useful
  for deduplication but is never a label and must not be promoted into one.
