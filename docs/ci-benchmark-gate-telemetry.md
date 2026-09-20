# Benchmark-gate telemetry

This contract is for Cuprum maintainers. It describes the durable benchmark
gate record stored with GitHub Actions artefacts, how to retrieve it, and how
to analyse trends without an external telemetry service.

## Problem and source of truth

The `changes` job decides whether `benchmark-ratchet` is admitted. Its
`$GITHUB_STEP_SUMMARY` table and `::notice::` annotation explain an individual
run, but they cannot answer cross-run questions about benchmark runs, skips, or
detector failures.

The decision step in `.github/workflows/ci.yml` is the single source of truth
for `event_class`, `detector_status`, and `decision`. The telemetry step copies
those outputs into one JSONL record. It must not recompute them from event data
or changed paths.

## Record contract

Each non-cancelled `changes` execution writes one line to `decisions.jsonl`.
The line is uploaded as the artefact `benchmark-gate-decision-${run_attempt}`
with a requested retention of 90 days. The existing `benchmark-ratchet` JSON
and Markdown reports also declare 90-day retention explicitly.

The schema is version 1:

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

The `labels` object contains exactly these bounded values:

| Label             | Allowed values                        | Meaning                      |
| ----------------- | ------------------------------------- | ---------------------------- |
| `event_class`     | `pull_request`, `other`               | Broad event type             |
| `detector_status` | `success`, `failure`, `unknown`       | `paths-filter` result        |
| `decision`        | `run`, `skip`, `skip-detector-failed` | Benchmark admission decision |

_Table 1: The metric's exact label set and closed vocabularies._

`run` means that the gate permits benchmark admission if the quality
prerequisites succeed; it does not prove that `benchmark-ratchet` actually
executed. `run_id`, `run_attempt`, and UTC `recorded_at` are metadata outside
`labels`. The two identifiers are decimal strings. `value` is always `1`; it is
an observation to count, not an exactly-once increment of a shared counter. No
changed path, command text, commit SHA, timestamp, secret, or other unbounded
value may become a label.

## Retention and delivery

The workflow requests 90 days for each decision artefact. The effective period
is limited by repository or organization policy and by artefact deletion, as
described in GitHub's
[artefact storage and retention guidance][github-artefacts]. Download archives
before expiry when longer retention is required.

Record writes and artefact uploads are fail-open. A failure emits a bounded
workflow warning and does not fail `changes` or change benchmark admission.
Because the record step uses `!cancelled()`, a detector failure is still
recorded. A cancelled run, missing context, or expired or deleted artefact is
unknown; it must not be represented as a zero decision.

When `env.ACT == 'true'`, artefact upload is skipped. The local harness
therefore needs no artefact service. Local tests verify the record and gate
outputs; hosted receipt is a separate check to perform after the workflow is
pushed.

## Retrieve records

Download one run's decision artefact with the GitHub CLI:

```bash
gh run download RUNID \
  --pattern 'benchmark-gate-decision-*' \
  --dir logs/RUNID
```

To retrieve recent CI runs, list their database IDs and download each archive
into a run-specific directory. A run without an artefact is expected for a
cancelled, failed-before-upload, or expired run. The message keeps omissions
visible without making one missing artefact fail the whole collection:

```bash
for run_id in $(gh run list --workflow ci.yml --limit 20 \
  --json databaseId --jq '.[].databaseId'); do
  if ! gh run download "$run_id" \
    --pattern 'benchmark-gate-decision-*' \
    --dir "logs/$run_id"; then
    printf 'decision artefact unavailable for run %s\n' "$run_id" >&2
  fi
done
```

Analysis must deduplicate by `(run_id, run_attempt)`. Re-downloading an
artefact or finding the same record in two local archives must not double the
count.

## Standard-library analysis

This runnable heredoc reads `logs/**/*.jsonl`, validates the schema and closed
labels, deduplicates run attempts, and writes `decisions.csv` and
`decisions.svg`. It uses only the Python standard library. Set
`INCLUDE_SHARES = False` to omit CSV share ratios. SVG labels are escaped even
though the label vocabulary is closed.

```bash
python - <<'PY'
from collections import Counter
from pathlib import Path
import csv
import html
import json

LABELS = ("event_class", "detector_status", "decision")
ALLOWED = {
    "event_class": {"pull_request", "other"},
    "detector_status": {"success", "failure", "unknown"},
    "decision": {"run", "skip", "skip-detector-failed"},
}
INCLUDE_SHARES = True
counts = Counter()
seen = set()
for path in sorted(Path("logs").rglob("*.jsonl")):
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, 1):
        record = json.loads(line)
        expected_keys = {
            "schema_version", "metric", "value", "labels", "run_id",
            "run_attempt", "recorded_at",
        }
        # `True == 1` in Python, so a bare `!= 1` would accept `true` for a
        # field the writer emits as an integer. The type checks below keep a
        # boolean from satisfying a numeric contract.
        if (
            set(record) != expected_keys
            or not isinstance(record["schema_version"], int)
            or isinstance(record["schema_version"], bool)
            or record["schema_version"] != 1
        ):
            raise ValueError(f"{path}:{line_number}: unsupported record")
        if record["metric"] != "benchmark_gate_decisions_total":
            raise ValueError(f"{path}:{line_number}: unexpected metric")
        if (
            not isinstance(record["value"], int)
            or isinstance(record["value"], bool)
            or record["value"] != 1
        ):
            raise ValueError(f"{path}:{line_number}: value must be 1")
        labels = record.get("labels")
        if not isinstance(labels, dict) or set(labels) != set(LABELS):
            raise ValueError(f"{path}:{line_number}: invalid label set")
        values = tuple(labels[name] for name in LABELS)
        if any(
            not isinstance(value, str) or value not in ALLOWED[name]
            for name, value in zip(LABELS, values)
        ):
            raise ValueError(f"{path}:{line_number}: invalid label value")
        recorded_at = record.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
            raise ValueError(f"{path}:{line_number}: recorded_at must be UTC")
        identity = (record.get("run_id"), record.get("run_attempt"))
        if not all(
            isinstance(value, str) and value.isascii() and value.isdecimal()
            for value in identity
        ):
            raise ValueError(f"{path}:{line_number}: invalid run identity")
        if identity in seen:
            continue
        seen.add(identity)
        counts[values] += 1

if not counts:
    raise SystemExit("no accepted benchmark-gate records found")

total = sum(counts.values())
with Path("decisions.csv").open("w", newline="", encoding="utf-8") as output:
    writer = csv.writer(output)
    writer.writerow([*LABELS, "count", "share"])
    for values, count in sorted(counts.items()):
        share = f"{count / total:.6f}" if INCLUDE_SHARES and total else ""
        writer.writerow([*values, count, share])

width, bar_start, bar_max, row_height = 1100, 400, 600, 24
height = 40 + max(1, len(counts)) * row_height
maximum = max(counts.values(), default=1)
bars = []
for index, (values, count) in enumerate(sorted(counts.items())):
    label = html.escape(" / ".join(values), quote=True)
    y = 20 + index * row_height
    bar_width = int(bar_max * count / maximum)
    bars.append(
        f'<text x="0" y="{y + 17}">{label}</text>'
        f'<rect x="{bar_start}" y="{y + 3}" width="{bar_width}" height="18" />'
        f'<text x="{bar_start + bar_width + 8}" y="{y + 17}">{count}</text>'
    )
Path("decisions.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" '
    f'viewBox="0 0 {width} {height}"><title>Benchmark gate observations</title>'
    '<style>text{font:13px sans-serif}rect{fill:#3465a4}</style>'
    + "".join(bars) + "</svg>\n",
    encoding="utf-8",
)
PY
```

## Alerting and operational checks

There is no automatic Grafana dashboard or alert. Existing failed-job
notifications and GitHub Actions notifications remain the immediate alerting
surface. Maintainers should periodically run the analysis recipe and inspect the
`skip-detector-failed` ratio. A missing artefact is a delivery or retention
question, not evidence of a zero-valued gate decision.

## Verified hosted receipt

Receipt was verified for commit [bbe408f][verified-commit] in
[run 35248836322][verified-run]. [Artefact 10508771038][verified-artefact],
named `benchmark-gate-decision-1`, was created at 2026-09-17 16:48:27 UTC and
expires at 2026-12-16 16:48:18 UTC according to GitHub's artefact API. The
downloaded `decisions.jsonl` contained a schema version 1 record with metric
`benchmark_gate_decisions_total`, value `1`, labels exactly `pull_request`/
`success`/`run`, run ID `35248836322`, attempt `1`, and `recorded_at`
`2026-09-17T16:48:26Z`. The dates validate the requested approximately 90-day
retention after artefact creation.

This verifies receipt and retention metadata for that run only. It does not
claim hosted harness execution or full CI completion. For subsequent changes,
run `changes` on a trusted event, download `benchmark-gate-decision-*`, and
validate one record's schema, exact labels, run metadata, and requested
retention. Local `act` tests cannot prove that GitHub accepted or retained the
artefact.

## Related records

- [ADR-014: Durable benchmark-gate telemetry](adr-014-benchmark-gate-telemetry-sink.md)
  records the superseding storage decision and consequences.
- [GitHub's artefact storage guidance][github-artefacts] defines the hosted
  retention policy.
- `tests/test_ci_benchmark_gate_telemetry.py` checks the workflow declaration.
- `tests/test_ci_benchmark_gate_telemetry_execution.py` checks record creation
  and fail-open behaviour.

[github-artefacts]: https://docs.github.com/en/actions/tutorials/store-and-share-data
[verified-commit]: https://github.com/leynos/cuprum/commit/bbe408f011d4a4c08d7c0e9f4ff3bf83c2b817a8
[verified-run]: https://github.com/leynos/cuprum/actions/runs/35248836322
[verified-artefact]: https://github.com/leynos/cuprum/actions/runs/35248836322/artifacts/10508771038
