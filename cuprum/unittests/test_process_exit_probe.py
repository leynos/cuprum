"""The zombie-aware exit probe answers the same way with and without procfs.

``tests.helpers.process_exit.observe_exit`` is what lets a teardown test tell a
killed descendant from a running one without waiting on the host's reaper. Its
two routes — Linux procfs and ``ps`` — must agree on the three states a test
can meet: a live process, an exited child nobody has reaped, and a pid that is
gone. The ``ps`` route is the only one a host without procfs has, so it is
exercised here by withholding procfs rather than left to the platforms that
lack it.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - spawns the zombie fixture's child with a fixed argv.
import sys
import typing as typ

import pytest

from tests.helpers import process_exit
from tests.helpers.process_exit import ExitObservation, observe_exit
from tests.helpers.process_state import ProcessState, process_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "waitid"),
    reason="needs a waitid that can leave a zombie in place",
)


def _withhold_procfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the probe see a host whose procfs cannot be read."""

    def unavailable(pid: int) -> ProcessState:
        """Report *pid* as unobservable, as a host without procfs does."""
        return ProcessState(pid, state=None, exited=False, wchan=None, available=False)

    monkeypatch.setattr(process_exit, "process_state", unavailable)


@pytest.fixture(params=["procfs", "ps"])
def route(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Select the probe's route, skipping one this host cannot supply."""
    name = typ.cast("str", request.param)
    if name == "procfs":
        if not process_state(os.getpid()).available:
            pytest.skip("this host has no readable procfs")
        return name
    if shutil.which("ps") is None:
        pytest.skip("this host has no ps to fall back to")
    _withhold_procfs(monkeypatch)
    return name


@pytest.fixture
def unreaped_child() -> cabc.Iterator[int]:
    """Yield the pid of a child that has exited and has not been reaped."""
    # Spawned rather than forked: forking the multi-threaded test process is
    # deprecated, and the child here needs nothing from its parent's memory.
    # Leaving the block waits for the child, which tolerates one a test has
    # already reaped itself.
    with subprocess.Popen(
        [sys.executable, "-c", "pass"],
    ) as child:
        # ``WNOWAIT`` blocks until the child has exited and reaps nothing, so
        # the zombie is in place before the probe runs rather than raced for.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        yield child.pid


@pytest.mark.usefixtures("route")
def test_a_live_process_is_reported_running() -> None:
    """The probe's own process is running on either route."""
    observed = observe_exit(os.getpid())
    assert observed is ExitObservation.RUNNING, (
        f"the running test process must be reported RUNNING, got {observed}"
    )


@pytest.mark.usefixtures("route")
def test_an_unreaped_zombie_is_reported_exited(unreaped_child: int) -> None:
    """A zombie is exited, although a null signal to it still succeeds."""
    os.kill(unreaped_child, 0)  # Raises if the precondition does not hold.
    observed = observe_exit(unreaped_child)
    assert observed is ExitObservation.EXITED, (
        f"an exited child nobody has reaped must be reported EXITED, got {observed}"
    )


@pytest.mark.usefixtures("route")
def test_a_reaped_process_is_reported_exited(unreaped_child: int) -> None:
    """A pid with no process behind it is exited."""
    os.waitpid(unreaped_child, 0)
    observed = observe_exit(unreaped_child)
    assert observed is ExitObservation.EXITED, (
        f"a reaped pid must be reported EXITED, got {observed}"
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FileNotFoundError("ps"), id="ps-missing"),
        pytest.param(subprocess.TimeoutExpired("ps", 5.0), id="ps-wedged"),
    ],
)
def test_a_host_that_cannot_answer_is_unobservable(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Neither route answering is its own outcome, not an exit or a survival."""
    _withhold_procfs(monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail as a host does when ``ps`` cannot be run to completion."""
        raise failure

    monkeypatch.setattr(process_exit.subprocess, "run", refuse)
    observed = observe_exit(os.getpid())
    assert observed is ExitObservation.UNOBSERVABLE, (
        "a host that can answer by neither procfs nor ps has said nothing "
        f"about the process, got {observed}"
    )


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("", id="silent"),
        pytest.param("ps: cannot open the process table\n", id="with-stderr"),
    ],
)
def test_a_failing_ps_does_not_report_a_live_process_exited(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
) -> None:
    """``ps`` failing as it does for a missing pid is not evidence of exit.

    ``ps`` exits 1 with nothing on stdout both when no process matched and
    when it could not read the process table, so that result alone must not
    turn a live process into an exited one.
    """
    _withhold_procfs(monkeypatch)

    def fail_like_no_match(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Return what ``ps`` returns when it prints no row."""
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(process_exit.subprocess, "run", fail_like_no_match)
    observed = observe_exit(os.getpid())
    assert observed is ExitObservation.UNOBSERVABLE, (
        "a live process that ps printed no row for must be UNOBSERVABLE, "
        f"never EXITED, got {observed}"
    )
