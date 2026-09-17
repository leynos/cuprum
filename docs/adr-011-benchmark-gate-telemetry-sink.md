# Architectural decision record (ADR) 011: Durable benchmark-gate telemetry sink

______________________________________________________________________

## Status

Accepted. This ADR records the proposed sink and its workflow contract. The
repository's telemetry variables and secret are not confirmed as provisioned:
attempts to list the GitHub secrets and variables both returned HTTP 403, and
no Grafana Cloud stack credentials are available to this repository. A
maintainer must complete the secure provisioning and receipt check described in
[operational contract](ci-benchmark-gate-telemetry.md)
before this decision can be called deployed.

## Date

2026-09-17

______________________________________________________________________

## Context and problem statement

The `changes` job decides, on every Continuous Integration (CI) run, whether
the metered `benchmark-ratchet` job should execute. It records that decision in
`$GITHUB_STEP_SUMMARY` and with a `::notice::` annotation. Those records are
useful for one run, but they cannot answer cross-run questions about benchmark
run, skip, or detector-failure frequency, nor can they drive a trend alert.

The gate already computes the bounded values needed for such a metric. What is
missing is a durable sink outside GitHub and an explicit rule for which values
may cross that boundary.

## Decision

Use Grafana Cloud's OTLP/JSON metrics endpoint as the external sink. The
`changes` job publishes `benchmark_gate_decisions_total` with exactly these
three labels:

- `event_class`: `pull_request` or `other`;
- `detector_status`: `success`, `failure`, or `unknown`;
- `decision`: `run`, `skip`, or `skip-detector-failed`.

The publish step transports the three outputs from the preceding decision step.
It validates that every output is present and belongs to its closed vocabulary;
invalid or missing values are refused before transport and produce only a
bounded warning. No changed path, command text, commit SHA, run ID, or
timestamp may be a label value.

The OTLP payload has an empty `resource` object. Grafana Cloud documents that
resource attributes can be promoted into labels or exposed through
`target_info`, so adding resource attributes would widen the metric's label
surface. The data point carries `asInt: "1"` for each decision observation; the
value is deliberately not a process-wide counter increment.

Use `curl` for the push. The endpoint and Grafana Cloud instance ID are
repository variables named `BENCHMARK_TELEMETRY_ENDPOINT` and
`BENCHMARK_TELEMETRY_INSTANCE_ID`. The credential is the repository secret
`BENCHMARK_TELEMETRY_TOKEN`, configured with the least-privilege
`metrics:write` scope. The token is never placed on a command line or printed.

The step is gated by the token's presence with `if: env.SINK_TOKEN != ''` and
remains under `!cancelled()`, so detector-failure decisions can be published.
Transport failure emits a warning and remains fail-open; it must not fail the
`changes` job or suppress `benchmark-ratchet`. A missing secret, including on a
fork, skips publication and is an intentional optional-integration degradation.

The metric is queried with `count_over_time`, which counts samples received by
the sink. It does not count cumulative counter increments or prove exactly-once
workflow execution. Retries, loss, backend deduplication, and repeated samples
with the same labels and timestamp can change the observed count; therefore
`rate()` and `increase()` are inappropriate for this value-one observation
series.

## Consequences

- Maintainers gain a bounded, cross-run query and alert surface in Grafana
  Explore and dashboards, subject to the Grafana Cloud Free tier's 14-day
  metrics retention.
- The workflow gains one optional external call. A missing secret, rejected
  request, or unavailable sink degrades telemetry without changing benchmark
  admission. Missing secrets skip the step; attempted pushes that fail warn.
- A repository secret and two repository variables must be provisioned and
  periodically read back by a maintainer. The token must hold only
  `metrics:write`, and must be rotated through the repository's secure secret
  settings.
- The integration cannot claim durable receipt from repository tests alone:
  receipt requires a maintainer-controlled stack and credential. The
  operational contract records the secure provisioning and read-back steps.
- The metric's label set stays finite because the workflow rejects all values
  outside the three vocabularies and sends no resource attributes. The
  OpenTelemetry mapping details are documented by
  [Grafana Cloud's OTLP format considerations][grafana-otlp].

[grafana-otlp]: https://grafana.com/docs/grafana-cloud/observe-and-act/send-data/otlp/otlp-format-considerations/
