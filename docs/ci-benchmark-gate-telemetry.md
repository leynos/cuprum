# Benchmark-gate telemetry

This contract is for maintainers of Cuprum's Continuous Integration (CI)
workflow. It describes the proposed external telemetry sink, its bounded
payload, and how to operate the resulting Grafana Cloud queries and alerts.

## Problem and source of truth

The `changes` job decides whether `benchmark-ratchet` is admitted. Its
`$GITHUB_STEP_SUMMARY` table and `::notice::` annotation explain an individual
run, but they cannot answer cross-run questions about benchmark runs, skips, or
detector failures. An external series supplies that history.

The decision step in `.github/workflows/ci.yml` is the single source of truth
for `event_class`, `detector_status`, and `decision`. The publish step carries
those outputs unchanged. It must never recompute labels from event data or
changed paths, because a summary and its metric must describe the same decision.

## Metric and closed labels

The workflow emits `benchmark_gate_decisions_total`. Each emitted observation
uses the value `1`, and carries exactly the following labels:

| Label             | Allowed values                        | Meaning                        |
| ----------------- | ------------------------------------- | ------------------------------ |
| `event_class`     | `pull_request`, `other`               | Broad event type               |
| `detector_status` | `success`, `failure`, `unknown`       | Result of `dorny/paths-filter` |
| `decision`        | `run`, `skip`, `skip-detector-failed` | Benchmark admission decision   |

_Table 1: The metric's labels and their closed vocabularies._

The step validates all three values, including missing values, before it invokes
`curl`. An invalid or missing value produces a bounded warning and no sample.
No changed file path, command text, commit SHA, run ID, or timestamp may appear
in a label position. The OTLP `resource` object is empty as well. Grafana Cloud
documents that resource attributes can be promoted into labels and that service
attributes are mapped to `job` and `instance` or exposed via `target_info`; an
empty resource avoids adding labels beyond Table 1. See
[Grafana Cloud's OTLP format considerations][grafana-otlp].

## Sink and provisioning status

The proposed sink is Grafana Cloud's OTLP/JSON endpoint:
`https://otlp-gateway-<region>.grafana.net/otlp/v1/metrics`. The workflow uses
`curl` and reads two repository variables and one repository secret:

| Name                              | Kind     | Contents                              |
| --------------------------------- | -------- | ------------------------------------- |
| `BENCHMARK_TELEMETRY_ENDPOINT`    | variable | OTLP gateway URL                      |
| `BENCHMARK_TELEMETRY_INSTANCE_ID` | variable | Grafana Cloud instance ID             |
| `BENCHMARK_TELEMETRY_TOKEN`       | secret   | Access policy token (`metrics:write`) |

_Table 2: The sink configuration and its least-privilege credential._

Provisioning and receipt are not verified by this repository. Attempts to list
the GitHub repository's secrets and variables returned HTTP 403, and no known
Grafana Cloud stack credentials are available. A maintainer must request and
complete this configuration securely:

1. In Grafana Cloud, select the metrics stack and record its regional OTLP
   gateway URL and instance ID.
2. Create an access policy token with `metrics:write` only. Do not put the
   token in source, a workflow variable, a command argument, or a log.
3. Add the endpoint and instance ID as Actions repository variables, and add
   the token as the `BENCHMARK_TELEMETRY_TOKEN` Actions repository secret.
4. From a trusted repository run, check the workflow log for a successful
   publish without exposing the token. In Grafana Explore, query the metric
   over the last 7 days and verify a sample has exactly the three labels in
   Table 1. This is the receipt check; local tests cannot perform it.

Until the secret is present, the publish step is skipped. Forks and other runs
without access to repository secrets therefore produce no external sample. That
is the intended optional-integration degradation, and absence alone must not be
interpreted as a broken benchmark gate.

## Query surface

Use Grafana Explore with the Prometheus data source for one-off checks. Create
the suggested dashboard **Cuprum CI / Benchmark Gate** with panels for the
following queries:

```promql
# Observations by decision over the last 7 days.
sum by (decision) (
  count_over_time(benchmark_gate_decisions_total[7d])
)

# Pull-request detector failures over the last 7 days.
sum(count_over_time(
  benchmark_gate_decisions_total{
    event_class="pull_request",detector_status="failure"
  }[7d]
))

# Pull-request skips over the last 14 days.
sum(count_over_time(
  benchmark_gate_decisions_total{
    event_class="pull_request",decision="skip"
  }[14d]
))
```

`count_over_time` counts samples received in the selected range. It does not
count increments of a shared cumulative counter and does not establish
exactly-once workflow execution. Retries and lost requests affect the count,
and a backend may coalesce repeated samples with the same labels and timestamp.
Because every observation carries `1`, `rate()` and `increase()` are
inappropriate: they calculate changes in the sample value, not the number of
received observations.

The Grafana Cloud Free plan retains metrics for 14 days. Keep queries within 7
or 14 days unless the stack's paid retention plan is separately confirmed; the
current plan is documented on [Grafana Cloud pricing][grafana-pricing].

## Alerting

Create a Grafana alert rule named **Benchmark gate detector failures rising**.
Evaluate it every 1 hour and require the condition for 2 hours. The following
expression compares the pull-request detector-failure share in the latest 7-day
window with the preceding 7-day window, and requires at least 20 current
observations and one observation in the previous window:

```promql
(
  (sum(count_over_time(benchmark_gate_decisions_total{
    event_class="pull_request",decision="skip-detector-failed"
  }[7d])) or vector(0))
  /
  clamp_min(sum(count_over_time(
    benchmark_gate_decisions_total{event_class="pull_request"}[7d]
  )) or vector(0), 1)
) > (
  (sum(count_over_time(benchmark_gate_decisions_total{
    event_class="pull_request",decision="skip-detector-failed"
  }[7d] offset 7d)) or vector(0))
  /
  clamp_min(sum(count_over_time(
    benchmark_gate_decisions_total{event_class="pull_request"}[7d] offset 7d
  )) or vector(0), 1)
) + 0.10
and
sum(count_over_time(
  benchmark_gate_decisions_total{event_class="pull_request"}[7d]
)) >= 20
and
sum(count_over_time(
  benchmark_gate_decisions_total{event_class="pull_request"}[7d] offset 7d
)) > 0
```

The ten-percentage-point margin is a suggested starting point; maintainers own
the threshold and must select their Grafana notification contact point during
provisioning. The zero fallback handles a healthy window with no
detector-failure series. Configure this rule's No Data state as Normal; missing
data must be investigated alongside workflow warnings and provisioning state. A
missing secret, fork event, sink outage, or request loss can produce no sample
while the benchmark gate remains healthy, so an absence alert must not be
treated as proof of a gate failure.

## Failure and security contract

The publish step runs only when `BENCHMARK_TELEMETRY_TOKEN` is non-empty and
under the `!cancelled()` guard, so detector-failure decisions can be emitted.
It writes the credential to a temporary, mode-0600 `curl` configuration file;
the token does not appear in process arguments or output.

Invalid labels are rejected before transport with a bounded warning. A `curl`
failure also produces a warning and remains fail-open through
`continue-on-error: true`; it must never fail `changes` or suppress
`benchmark-ratchet`. The step summary remains the per-run human-readable
record, regardless of sink availability.

The payload is hand-built OTLP/JSON with a monotonic cumulative Sum and one
data point whose `asInt` is the string `"1"`. `startTimeUnixNano` is set to the
sample timestamp for the individual observation. It does not claim that the
backend has an exactly-once, process-wide counter; use the sample-count queries
above.

## Related records and tests

- [ADR-011: Durable benchmark-gate telemetry sink](adr-011-benchmark-gate-telemetry-sink.md)
  records the sink decision and its consequences.
- `tests/test_ci_benchmark_gate_telemetry.py` checks the workflow declaration.
- `tests/test_ci_benchmark_gate_telemetry_execution.py` parses the bytes sent
  to the stub transport and checks label safety and payload shape.

[grafana-otlp]: https://grafana.com/docs/grafana-cloud/observe-and-act/send-data/otlp/otlp-format-considerations/
[grafana-pricing]: https://grafana.com/pricing/
