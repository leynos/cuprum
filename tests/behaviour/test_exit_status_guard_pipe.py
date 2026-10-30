"""Behavioural coverage for the opt-in exit-status guard (#671).

A real pipe is the only witness for this contract. The probe relays a child
whose output the scenario stops reading partway through, so CPython's final
flush of the probe's standard streams meets a closed reader. Without the
guard, that flush fails and the interpreter reports 120 in place of the
child's own status; with the guard armed at the entry point, the status is
preserved. The control scenario is what makes the others meaningful: it
reproduces the defect the helper exists to remove, and it is CPython-specific
because the shutdown ordering being observed is CPython's.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - a real pipe is the boundary under test.
import sys
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from tests.behaviour._exit_status_guard_support import (
    PROBE_WAIT_SECONDS,
    READY_LINE,
    write_probe,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the guard relies on POSIX descriptor duplication",
)

# A probe killed rather than reaped by the scenario only needs long enough for
# the signal and the kernel's own teardown.
_REAP_SECONDS = 5.0
_CPYTHON_STATUS = 120


@dc.dataclass(slots=True)
class _ProbeScenario:
    """State the Given steps accumulate before the probe is launched.

    Attributes
    ----------
    child_status:
        The status the relayed child exits with.
    install_guard:
        Whether the probe arms the exit-status guard.
    merge_stderr:
        Whether the probe's stderr shares the stdout pipe.
    returncode:
        The status the probe exited with, once the reader has closed.
    stderr:
        What the probe wrote to a separate stderr pipe, for diagnostics.
    """

    child_status: int
    install_guard: bool = False
    merge_stderr: bool = False
    returncode: int | None = None
    stderr: str = ""


@given(
    parsers.parse("a probe that relays a child which exits with status {status:d}"),
    target_fixture="probe_scenario",
)
def _relaying_probe(status: int) -> _ProbeScenario:
    """Start a scenario around a child with the requested status."""
    return _ProbeScenario(child_status=status)


@given("the probe installs the exit-status guard")
def _install_guard(probe_scenario: _ProbeScenario) -> None:
    """Arm the guard before the probe is launched."""
    probe_scenario.install_guard = True


@given("the probe does not install the exit-status guard")
def _skip_guard(probe_scenario: _ProbeScenario) -> None:
    """Leave the probe without a guard, as the control requires."""
    probe_scenario.install_guard = False


@given("the probe's standard error is merged into its standard output")
def _merge_stderr(probe_scenario: _ProbeScenario) -> None:
    """Give the probe one pipe for both standard streams."""
    probe_scenario.merge_stderr = True


@when("the reader closes after the child is ready")
def _close_reader(
    probe_scenario: _ProbeScenario,
    tmp_path: Path,
) -> None:
    """Run the probe, close its pipe once the child is ready, and reap it."""
    script = write_probe(
        tmp_path,
        install_guard=probe_scenario.install_guard,
        child_status=probe_scenario.child_status,
    )
    marker = tmp_path / "marker"
    process = _start_probe(script, marker, merge_stderr=probe_scenario.merge_stderr)
    try:
        stdout = process.stdout
        assert stdout is not None, "the probe's stdout must be a pipe"
        ready = stdout.readline()
        assert ready.strip() == READY_LINE, (
            "the probe must relay the child's readiness line, got "
            f"{ready!r} (probe stderr: {_read_stderr(process)!r})"
        )
        stdout.close()
        marker.write_text("go", encoding="utf-8")
        probe_scenario.returncode = process.wait(timeout=PROBE_WAIT_SECONDS)
        probe_scenario.stderr = _read_stderr(process)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        _terminate(process)
        if process.stderr is not None:
            process.stderr.close()


@then(parsers.parse("the probe exits with status {status:d}"))
def _assert_status(probe_scenario: _ProbeScenario, status: int) -> None:
    """Assert the probe reported the status the scenario asked for."""
    if status == _CPYTHON_STATUS and sys.implementation.name != "cpython":
        pytest.skip("the control observes CPython's finalization semantics")
    assert probe_scenario.returncode == status, (
        "the probe must report "
        f"{status}, not {probe_scenario.returncode}; child status "
        f"{probe_scenario.child_status}, guard "
        f"{probe_scenario.install_guard}, merged stderr "
        f"{probe_scenario.merge_stderr}, probe stderr "
        f"{probe_scenario.stderr!r}"
    )


def _start_probe(
    script: Path,
    marker: Path,
    *,
    merge_stderr: bool,
) -> subprocess.Popen[str]:
    """Launch the generated probe with the marker path as its argument."""
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter argv.
        [sys.executable, str(script), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
    )


def _read_stderr(process: subprocess.Popen[str]) -> str:
    """Return what the probe wrote to a separate stderr pipe, if any."""
    if process.stderr is None or process.returncode is None:
        return ""
    return process.stderr.read()


def _terminate(process: subprocess.Popen[str]) -> None:
    """Kill and reap a probe the scenario did not launch to completion."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_REAP_SECONDS)


@scenario(
    "../features/exit_status_guard.feature",
    "The guard preserves a successful child's status",
)
def test_guard_preserves_success_status() -> None:
    """A successful run keeps status 0 through a closed pipe."""


@scenario(
    "../features/exit_status_guard.feature",
    "The guard preserves a failing child's status",
)
def test_guard_preserves_failure_status() -> None:
    """A failing run keeps the child's own status through a closed pipe."""


@scenario(
    "../features/exit_status_guard.feature",
    "The guard preserves the status when stderr is merged into stdout",
)
def test_guard_preserves_status_with_merged_streams() -> None:
    """Merging stderr onto stdout does not create a second 120 path."""


@scenario(
    "../features/exit_status_guard.feature",
    "Without the guard CPython reports 120",
)
def test_without_the_guard_the_status_becomes_120() -> None:
    """The control reproduces the defect the guard removes."""
