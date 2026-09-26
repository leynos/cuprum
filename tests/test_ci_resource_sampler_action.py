"""Behavioural tests for the resource-sampler composite action.

The workflow contracts prove the action is wired into every paid Linux job.
They prove nothing about what it does. This module runs the action's own shell
bodies, so a sampler that exported no process identifier, sampled nothing, or
computed its peaks wrongly would fail here rather than quietly reporting
`unknown` for the life of the estate.

The sampler is a Linux measurement. It reads `free -m` and `df -m .` by column
position, and these tests run its shell under `/bin/bash`. Where those
assumptions do not hold the shell tests skip with the reason, so a contributor
on another platform is told why the question does not apply instead of seeing
the action reported as broken. On Linux nothing skips: a missing tool is the
defect this module exists to catch, and a skip there would hide it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv
import sys
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
#: The sampler's loop sleeps before its first row, so the first sample lands at
#: roughly 15 s and the second at 30 s. Allowing 40 s therefore tolerates a
#: missed interval, which is what keeps the test honest on a loaded runner.
SAMPLE_DEADLINE_SECONDS = 40
#: `pyproject.toml` sets a suite-wide `timeout = 30`, which is shorter than
#: `SAMPLE_DEADLINE_SECONDS`: pytest-timeout would kill this test at 30 s and
#: the assertion below would never get to report. The per-test marker raises
#: the ceiling above the wait (40 s) plus the start step's own subprocess limit
#: (60 s), so the test's own diagnostic always wins the race.
SAMPLE_TEST_TIMEOUT_SECONDS = 120


def _shell_skip_reason() -> str:
    """Return why the action's shell cannot be exercised here.

    Returns
    -------
    str
        A human-readable blocker, or the empty string when this host can run
        the action's shell bodies.
    """
    if sys.platform != "linux":
        return (
            "the sampler reads `free` and `df` by Linux column positions and "
            f"runs under /bin/bash; this is {sys.platform}"
        )
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        return (
            f"the sampler shells out to {', '.join(missing)}, which this host "
            "does not provide"
        )
    return ""


_SHELL_SKIP_REASON = _shell_skip_reason()

#: For every test that runs the action's shell. The sampler is measured on
#: Linux runners and those are the only hosts where its output means anything,
#: so a foreign platform skips rather than failing a question that does not
#: apply to it.
requires_the_sampler_toolbox = pytest.mark.skipif(
    bool(_SHELL_SKIP_REASON),
    reason=_SHELL_SKIP_REASON or "the sampler's toolbox is present",
)

#: The toolbox assertion is a contract about the runner, and the runner is
#: Linux. It deliberately does *not* skip when a tool is missing: that is the
#: defect it exists to catch, and skipping there would make it a tautology.
on_linux = pytest.mark.skipif(
    sys.platform != "linux",
    reason=f"the sampler's toolbox is a Linux runner contract; this is {sys.platform}",
)


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


@on_linux
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


@requires_the_sampler_toolbox
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


@requires_the_sampler_toolbox
@pytest.mark.timeout(SAMPLE_TEST_TIMEOUT_SECONDS)
def test_the_sampler_writes_three_numbers_per_interval(
    tmp_path: Path, sampler_pid: list[int]
) -> None:
    """Record used memory, used disk, and free disk on every tick.

    The report step reads these by column position, so a row with a different
    shape would silently produce wrong peaks rather than an error.

    The wait is `SAMPLE_DEADLINE_SECONDS`, which is longer than the suite's own
    default timeout. Without the marker above, pytest-timeout would kill the
    test before the loop ended and the diagnostic below would never be
    reported; the failure would read as an unexplained timeout instead of
    naming the sampler that produced nothing.
    """
    result = run_step(step_script(ACTION, START_STEP), workdir=tmp_path)
    sampler_pid.append(int(result.exported["RESOURCE_SAMPLER_PID"]))
    log = tmp_path / "resource.log"
    deadline = time.monotonic() + SAMPLE_DEADLINE_SECONDS
    while time.monotonic() < deadline and not log.read_text(encoding="utf-8").strip():
        time.sleep(1)
    rows = [
        line.split() for line in log.read_text(encoding="utf-8").splitlines() if line
    ]
    assert rows, (
        f"the sampler produced no rows within {SAMPLE_DEADLINE_SECONDS} s"
    )
    for row in rows:
        assert len(row) == 3, f"expected memory, used disk, free disk; got {row}"
        assert all(field.isdigit() for field in row), f"non-numeric sample: {row}"


@requires_the_sampler_toolbox
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


@requires_the_sampler_toolbox
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
