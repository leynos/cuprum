# Architectural decision record (ADR) 011: Durable benchmark-gate telemetry sink

## Status

Accepted on 2026-09-16. The `changes` job in `.github/workflows/ci.yml`
publishes one bounded counter per run to Grafana Cloud over the OpenTelemetry
Protocol (OTLP), gated on a repository secret and failing open. Delivered by
issue #339.

## Date

2026-09-16.

## Context and problem statement

The `changes` job decides, on every Continuous Integration (CI) run, whether
the metered `benchmark-ratchet` job should execute. It records that decision in
the GitHub Actions step summary as a Markdown table and a `::notice`
annotation, and both are durable only for as long as the run record is.

Nothing aggregates the decision across runs. The repository therefore cannot
answer questions whose answers are trends rather than single runs: how often is
the gate skipping, and is `skip-detector-failed` — the case where
`dorny/paths-filter` failed and performance was silently not measured —
becoming more frequent? Answering those from individual run records means
paging through the Actions UI, and no alert can be attached to them.

The gate already computes three closed vocabularies for its own summary, so the
values needed for a bounded metric exist and are stable. What is missing is a
sink outside GitHub that retains them, and a decision about which values may
cross that boundary.

## Decision drivers

- The metric must be bounded. Changed file paths, command text, commit SHAs,
  run identifiers, and timestamps must never reach a label, because unbounded
  labels exhaust the time-series budget and publish repository content to an
  external service.
- Telemetry must never gate the product's own CI. A sink outage, a revoked
  token, or a fork without the secret must not fail a run or block
  `benchmark-ratchet`.
- The metric must be emitted from the same shell code path that computes the
  decision it reports, so the summary and the metric cannot disagree.
- Adding the sink must not widen the `changes` job's permissions. An existing
  contract test pins them to `contents: read` and `pull-requests: read`, and
  the job's authority should follow its filter work rather than its reporting.
- The push should introduce no new runtime dependency, because the job runs on
  every event and should stay cheap.

## Requirements

### Functional requirements

- Publish a monotonic counter named `benchmark_gate_decisions_total` with
  exactly three labels: `event_class`, `detector_status`, and `decision`.
- Restrict every label to a finite set: `event_class` to
  {`pull_request`, `other`}, `detector_status` to {`success`, `failure`,
  `unknown`}, and `decision` to {`run`, `skip`, `skip-detector-failed`}.
- Skip the push entirely when the sink credential is absent.
- Continue the run, and record the same decision, when the push fails.

### Technical requirements

- Use the OTLP/JSON encoding, which can be produced with `printf` and sent with
  `curl`, so no new binary or library is required.
- Read the credential from `secrets` only, and never write it to a log.
- Emit from the step that already computes the decision, under the same
  `!cancelled()` condition, so the detector-failure path is reported too.

## Options considered

### Option A: Grafana Cloud OTLP gateway with a static token

POST an OTLP/JSON `ExportMetricsServiceRequest` to
`https://otlp-gateway-<region>.grafana.net/otlp/v1/metrics` using HTTP Basic
authentication, with the instance identifier and an API key holding the
`metrics:write` scope.

This reuses an existing managed observability platform, needs only `curl`, and
keeps the credential static, so the job's permissions are untouched. It adds an
external service dependency and the free tier's retention window bounds how far
back a query can look.

### Option B: GitHub OIDC workload identity against the same gateway

Exchange a short-lived OIDC token for a scoped credential at run time, which
removes the long-lived secret.

This is the stronger credential model, but the exchange needs
`id-token: write`, and the `changes` job's permissions are pinned by an
existing contract test and by the principle that the job's authority should
follow its filter work. Widening the job's authority to serve reporting is a
poor trade for a metric whose failure mode is "no data".

### Option C: keep the step summary and read it back from the Actions API

Compute the trend by querying the Actions API for past run summaries.

This needs no new service, but GitHub retains step summaries for the life of
the run record, the API is paginated and rate-limited for a question that is
naturally a time-series query, and no alert can fire on it.

| Topic                  | Option A   | Option B                     | Option C    |
| ---------------------- | ---------- | ---------------------------- | ----------- |
| New dependency         | curl only  | curl plus IaC token exchange | none        |
| Job permissions change | none       | `id-token: write`            | none        |
| Secret lifetime        | long-lived | short-lived                  | none        |
| Queryable surface      | PromQL     | PromQL                       | Actions API |
| Alertable              | yes        | yes                          | no          |

_Table 1: Comparison of telemetry sink options._

## Decision outcome / proposed direction

Option A. The `changes` job gains a final step,
`Publish the benchmark gate decision`, placed after
`Record the benchmark gate decision` and carrying the same
`if: ${{ !cancelled() }}` condition, so that a detector failure is reported as
well as a normal decision.

The step constructs an OTLP export containing one Sum data point with
`isMonotonic: true`, `aggregationTemporality: 2` (cumulative), and an integer
value of `1`. Enum fields are encoded as integers and 64-bit integers as
decimal strings, as the protocol requires, and the payload is sent with
`curl --fail-with-body --max-time`. The step is gated on the token being
non-empty, matching the repository's existing optional-integration pattern, and
a failed or skipped push is reported as a `::notice` without failing the job.

## Goals and non-goals

- Goals:
  - Make the gate's decision history queryable as a retained time series.
  - Keep every label value inside a closed, finite vocabulary.
  - Degrade to "no data" rather than to "broken CI" when the sink is
    unavailable.
- Non-goals:
  - Instrumenting the `benchmark-ratchet` job's own measurements. This ADR
    covers the admission decision only.
  - Replacing the step summary, which stays the per-run human-readable record.
  - Establishing alert thresholds. The operational document defines the
    suggested rule; the maintainer owns the values.

## Known risks and limitations

- The free tier retains a bounded window and caps active series, so a label
  that unexpectedly acquires cardinality would be visible as dropped data
  rather than as an error. The operational document therefore requires a
  one-off read-back check that the label set is exactly the three intended
  labels.
- The step cannot be verified against the live gateway from the repository's
  tests, because that needs a credential. The integration harness verifies the
  launch conditions and the payload shape instead, and the residual gap is
  recorded as a documented manual read-back.
- Grafana Cloud's OTLP translation drops delta temporality, so a future change
  to delta encoding would silently stop contributing to the series. Cumulative
  is therefore recorded here as the required temporality, not a preference.

## Consequences

### Positive

- Gate decisions accumulate outside GitHub, so a rising
  `skip-detector-failed` share becomes visible and alertable.
- The metric's values are safe to publish, because the vocabulary is closed and
  no repository content reaches a label.
- A sink outage costs one `::notice` line and nothing else.

### Negative

- The repository gains a credential to manage, and a sink whose availability
  is now part of the answer to "why is this series flat?".
- One more step runs on every CI event, including events that will never run
  the benchmark job.
