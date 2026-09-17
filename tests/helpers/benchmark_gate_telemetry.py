"""Execute the real workflow log writer without an external service."""

from __future__ import annotations

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - execute the checked-in workflow script.
import typing as typ

from tests.helpers.workflow import CHANGES_JOB, mapping, script_of, step_named

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow import Workflow

LOG_STEP = "Persist the benchmark gate decision"
UPLOAD_STEP = "Upload the benchmark gate log"
LABEL_NAMES = ("event_class", "detector_status", "decision")
VALUE_INPUTS = ("EVENT_CLASS", "DETECTOR_STATUS", "DECISION")


@dc.dataclass(frozen=True, slots=True)
class Verdict:
    """The three bounded values computed by the canonical decision step.

    Attributes
    ----------
    event_class : str
        Pull-request or other event class.
    detector_status : str
        Successful, failed, or unknown detector outcome.
    decision : str
        Run, skip, or detector-failure admission verdict.
    """

    event_class: str
    detector_status: str
    decision: str

    def as_inputs(self) -> dict[str, str]:
        """Return the verdict as the log step's environment inputs."""
        return dict(
            zip(
                VALUE_INPUTS,
                (self.event_class, self.detector_status, self.decision),
                strict=True,
            )
        )

    def as_labels(self) -> dict[str, str]:
        """Return the three fields under their stored label names."""
        return dict(
            zip(
                LABEL_NAMES,
                (self.event_class, self.detector_status, self.decision),
                strict=True,
            )
        )


@dc.dataclass(frozen=True, slots=True)
class LogRun:
    """Capture one execution of the workflow log writer.

    Attributes
    ----------
    exit_code : int
        Shell process exit status.
    stdout : str
        Standard output, including bounded warnings.
    stderr : str
        Standard error for failure diagnosis.
    body : str
        Persisted JSON Lines contents, empty if no record was written.
    outputs : str
        Actual GitHub step-output file contents.
    """

    exit_code: int
    stdout: str
    stderr: str
    body: str
    outputs: str


def log_script(workflow_data: Workflow) -> str:
    """Return the log writer's declared shell script.

    Parameters
    ----------
    workflow_data : tests.helpers.workflow.Workflow
        Parsed CI workflow under test.

    Returns
    -------
    str
        The repository-owned shell script.

    Raises
    ------
    AssertionError
        If the workflow no longer declares the log writer.
    """
    script = script_of(step_named(workflow_data, CHANGES_JOB, LOG_STEP))
    if script is None:
        message = "the decision log step must declare a script"
        raise AssertionError(message)
    return script


def log_env(workflow_data: Workflow) -> dict[str, object]:
    """Return the log writer's validated environment mapping."""
    return mapping(
        step_named(workflow_data, CHANGES_JOB, LOG_STEP).get("env"),
        "the decision log step must declare its gate output inputs",
    )


def run_log_script(
    *, verdict: Verdict, workflow_data: Workflow, tmp_path: pth.Path
) -> LogRun:
    """Run the checked-in log writer and capture its persisted record.

    Parameters
    ----------
    verdict : Verdict
        Gate outputs delivered to the workflow step.
    workflow_data : tests.helpers.workflow.Workflow
        Parsed CI workflow under test.
    tmp_path : pathlib.Path
        Isolated runner temporary directory. A file named `benchmark-gate`
        here can deliberately prevent directory creation for failure tests.

    Returns
    -------
    LogRun
        The process result and the actual file and step-output contents.
    """
    output = tmp_path / "outputs.txt"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - literal argv executes the repository-owned workflow script.
        ["/usr/bin/env", "bash", "-c", log_script(workflow_data)],
        env={
            "PATH": "/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_RUN_ATTEMPT": "2",
            **verdict.as_inputs(),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    log = tmp_path / "benchmark-gate" / "decisions.jsonl"
    return LogRun(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        body=log.read_text(encoding="utf-8") if log.is_file() else "",
        outputs=output.read_text(encoding="utf-8") if output.is_file() else "",
    )
