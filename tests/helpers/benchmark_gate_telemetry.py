"""Execute the benchmark-gate publish step and parse its OTLP/JSON export.

The publish step builds its payload with `printf` and sends it with `curl`, so
the only thing standing between a typo and a silently rejected export is the
exact bytes it prints. Reading the script's source would confirm the words and
not the bytes, so this helper runs the real `run:` block with a stub `curl` on
`PATH` and hands back the request body the step actually produced.

The step touches only its own environment, a temporary payload file, and
`curl`, which is what makes running it outside Actions meaningful rather than a
simulation of it.
"""

from __future__ import annotations

import dataclasses as dc
import json
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests execute the checked-in workflow script.
import typing as typ

from tests.helpers.workflow import CHANGES_JOB, script_of, step_named

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from tests.helpers.workflow import Workflow

PUBLISH_STEP = "Publish the benchmark gate decision"
SUMMARY_STEP = "Record the benchmark gate decision"

#: Prefix the OTLP/JSON body is passed to `curl` with, so the stub can find it.
BODY_PREFIX = "@"

#: Metric label names, in the order the payload declares them. These are the
#: names the sink counts series against, so they are the bounded part of the
#: metric's identity.
LABEL_NAMES = ("event_class", "detector_status", "decision")

#: Environment names carrying the three bounded values, in the same order. The
#: publish step maps each from the gate step's outputs rather than recomputing.
VALUE_INPUTS = ("EVENT_CLASS", "DETECTOR_STATUS", "DECISION")

#: A stand-in credential. It is obviously not one, and the tests assert it
#: reaches `curl` and nothing else; no real credential enters the repository.
EXAMPLE_CREDENTIAL = "glc-example-credential"

_STUB = """#!/bin/sh
# Record the transport and body of one curl invocation for test inspection.
printf '%s\\n' "$@" > "${CURL_ARGV_CAPTURE}"
printf '%s\\n' "${SINK_TOKEN:-}" > "${CURL_TOKEN_CAPTURE}"
for argument in "$@"; do
  case "${argument}" in
    @*) cp "${argument#@}" "${CURL_BODY_CAPTURE}" ;;
  esac
done
exit "${CURL_STUB_EXIT:-0}"
"""


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


@dc.dataclass(frozen=True, slots=True)
class Verdict:
    """The three bounded values a run records, in label order.

    Attributes
    ----------
    event_class : str
        Event class the gate derived from the triggering event name.
    detector_status : str
        Detector outcome the gate observed.
    decision : str
        Benchmark decision the gate reached.
    """

    event_class: str
    detector_status: str
    decision: str

    def as_inputs(self) -> dict[str, str]:
        """Return the verdict as the publish step's environment inputs."""
        return dict(
            zip(
                VALUE_INPUTS,
                (self.event_class, self.detector_status, self.decision),
                strict=True,
            )
        )

    def as_labels(self) -> dict[str, str]:
        """Return the verdict keyed by the label names it is published under."""
        return dict(
            zip(
                LABEL_NAMES,
                (self.event_class, self.detector_status, self.decision),
                strict=True,
            )
        )


@dc.dataclass(frozen=True, slots=True)
class Sink:
    """The telemetry sink as the publish script experiences it.

    Attributes
    ----------
    endpoint : str
        OTLP/HTTP endpoint the script posts to.
    instance_id : str
        Account identifier the script authenticates as.
    credential : str
        Credential the script authenticates with.
    exit_code : int
        Status the transport answers with. A non-zero value stands in for a
        refused connection, a timeout, or an HTTP error, each of which the
        script must survive without failing the job.
    """

    endpoint: str = "https://otlp-gateway.example.grafana.net/otlp/v1/metrics"
    instance_id: str = "1234567"
    credential: str = EXAMPLE_CREDENTIAL
    exit_code: int = 0


@dc.dataclass(frozen=True, slots=True)
class PublishRun:
    """Represent one execution of the publish step.

    Attributes
    ----------
    exit_code : int
        Process exit status of the script.
    stdout : str
        Everything the script wrote to standard output.
    stderr : str
        Everything the script wrote to standard error.
    argv : tuple[str, ...]
        Arguments the script passed to `curl`, one per line in the capture.
    body : str
        Request body the script asked `curl` to send, or an empty string when
        the script never handed `curl` a body.
    credential : str
        Credential value as `curl` observed it.
    """

    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]
    body: str
    credential: str


@dc.dataclass(frozen=True, slots=True)
class Payload:
    """Represent a parsed OTLP/JSON export.

    Attributes
    ----------
    document : dict[str, object]
        Parsed export body.
    resource_attributes : dict[str, str]
        Resource-level attributes, which the sink folds into metric labels.
    metric_name : str
        Name declared on the exported metric.
    summation : dict[str, object]
        The exported Sum, carrying monotonicity and temporality.
    point : dict[str, object]
        The Sum's first data point.
    labels : dict[str, str]
        Data point attributes, which become the metric's own labels.
    """

    document: dict[str, object]
    resource_attributes: dict[str, str]
    metric_name: str
    summation: dict[str, object]
    point: dict[str, object]
    labels: dict[str, str]


def publish_script(workflow_data: Workflow) -> str:
    """Return the publish step's shell script, as ``ci.yml`` declares it."""
    script = script_of(step_named(workflow_data, CHANGES_JOB, PUBLISH_STEP))
    _require(
        condition=script is not None,
        message=f"the {PUBLISH_STEP!r} step must run a script",
    )
    return typ.cast("str", script)


def publish_env(workflow_data: Workflow) -> dict[str, object]:
    """Return the publish step's ``env:`` mapping."""
    declared = step_named(workflow_data, CHANGES_JOB, PUBLISH_STEP).get("env")
    _require(
        condition=isinstance(declared, dict),
        message=f"the {PUBLISH_STEP!r} step must declare env",
    )
    return typ.cast("dict[str, object]", declared)


def run_publish_script(
    *,
    verdict: Verdict,
    workflow_data: Workflow,
    tmp_path: pth.Path,
    sink: Sink | None = None,
) -> PublishRun:
    """Execute the real publish script against a stub ``curl``.

    Parameters
    ----------
    verdict : Verdict
        The three bounded values the gate step would have published.
    workflow_data : tests.helpers.workflow.Workflow
        Parsed workflow fixture, supplied at test execution rather than import.
    tmp_path : pathlib.Path
        Pytest temporary directory for the stub, its captures, and the payload.
    sink : Sink | None
        The sink as the script experiences it, including the status the
        transport answers with. Defaults to a successful publish.

    Returns
    -------
    PublishRun
        Exit status, output, ``curl`` arguments, request body, and the
        credential as ``curl`` observed it.
    """
    resolved = sink if sink is not None else Sink()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    argv_capture = tmp_path / "curl-argv.txt"
    body_capture = tmp_path / "curl-body.json"
    credential_capture = tmp_path / "curl-credential.txt"
    stub = stub_dir / "curl"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - literal vector plus workflow run block; no test input reaches the command line.
        ["/usr/bin/env", "bash", "-c", publish_script(workflow_data)],
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SINK_ENDPOINT": resolved.endpoint,
            "SINK_INSTANCE_ID": resolved.instance_id,
            "SINK_TOKEN": resolved.credential,
            "CURL_STUB_EXIT": str(resolved.exit_code),
            "CURL_ARGV_CAPTURE": str(argv_capture),
            "CURL_BODY_CAPTURE": str(body_capture),
            "CURL_TOKEN_CAPTURE": str(credential_capture),
            **verdict.as_inputs(),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return PublishRun(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        argv=tuple(_read(argv_capture).splitlines()),
        body=_read(body_capture),
        credential=_read(credential_capture).strip(),
    )


def parse_payload(captured: str) -> Payload:
    """Parse a captured OTLP/JSON export into its declared shape.

    Parameters
    ----------
    captured : str
        Request body the publish script handed to ``curl``.

    Returns
    -------
    Payload
        Parsed resource attributes, metric name, Sum, data point, and labels.
    """
    document = typ.cast("dict[str, object]", json.loads(captured))
    resource = _at(document, ["resourceMetrics"], 0, "resourceMetrics")
    scope = _at(resource, ["scopeMetrics"], 0, "scopeMetrics")
    metric = _at(scope, ["metrics"], 0, "metrics")
    summation = typ.cast("dict[str, object]", metric.get("sum"))
    _require(condition=bool(summation), message="the export must carry a Sum")
    point = _at(summation, ["dataPoints"], 0, "dataPoints")
    return Payload(
        document=document,
        resource_attributes=_attributes(resource.get("resource")),
        metric_name=typ.cast("str", metric.get("name")),
        summation=summation,
        point=point,
        labels=_attributes(point),
    )


def _read(path: pth.Path) -> str:
    """Return a capture file's text, or an empty string when absent."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _at(
    parent: object, keys: cabc.Sequence[str], index: int, field: str
) -> dict[str, object]:
    """Walk ``keys`` into ``parent`` and return element ``index``."""
    found: object = parent
    for key in keys:
        _require(
            condition=isinstance(found, dict),
            message=f"expected a mapping under {key!r}",
        )
        found = typ.cast("dict[str, object]", found).get(key)
    _require(
        condition=isinstance(found, list), message=f"expected a list under {field!r}"
    )
    return typ.cast("dict[str, object]", _element(found, index, field))


def _element(found: object, index: int, field: str) -> object:
    """Return element ``index`` of a validated list."""
    elements = typ.cast("list[object]", found)
    _require(
        condition=len(elements) > index,
        message=f"expected an element at {field}[{index}]",
    )
    return elements[index]


def _attributes(container: object) -> dict[str, str]:
    """Return an OTLP attribute list as a plain mapping."""
    _require(
        condition=isinstance(container, dict), message="expected an attribute container"
    )
    declared = typ.cast("dict[str, object]", container).get("attributes") or []
    _require(condition=isinstance(declared, list), message="expected an attribute list")
    result: dict[str, str] = {}
    for attribute in typ.cast("list[object]", declared):
        _require(
            condition=isinstance(attribute, dict),
            message="expected an attribute mapping",
        )
        encoded = typ.cast("dict[str, object]", attribute).get("value")
        _require(
            condition=isinstance(encoded, dict) and len(encoded) == 1,
            message="each OTLP attribute value must carry exactly one typed field",
        )
        result[typ.cast("str", typ.cast("dict[str, object]", attribute).get("key"))] = (
            typ.cast("str", next(iter(typ.cast("dict[str, object]", encoded).values())))
        )
    return result
