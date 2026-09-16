# Benchmark-gate telemetry

Cuprum's `changes` job decides whether the benchmark job runs, and records that
decision in the step summary. A step summary is a run artefact: it is readable
for as long as the run record is retained, and it is readable one run at a
time. Nothing about it answers "has this gate been skipping more pull requests
than it used to?", which is the question that tells a maintainer whether the
path filter has drifted away from the code it was meant to select.

The `changes` job therefore publishes the same decision to an external sink,
where it accumulates into a series that can be queried and alerted on. This
document records what is published, where it lands, how to query it, and what
it deliberately does not contain.

## The metric

One sample per run, named `benchmark_gate_decisions_total`, carrying exactly
three labels:

| Label             | Values                                |
| ----------------- | ------------------------------------- |
| `event_class`     | `pull_request`, `other`               |
| `detector_status` | `success`, `failure`, `unknown`       |
| `decision`        | `run`, `skip`, `skip-detector-failed` |

_Table 1: The metric's labels and their closed vocabularies._

All three vocabularies are finite and are asserted closed by
`tests/test_ci_benchmark_gate_telemetry_execution.py`, which reads them out of
the request body the step actually sent rather than out of the script text.
That distinction matters: a label whose value came from the event payload would
still be _named_ `event_class`, and would still grow the series count with the
repository's history.

`decision` is the operative value. It is computed once, by the step that
records the summary, and the publish step transports that value rather than
recomputing it — so the table a maintainer reads in the run and the series a
query returns cannot disagree.

## Where it lands

The sink is **Grafana Cloud**, reached over its OTLP gateway at
`https://otlp-gateway-<region>.grafana.net/otlp/v1/metrics`. Three repository
variables and one secret configure it:

| Name                                 | Kind     | Holds                         |
| ------------------------------------ | -------- | ----------------------------- |
| `BENCHMARK_TELEMETRY_ENDPOINT`       | variable | the gateway URL               |
| `BENCHMARK_TELEMETRY_INSTANCE_ID`    | variable | the Grafana Cloud instance ID |
| `BENCHMARK_TELEMETRY_TOKEN`          | secret   | an access policy token        |
| `BENCHMARK_TELEMETRY_TOKEN` (absent) | —        | the step does not run at all  |

_Table 2: The configuration the sink reads, and what each part holds._

The token needs the `metrics:write` scope and nothing else. It is pushed by
`curl`, with the credential written to a `mktemp`-created file (mode 0600) and
passed with `--config`, never with `--user`. That is deliberate: `-u` would put
the token in the process arguments, where any other process on the runner can
read it, and it would appear in a failing step's logs. The repository's own
tests assert the credential reaches `curl` and nothing else.

The push is **best-effort**. The step is gated on the secret being non-empty
and set `continue-on-error: true`, because the failure mode of a telemetry
outage must not be a failed `changes` job: `changes` failing skips the
benchmark job, so a telemetry problem would silently stop the baseline being
refreshed and turn into a benchmarking problem. A failed push is reported as a
workflow notice in the run, which is where a maintainer will see it.

Until the secret exists, the step is skipped and the metric is empty. That is
the intended pre-deployment state, not a misconfiguration.

## Querying it

The series accumulates one sample per run. Because the published value is always
`1` and the counter is cumulative, **the count is in the sample timestamps,
not in the sample values**, so `rate()` and `increase()` do not work: both
compute `last - first` over the window, and a series whose value is `1` at
every sample has a difference of zero at every window size. They return 0, not
because the telemetry is broken, but because the arithmetic has no information
to work with.

Use `count_over_time` instead:

```promql
# Decided skips over the last 30 days, by decision.
sum by (decision) (count_over_time(benchmark_gate_decisions_total[30d]))

# Pull requests where the detector itself failed, over the last 7 days.
sum(count_over_time(
  benchmark_gate_decisions_total{event_class="pull_request",detector_status="failure"}[7d]
))

# Share of pull requests the gate skipped, over the last 30 days.
sum(count_over_time(benchmark_gate_decisions_total{decision="skip"}[30d]))
  /
sum(count_over_time(benchmark_gate_decisions_total{event_class="pull_request"}[30d]))
```

The `30d` range is not a display window; `count_over_time` counts the samples
in the range you give it, so the range is the reporting period. Grafana's range
is not the same thing and does not substitute for it.

A useful invariant to put on a dashboard: `count_over_time` over a period,
grouped by `decision`, should sum to the number of runs in that period. A short
total means pushes are being dropped — either the sink rejected them, or the
secret was rotated away — and each drop is reported as a workflow notice in the
run that suffered it.

## Retention and alerting

The series is retained under whatever retention the Grafana Cloud metrics
instance is configured with; the repository does not pin it. Two alert rules
are worth having, and both are stated in `count_over_time` terms:

- **Telemetry has stopped.** No samples for longer than the longest expected
  gap between default-branch runs. This catches a rotated secret or a rejected
  endpoint, neither of which fails a run and so neither of which is visible
  without looking for it.
- **The gate has stopped skipping.** `decision="skip"` absent over a long
  window while `decision="run"` continues. A path filter that matches
  everything is indistinguishable from a healthy gate on any single run.

Alert on absence, not on a threshold crossing. A best-effort push can fail for
one run without the telemetry being broken, but a metric that has been quiet
for a week is a statement about the pipeline.

## What is never published

No changed file path, no command text, no commit SHA, no run identifier, and no
timestamp appears as a label value. Each of those would either publish
repository content to a third party or give the series one identity per run,
and a series with one identity per run counts nothing. The check is not
textual: the tests parse the emitted payload and assert every label value is a
member of the vocabularies in Table 1, and that the resource attributes are
limited to `service.name` and `service.namespace`.

`service.instance.id` is deliberately absent. The OTLP-to-Prometheus
translation turns it into an `instance` label, which would split the series per
run; `service.name` and `service.namespace` become the `job` label and identify
the sender without splitting it.

## Wire format

The payload is hand-built OTLP/JSON, sent as `application/json`, carrying a
monotonic Sum with `aggregationTemporality: 2` (cumulative). Two encoding
choices are load-bearing rather than cosmetic:

- **Cumulative, not delta.** Grafana Cloud's remote-write translation drops
  non-cumulative monotonic sums, so a delta-encoded payload is accepted,
  acknowledged, and then discarded. Nothing in the workflow's output would show
  that. The tests assert the temporality on the emitted bytes.
- **`startTimeUnixNano` equal to `timeUnixNano`.** This declares "a new
  unbroken sequence of observations begins with a reset at an unknown start
  time", which is what lets one long-lived series accumulate across runs.
  Leaving it unset or at zero would instead describe a series that began at the
  epoch — a different and false claim.

The metric is named `benchmark_gate_decisions_total` with the `_total` suffix
already applied, because the translation appends `_total` to a monotonic Sum
whose name lacks it and leaves a name that already carries it unchanged.
Publishing the final name means the documented query does not depend on the
translator's suffixing rule.

## Related

- `docs/adr-011-benchmark-gate-telemetry-sink.md` records why the sink, the
  wire format, and the secret-gated push were chosen.
- `docs/adr-012-actions-runner-integration-harness.md` records the harness that
  exercises the workflow boundary offline.
- `.github/workflows/ci.yml` declares the step; the contract tests in
  `tests/test_ci_benchmark_gate_telemetry.py` and
  `tests/test_ci_benchmark_gate_telemetry_execution.py` hold it to this
  document.
