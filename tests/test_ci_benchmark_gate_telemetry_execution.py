"""Execute the benchmark-gate publish step and assert on what it sends.

The declarations live next door in `tests/test_ci_benchmark_gate_telemetry.py`.
They cannot establish the two properties that matter most here: that a label
name is bounded while its *value* is not, and that a payload can name the right
fields and still be malformed. These tests extract the real `run:` block from
`ci.yml`, execute it under `bash` with a stub `curl`, and assert against the
request body the step actually handed over.

The assertions are on bytes rather than on the script text for the same reason:
a label whose value varied per run would still be *called* `event_class`, and
the series count would grow with the repository's history while every textual
assertion passed.

The operational contract — query surface, retention, alerting — is
`docs/ci-benchmark-gate-telemetry.md`.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.benchmark_gate_telemetry import (
    EXAMPLE_CREDENTIAL,
    LABEL_NAMES,
    Sink,
    Verdict,
    parse_payload,
    publish_env,
    publish_script,
    run_publish_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.benchmark_gate_telemetry import Payload
    from tests.helpers.workflow import Workflow

#: The verdict for a performance-relevant pull request, used wherever a test
#: needs *a* recorded run rather than three specific values.
RECORDED_RUN = Verdict("pull_request", "success", "run")

#: The metric name, verbatim, and the reason the published name survives the
#: sink's translation: the OpenTelemetry-to-Prometheus rule appends `_total` to
#: a monotonic Sum whose name lacks it, and leaves a name that carries it alone.
METRIC_NAME = "benchmark_gate_decisions_total"

#: Each label's closed vocabulary, so a query can enumerate the series.
LABEL_VOCABULARIES = {
    "event_class": frozenset({"pull_request", "other"}),
    "detector_status": frozenset({"success", "failure", "unknown"}),
    "decision": frozenset({"run", "skip", "skip-detector-failed"}),
}

#: Resource attribute keys that become labels after translation. Every other
#: resource attribute is dropped, so this is the whole resource surface; an
#: `instance` label in particular would give the series one identity per run.
RESOURCE_LABELS = frozenset({"service.name", "service.namespace"})

#: The gate's own rows, which the publish step must not re-derive. The verdict
#: is computed once, in the summary step, and transported; two computations of
#: one decision can disagree, and each is keyed by the gate row that produces
#: it — `event|detector|bench`, with an empty field where the gate recorded
#: nothing.
GATE_ROWS = {
    "pull_request|success|true": Verdict("pull_request", "success", "run"),
    "pull_request|success|false": Verdict("pull_request", "success", "skip"),
    "push|success|false": Verdict("other", "success", "run"),
    "pull_request|failure|": Verdict("pull_request", "failure", "skip-detector-failed"),
    "pull_request||": Verdict("pull_request", "unknown", "skip-detector-failed"),
}


def test_the_publish_step_does_not_fail_the_job_when_the_sink_rejects_it(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """A non-zero `curl` must not propagate as a script failure.

    `continue-on-error` protects the job, but a script that exits non-zero on a
    transport error would still be reported as a failed step, which is the
    signal a maintainer reads as "the gate is broken".
    """
    run = run_publish_script(
        verdict=RECORDED_RUN,
        workflow_data=workflow_data,
        tmp_path=tmp_path,
        sink=Sink(exit_code=22),
    )
    assert run.exit_code == 0, (
        "the publish script must exit 0 when the sink rejects the request, so a "
        f"telemetry outage is not reported as a gate failure; stderr was:\n"
        f"{run.stderr}"
    )
    assert "::notice" in run.stdout or "::warning" in run.stdout, (
        f"a rejected publish must still be reported; output was:\n{run.stdout}"
    )


def test_the_publish_step_sends_the_credential_only_to_curl(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Keep the credential off the command line and out of the log.

    `curl -u` would put the token in the process arguments, where it is visible
    to any other process on the runner. A `--config` file keeps it out of the
    argument list, and the script must not echo it either.
    """
    run = run_publish_script(
        verdict=RECORDED_RUN,
        workflow_data=workflow_data,
        tmp_path=tmp_path,
    )
    assert run.credential == EXAMPLE_CREDENTIAL, (
        "the stub curl must observe the credential through its environment or "
        f"config, so the script can authenticate; argv was {run.argv!r}"
    )
    assert not any(EXAMPLE_CREDENTIAL in argument for argument in run.argv), (
        f"the credential must not appear in curl's arguments; argv was {run.argv!r}"
    )
    assert EXAMPLE_CREDENTIAL not in run.stdout, (
        "the publish step must never print the credential"
    )


def test_the_payload_is_a_cumulative_monotonic_sum(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Pin the temporality the sink's translator requires.

    The Prometheus remote-write translation drops non-cumulative monotonic
    sums, so a delta or non-monotonic payload is accepted, acknowledged, and
    then discarded. Nothing in the workflow's own output would show that, which
    is why the encoding is asserted on the emitted bytes.
    """
    payload = _payload(tmp_path, workflow_data)
    assert payload.metric_name == METRIC_NAME, (
        f"the export must declare {METRIC_NAME!r}; found {payload.metric_name!r}"
    )
    assert payload.summation.get("isMonotonic") is True, (
        "the Sum must be monotonic, or the sink will not treat it as a counter"
    )
    assert payload.summation.get("aggregationTemporality") == 2, (
        "the Sum must be cumulative (2): the remote-write exporter drops a "
        "delta-encoded monotonic sum; found "
        f"{payload.summation.get('aggregationTemporality')!r}"
    )
    assert payload.point.get("asInt") == "1", (
        "each run contributes one observation, and 64-bit integers are encoded "
        f"as decimal strings in OTLP/JSON; found {payload.point.get('asInt')!r}"
    )


def test_the_data_point_declares_a_start_time(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """A start time equal to the sample time means "reset, unknown start".

    That is what lets one long-lived series accumulate across runs. Omitting
    the field or leaving it at zero would instead describe a series that began
    at the epoch, which is a different and false claim.
    """
    payload = _payload(tmp_path, workflow_data)
    start = payload.point.get("startTimeUnixNano")
    sample = payload.point.get("timeUnixNano")
    assert isinstance(start, str), (
        f"startTimeUnixNano must be a string; found {start!r}"
    )
    assert isinstance(sample, str), f"timeUnixNano must be a string; found {sample!r}"
    assert start.isdigit(), (
        f"startTimeUnixNano must be a decimal string; found {start!r}"
    )
    assert sample.isdigit(), f"timeUnixNano must be a decimal string; found {sample!r}"
    assert start == sample, (
        "startTimeUnixNano must equal timeUnixNano, so the sink records a reset "
        f"at an unknown start time rather than a series beginning at the epoch; "
        f"found start={start!r} sample={sample!r}"
    )


def test_the_metric_carries_exactly_the_three_bounded_labels(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Fix the label set from the bytes the step actually sends.

    Reading the label names out of the script would pass for a script that
    computed a fourth label and forgot to print it, or that printed the three
    alongside a debug field. The parity is taken from the payload instead,
    because that is what the sink receives and counts.
    """
    payload = _payload(tmp_path, workflow_data)
    assert set(payload.labels) == set(LABEL_NAMES), (
        f"the metric must carry exactly the labels {sorted(LABEL_NAMES)}; the "
        f"payload carried {sorted(payload.labels)}"
    )


@pytest.mark.parametrize("verdict", GATE_ROWS.values(), ids=GATE_ROWS.keys())
def test_every_recorded_verdict_is_published_verbatim(
    tmp_path: pth.Path, workflow_data: Workflow, verdict: Verdict
) -> None:
    """Publish each verdict the gate can record, unchanged.

    The gate's row space is the whole input space the step has: two closed sets
    and a decision. Enumerating it keeps the metric from drifting away from the
    table it is derived from, which is a divergence no run would reveal.
    """
    payload = _payload(tmp_path, workflow_data, verdict)
    assert payload.labels == verdict.as_labels(), (
        f"the payload must publish the recorded verdict verbatim; found "
        f"{payload.labels}"
    )


@pytest.mark.parametrize("label", sorted(LABEL_NAMES))
def test_every_label_value_stays_inside_its_closed_vocabulary(
    label: str, tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Every emitted label value must be one the vocabulary enumerates.

    This is the check that makes the label set finite in practice rather than
    in name: a label whose value comes from the event payload would still be
    called `event_class`, and would still grow the series count with the
    repository's history.
    """
    payload = _payload(tmp_path, workflow_data)
    value = payload.labels[label]
    assert value in LABEL_VOCABULARIES[label], (
        f"{label!r} must be one of {sorted(LABEL_VOCABULARIES[label])}; the payload "
        f"carried {value!r}"
    )


def test_the_payload_exposes_no_unbounded_label_values(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Refuse repository content and per-run identity in every label position.

    The sink is an external service and its series budget is finite, so a label
    that varies per run or per file either exhausts the budget or publishes
    repository content. Both data point attributes and resource attributes are
    checked, because the translation folds resource attributes into labels too.
    """
    payload = _payload(tmp_path, workflow_data)
    assert set(payload.resource_attributes) <= RESOURCE_LABELS, (
        "only service.name and service.namespace become resource-derived labels; "
        "every other resource attribute is dropped, and an instance identifier "
        f"would create one series per run; found {sorted(payload.resource_attributes)}"
    )
    unbounded = {
        name: value for name, value in payload.labels.items() if not _is_bounded(value)
    }
    assert not unbounded, (
        f"every data point attribute must be a short, structure-free token; "
        f"found {unbounded!r}"
    )
    for name, value in payload.resource_attributes.items():
        assert _is_bounded(value), (
            f"resource label {name!r} must stay bounded; found {value!r}"
        )


def test_the_payload_is_well_formed_otlp_json(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Parse the emitted body, so a typo cannot pass as a plausible payload.

    The step builds JSON with `printf` rather than a serializer, so a stray
    quote would be accepted by every textual assertion above and rejected by
    the sink. `parse_payload` decodes it, which is the check that matters.
    """
    payload = _payload(tmp_path, workflow_data)
    assert payload.point, "the export must carry a data point"
    assert payload.summation, "the export must carry a Sum"
    assert isinstance(payload.document, dict), "the export must be a JSON object"


def test_the_sink_is_addressed_through_repository_configuration(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """Address the sink from configuration a maintainer can retarget.

    A literal endpoint in the workflow body would be undeployable without a
    code change, and a literal credential would be published to everyone who
    can read the repository.
    """
    env = publish_env(workflow_data)
    values = " ".join(value for value in env.values() if isinstance(value, str))
    assert "vars." in values, (
        f"the sink endpoint must be supplied through a `vars.*` repository "
        f"variable; found {sorted(env)}"
    )
    script = publish_script(workflow_data)
    assert "https://otlp-gateway" not in script, (
        "the endpoint must not be hard-coded in the script body"
    )
    assert "glc_" not in script, "the script must not embed a credential"


def test_the_metric_name_keeps_its_total_suffix() -> None:
    """Keep the name translation-stable.

    The OpenTelemetry-to-Prometheus translation appends `_total` to a monotonic
    Sum whose name lacks it, and leaves a name that already carries it alone.
    Publishing the final name means the documented query does not depend on the
    translator's suffixing rule.
    """
    assert METRIC_NAME.endswith("_total"), (
        f"{METRIC_NAME!r} must already end in '_total', so the translation leaves "
        f"the published name unchanged"
    )
    assert METRIC_NAME.islower(), "metric names must be lower-case snake case"


def _payload(
    tmp_path: pth.Path,
    workflow_data: Workflow,
    verdict: Verdict = RECORDED_RUN,
) -> Payload:
    """Run the publish step and parse the export it handed to ``curl``."""
    run = run_publish_script(
        verdict=verdict,
        workflow_data=workflow_data,
        tmp_path=tmp_path,
    )
    assert run.body, (
        f"the publish step must hand curl an OTLP/JSON body; argv was {run.argv!r} "
        f"and stderr was:\n{run.stderr}"
    )
    return parse_payload(run.body)


def _is_bounded(value: str) -> bool:
    """Return whether ``value`` is a short, structure-free label value."""
    return 0 < len(value) < 64 and not any(character in value for character in "/ \t")
