"""Behavioural tests for the resource-sampler composite action.

The workflow contracts prove the action is wired into every paid Linux job.
They prove nothing about what it does. This module runs the action's own shell
bodies, so a sampler that exported no process identifier, sampled nothing, or
computed its peaks wrongly would fail here rather than quietly reporting
`unknown` for the life of the estate.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import time
import typing as typ

import pytest

from tests.helpers.composite_actions import action_document, run_step, step_script

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

ACTION = ".github/actions/resource-sampler"
START_STEP = "Start resource sampler"
REPORT_STEP = "Report peak resource use"
#: `free` and `df` must exist for the sampler to sample anything at all.
REQUIRED_TOOLS = ("free", "df", "du")


def _process_is_alive(pid: int) -> bool:
    """Report whether a process identifier still names a live process."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture(name="sampler_pid")
def _sampler_pid() -> cabc.Iterator[list[int]]:
    """Collect sampler process identifiers and reap them after the test.

    Yields
    ------
    list[int]
        A list the test appends each started sampler's identifier to.

    Notes
    -----
    Signalled individually rather than by process group: the sampler is a child
    of the shell this test ran and shares the test runner's group, so a group
    signal would take pytest with it.
    """
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)


def test_the_action_declares_the_inputs_the_workflows_pass() -> None:
    """Keep the callers and the action agreed on its interface."""
    document = action_document(ACTION)
    inputs = typ.cast("dict[str, object]", document.get("inputs", {}))
    assert set(inputs) == {"mode", "vcpus"}, (
        f"the sampler must declare exactly `mode` and `vcpus`; got {sorted(inputs)}"
    )
    mode = typ.cast("dict[str, object]", inputs["mode"])
    assert mode.get("required") is True, (
        "`mode` selects the behaviour, so it is required"
    )
    vcpus = typ.cast("dict[str, object]", inputs["vcpus"])
    assert vcpus.get("required") is False, "`vcpus` is a label, not behaviour"
    assert vcpus.get("default"), (
        "`vcpus` must default to something printable, so a caller that forgets "
        "it still produces a readable line rather than an empty one"
    )
    runs = typ.cast("dict[str, object]", document["runs"])
    assert runs.get("using") == "composite", (
        "the sampler brackets steps inside a job, so it cannot be a "
        "container or JavaScript action"
    )


@pytest.mark.parametrize("tool", REQUIRED_TOOLS)
def test_the_sampling_tools_exist(tool: str) -> None:
    """Fail loudly here rather than silently sampling nothing on the runner."""
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no input
    assert (
        subprocess.run(
            ["/usr/bin/env", "which", tool],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ), f"the sampler shells out to {tool!r}"


def test_start_exports_a_live_sampler_process(
    tmp_path: Path, sampler_pid: list[int]
) -> None:
    """Start a background sampler and hand its identifier to the later step."""
    result = run_step(step_script(ACTION, START_STEP), workdir=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "RESOURCE_SAMPLER_PID" in result.exported, (
        "the report step kills the process this name carries, so a start that "
        f"exports nothing leaves a sampler running for the job; got {result.exported}"
    )
    pid = int(result.exported["RESOURCE_SAMPLER_PID"])
    sampler_pid.append(pid)
    assert _process_is_alive(pid), "the exported identifier must name a live process"


def test_the_sampler_writes_three_numbers_per_interval(
    tmp_path: Path, sampler_pid: list[int]
) -> None:
    """Record used memory, used disk, and free disk on every tick.

    The report step reads these by column position, so a row with a different
    shape would silently produce wrong peaks rather than an error.
    """
    result = run_step(step_script(ACTION, START_STEP), workdir=tmp_path)
    sampler_pid.append(int(result.exported["RESOURCE_SAMPLER_PID"]))
    log = tmp_path / "resource.log"
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not log.read_text(encoding="utf-8").strip():
        time.sleep(1)
    rows = [
        line.split() for line in log.read_text(encoding="utf-8").splitlines() if line
    ]
    assert rows, "the sampler produced no rows within 40 s"
    for row in rows:
        assert len(row) == 3, f"expected memory, used disk, free disk; got {row}"
        assert all(field.isdigit() for field in row), f"non-numeric sample: {row}"


def test_report_publishes_the_peaks_and_stops_the_sampler(tmp_path: Path) -> None:
    """Take the maximum of both used columns and the minimum of free disk."""
    (tmp_path / "resource.log").write_text(
        "100 5000 900\n700 5200 400\n300 4800 1200\n", encoding="utf-8"
    )
    with subprocess.Popen(["/bin/sleep", "120"]) as victim:
        try:
            result = run_step(
                step_script(ACTION, REPORT_STEP),
                workdir=tmp_path,
                environment={
                    "RESOURCE_SAMPLER_PID": str(victim.pid),
                    "JOB_VCPUS": "2",
                },
            )
            assert result.returncode == 0, result.stderr
            for expected in (
                "memory: 700 MiB on 2 vCPUs",
                "disk used: 5200 MiB",
                "least free: 400 MiB",
            ):
                assert expected in result.stdout, (
                    f"the log must carry {expected!r}: the jobs API exposes the "
                    f"log and not the summary. Got:\n{result.stdout}"
                )
                assert expected in result.summary, (
                    f"the summary must carry {expected!r}"
                )
            # The step must have killed it; waiting proves that rather than
            # assuming it.
            victim.wait(timeout=10)
        finally:
            if victim.poll() is None:  # pragma: no cover - assertion failure only
                victim.kill()


def test_report_survives_a_job_that_never_started_the_sampler(tmp_path: Path) -> None:
    """Report `unknown` rather than failing a job that already failed.

    The step runs under `if: always()`, so it must tolerate a job that died
    before the sampler wrote anything. Masking the real failure with a shell
    error would be worse than reporting nothing.
    """
    result = run_step(
        step_script(ACTION, REPORT_STEP),
        workdir=tmp_path,
        environment={"RESOURCE_SAMPLER_PID": "", "JOB_VCPUS": "2"},
    )
    assert result.returncode == 0, result.stderr
    assert "memory: unknown MiB" in result.stdout, result.stdout
    assert "disk used: unknown MiB, least free: unknown MiB" in result.stdout, (
        f"a job with no samples must still report readable placeholders; "
        f"got:\n{result.stdout}"
    )
