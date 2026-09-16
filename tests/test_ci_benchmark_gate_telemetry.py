"""Contract tests for benchmark-gate telemetry published outside GitHub.

The `changes` job computes a bounded verdict on every run and records it in the
step summary, where it survives only as long as the run record does. The
publish step sends that same verdict to an external sink so that the trend is
queryable and alertable. Every property that makes the metric safe to publish
is declared in `ci.yml` — which credential gates it, whether it may fail the
job, which label names it carries, and what may appear in a label position —
and each fails in a direction no ordinary test notices:

- drop the credential gate and every fork, and every run before the secret
  exists, posts a request to a third-party service with an empty credential;
- drop the fail-open guard and a sink outage becomes a failed `changes` job,
  which skips `benchmark-ratchet` and stops the baseline being refreshed, so a
  telemetry problem becomes a benchmarking problem;
- let a changed file path, command line, commit SHA, run identifier, or
  timestamp reach a label position and the series count grows with the
  repository's history until the sink drops data, while publishing repository
  content to an external service.

These tests read the declarations. That half is necessary but not sufficient:
a label name can be bounded while its value is not, and a payload can name the
right fields and still be malformed. Its sibling module,
`tests/test_ci_benchmark_gate_telemetry_execution.py`, runs the real `run:`
block with a stub `curl` and asserts against the request body the step actually
produced. The operational contract — query surface, retention, alerting — is
`docs/ci-benchmark-gate-telemetry.md`.
"""

from __future__ import annotations

import typing as typ

import yaml

from tests.helpers.benchmark_gate_telemetry import (
    PUBLISH_STEP,
    VALUE_INPUTS,
    publish_env,
    publish_script,
)
from tests.helpers.workflow import CHANGES_JOB, script_of, step_named, steps

if typ.TYPE_CHECKING:
    from tests.helpers.workflow import Workflow

SUMMARY_STEP = "Record the benchmark gate decision"

#: The metric name, verbatim. It already ends in `_total`, which is the suffix
#: the OpenTelemetry-to-Prometheus translation would otherwise append to a
#: monotonic Sum, so the published name survives translation unchanged and the
#: documented query does not depend on the translator's suffixing rule.
METRIC_NAME = "benchmark_gate_decisions_total"


def _publish_step(workflow_data: Workflow) -> dict[str, object]:
    """Return the declared telemetry-publish step of the `changes` job."""
    return step_named(workflow_data, CHANGES_JOB, PUBLISH_STEP)


def test_the_changes_job_publishes_the_gate_decision(
    workflow_data: Workflow,
) -> None:
    """The publish step must exist, and must follow the summary step.

    Ordering is what lets the publish step transport the verdict rather than
    recompute it, which is the difference between one computation and two that
    can disagree.
    """
    names = [step.get("name") for step in steps(workflow_data, CHANGES_JOB)]
    assert SUMMARY_STEP in names, f"the {CHANGES_JOB!r} job must record the decision"
    assert PUBLISH_STEP in names, (
        f"the {CHANGES_JOB!r} job must declare a {PUBLISH_STEP!r} step; found {names}"
    )
    assert names.index(SUMMARY_STEP) < names.index(PUBLISH_STEP), (
        f"{PUBLISH_STEP!r} must follow {SUMMARY_STEP!r}, whose outputs it publishes; "
        f"found {names}"
    )


def test_the_publish_step_is_gated_on_the_sink_credential(
    workflow_data: Workflow,
) -> None:
    """Send nothing at all when the credential is absent.

    The guard matches the repository's existing optional-integration
    convention: no secret, no request, and no failure. It reads the secret into
    the step environment first, because a `secrets` context is not available to
    an `if:` expression directly.
    """
    condition = _publish_step(workflow_data).get("if")
    assert isinstance(condition, str), (
        f"the {PUBLISH_STEP!r} step must declare an `if:` guard on its credential; "
        f"found {condition!r}"
    )
    for token in ("env.", "!=", "''"):
        assert token in condition, (
            f"the {PUBLISH_STEP!r} guard must test the credential for emptiness, "
            f"which requires {token!r}; found {condition!r}"
        )
    env = publish_env(workflow_data)
    assert "TOKEN" in "".join(env), (
        f"the credential must be exposed through the step environment before the "
        f"guard can test it; found {sorted(env)}"
    )
    from_secrets = [value for value in env.values() if isinstance(value, str)]
    assert any("secrets." in value for value in from_secrets), (
        f"the credential must come from `secrets`; found {sorted(env)}"
    )


def test_the_publish_step_is_fail_open_and_reports_failure(
    workflow_data: Workflow,
) -> None:
    """A sink that is down must cost a notice, not the run.

    Failing `changes` would skip `benchmark-ratchet`, so a telemetry outage
    would silently stop the baseline being refreshed. The step therefore
    tolerates its own failure, and must still surface that failure rather than
    swallowing it — a metric that quietly stopped would otherwise look like a
    metric nobody triggers.
    """
    step = _publish_step(workflow_data)
    assert step.get("continue-on-error") is True, (
        f"the {PUBLISH_STEP!r} step must set `continue-on-error: true`, so a sink "
        f"outage cannot fail {CHANGES_JOB!r} and skip the benchmark job"
    )
    script = publish_script(workflow_data)
    assert "::notice" in script or "::warning" in script, (
        "a failed publish must be reported as a workflow notice or warning; "
        f"the script was:\n{script}"
    )


def test_the_publish_step_transports_the_recorded_verdict(
    workflow_data: Workflow,
) -> None:
    """Read the verdict from the gate step, never recompute it.

    The three values are the gate's own outputs. Sourcing them from the event
    payload or the detector directly would be a second computation of a
    decision that already exists, and the table a maintainer reads and the
    series a query returns could then disagree.
    """
    env = publish_env(workflow_data)
    missing = [name for name in VALUE_INPUTS if name not in env]
    assert not missing, (
        f"the {PUBLISH_STEP!r} step must read {missing!r} from the values the "
        f"summary step computed; found {sorted(env)}"
    )
    referenced = {name: value for name, value in env.items() if isinstance(value, str)}
    assert any("steps." in value for value in referenced.values()), (
        f"the bounded values must come from the gate step's outputs; found "
        f"{sorted(referenced)}"
    )


def test_the_declared_script_round_trips_through_yaml(
    workflow_data: Workflow,
) -> None:
    """Keep every assertion here describing the text that actually executes.

    These tests read the script out of the parsed workflow, and the sibling
    module executes it. A `run:` block folded or escaped differently from what
    Actions executes would leave both describing a script that never runs.
    """
    script = script_of(_publish_step(workflow_data))
    assert script is not None, (
        f"the {PUBLISH_STEP!r} step must run a script for the round trip to mean "
        f"anything"
    )
    assert yaml.safe_load(yaml.safe_dump(script)) == script, (
        "the publish step's script must survive a YAML round trip"
    )
