"""The detached launch, observed across a real process boundary.

Every case here starts a launcher interpreter that runs cuprum, launches a
worker, and exits. What the worker sees is then read back from a file, because
a detached launch gives it no stream to report on. That arrangement is what
makes the central claim testable at all: a worker that survives its launcher
cannot be observed from inside the launcher.

The cases are split by the fact they witness. Survival and identity are one
fact; the disconnected streams are another, because a worker holding a launcher
pipe would keep the launcher's ``subprocess.run`` from returning as well as
giving the worker somewhere to write. The argument vector, environment, and
working directory are a third, and are only proved here: a recorder can show
what cuprum passes to the spawn, but only a real child can show what arrived.

Cleanup is owned by :func:`_launched` rather than by each case's ``finally``, so
a case that fails while the launcher is still producing its receipt or report
still stops the worker. The records are read in the order they are published:
the worker writes its own report as soon as it runs, before the launcher writes
the receipt.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import os
import pathlib
import sys
import time
import typing as typ

import pytest

from tests.helpers.detached import (
    Receipt,
    WorkerReport,
    is_running,
    read_heartbeat,
    read_receipt,
    read_worker_report,
    stop_group,
    wait_for_file,
    wait_for_heartbeats,
)
from tests.helpers.detached_process import (
    WORKER_SOURCE,
    LaunchSpec,
    heartbeat_path,
    run_launcher,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Every case launches a real worker, so all of them need POSIX sessions.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a detached launch requires POSIX session isolation",
)

#: Arguments carrying the shapes a naive argument hand-off would split, expand,
#: or strip. The worker reports its own ``sys.argv``, so the comparison is made
#: against what the child actually received.
_ARGUMENTS = ("café; rm -rf /tmp/x", "$HOME", "'quoted'", "a b", "*")

#: A variable the environment cases set, and one they remove.
_PRESENT = "CUPRUM_DETACHED_MARKER"
_ABSENT = "CUPRUM_DETACHED_ABSENT"

#: How long a failing case waits for a worker record to appear before deciding
#: there is nothing to stop. The worker records itself as its first action, so
#: this only matters for a launcher that failed in the instant after the fork.
_CLEANUP_SECONDS = 5.0

#: How often the cleanup poll re-checks for a record.
_CLEANUP_POLL_SECONDS = 0.01


@dc.dataclass(frozen=True, slots=True)
class _LaunchOptions:
    """The execution options a launch case varies from the default."""

    env: cabc.Mapping[str, str | None] = dc.field(default_factory=dict)
    env_mode: str = "overlay"
    cwd: pathlib.Path | None = None
    allow: bool = True
    bind: str | None = None
    aio: bool = False


def _launch(
    tmp_path: pathlib.Path,
    options: _LaunchOptions | None = None,
) -> tuple[Receipt, WorkerReport]:
    """Launch a worker through the outer launcher and return what it reported.

    Returns
    -------
    tuple[Receipt, WorkerReport]
        The launcher's receipt and the worker's own account of itself.
    """
    varied = _LaunchOptions() if options is None else options
    report_path = _report_path(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    spec = LaunchSpec(
        argv=("-c", WORKER_SOURCE, str(report_path), *_ARGUMENTS),
        receipt_path=receipt_path,
        env=varied.env,
        env_mode=varied.env_mode,
        cwd=varied.cwd,
        allow=varied.allow,
        bind=varied.bind,
        aio=varied.aio,
    )
    completed = run_launcher(spec)
    assert completed.returncode == 0, (
        f"the launcher failed: {completed.stderr}\n{completed.stdout}"
    )
    wait_for_file(report_path)
    return read_receipt(receipt_path), read_worker_report(report_path)


@contextlib.contextmanager
def _launched(
    tmp_path: pathlib.Path,
    options: _LaunchOptions | None = None,
) -> cabc.Iterator[tuple[Receipt, WorkerReport]]:
    """Yield a launched worker and stop it however the case ends."""
    try:
        yield _launch(tmp_path, options)
    finally:
        _stop_recorded_worker(tmp_path)


def _stop_recorded_worker(tmp_path: pathlib.Path) -> None:
    """Stop the worker this case launched, if it left a record naming itself."""
    pid = _recorded_pid(tmp_path)
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while pid is None and time.monotonic() < deadline:
        # A launcher that died between the spawn and its receipt has still left
        # a worker that records itself, so wait briefly rather than leaking it.
        time.sleep(_CLEANUP_POLL_SECONDS)
        pid = _recorded_pid(tmp_path)
    if pid is not None:
        stop_group(pid)


def _recorded_pid(tmp_path: pathlib.Path) -> int | None:
    """Return the worker's PID from whichever record exists, report first."""
    report_path = _report_path(tmp_path)
    if report_path.exists():
        return read_worker_report(report_path).pid
    receipt_path = tmp_path / "receipt.json"
    if not receipt_path.exists():
        return None
    try:
        pid = read_receipt(receipt_path).pid
    except (OSError, ValueError, KeyError):
        return None
    return pid


def test_the_worker_outlives_the_launcher_that_started_it(
    tmp_path: pathlib.Path,
) -> None:
    """The worker keeps running after the process that launched it has exited."""
    with _launched(tmp_path) as (receipt, report):
        assert receipt.entry_point == "sync", "this case must use the sync spelling"
        assert receipt.pid == report.pid, (
            "the receipt must name the process that actually ran"
        )
        assert is_running(receipt.pid), "the worker must survive its launcher"


def test_an_async_launch_outlives_the_loop_that_started_it(
    tmp_path: pathlib.Path,
) -> None:
    """A worker launched on an ``asyncio`` loop keeps working after it closes."""
    with _launched(tmp_path, _LaunchOptions(aio=True)) as (receipt, report):
        assert receipt.entry_point == "async", (
            "this case must exercise the asyncio entry point"
        )
        assert receipt.pid == report.pid, (
            "the receipt must name the process that actually ran"
        )
        beat_path = heartbeat_path(_report_path(tmp_path))
        # The launcher has already exited, so every beat counted from here is
        # evidence of continued progress without the loop that started it.
        published = read_heartbeat(beat_path)
        wait_for_heartbeats(beat_path, after=published, count=2)
        assert is_running(receipt.pid), "the worker must survive its launcher"


def test_the_worker_leads_its_own_session_and_process_group(
    tmp_path: pathlib.Path,
) -> None:
    """Detachment places the worker outside the launcher's session and group."""
    with _launched(tmp_path) as (receipt, report):
        assert report.sid == receipt.pid, (
            f"the worker must lead its own session, got sid={report.sid}"
        )
        assert report.pgid == receipt.pid, (
            f"the worker must lead its own process group, got pgid={report.pgid}"
        )


def test_the_worker_holds_no_launcher_owned_stream(tmp_path: pathlib.Path) -> None:
    """All three streams are the kernel's null device, and stdin is at EOF."""
    with _launched(tmp_path) as (_, report):
        assert report.stdin_eof, (
            "the worker must read end-of-file on stdin, not a launcher pipe"
        )
        resolved = {
            fd: target for fd, target in report.fd_targets.items() if target is not None
        }
        if sys.platform.startswith("linux"):
            assert len(resolved) == len(report.fd_targets), (
                "every descriptor must resolve through procfs, got "
                f"{report.fd_targets!r}"
            )
        for fd, target in resolved.items():
            assert target == "/dev/null", (
                f"fd {fd} must be /dev/null, but the worker saw {target}"
            )


def test_the_worker_receives_the_requested_arguments_and_directory(
    tmp_path: pathlib.Path,
) -> None:
    """Argument boundaries and the working directory survive a real spawn."""
    work_directory = tmp_path / "work"
    work_directory.mkdir()
    with _launched(tmp_path, _LaunchOptions(cwd=work_directory)) as (_, report):
        # ``python -c`` puts the source in the command line but not in
        # ``sys.argv``, so the child's own view starts at the first argument.
        expected_arguments = (str(_report_path(tmp_path)), *_ARGUMENTS)
        assert report.argv[1:] == expected_arguments, (
            f"argument boundaries did not survive the spawn: {report.argv!r}"
        )
        assert pathlib.Path(report.cwd).resolve() == work_directory.resolve(), (
            f"the worker ran in {report.cwd!r}, not {work_directory}"
        )


def test_the_worker_receives_a_replaced_environment(tmp_path: pathlib.Path) -> None:
    """A ``REPLACE`` overlay reaches the worker without the launcher's own."""
    options = _LaunchOptions(env={_PRESENT: "1"}, env_mode="replace")
    with _launched(tmp_path, options) as (_, report):
        assert report.env.get(_PRESENT) == "1", (
            f"the overlay did not reach the worker, got {report.env!r}"
        )
        assert "PATH" not in report.env, (
            f"a replaced environment must discard the launcher's, got {report.env!r}"
        )


def test_an_unset_variable_is_withheld_from_the_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``UNSET`` overlay value removes an inherited variable."""
    monkeypatch.setenv(_ABSENT, "inherited")
    with _launched(tmp_path, _LaunchOptions(env={_ABSENT: None})) as (_, report):
        assert _ABSENT not in report.env, (
            f"an unset variable must not reach the worker, got {report.env!r}"
        )
        assert "PATH" in report.env, (
            "an overlay must keep the rest of the launcher's environment"
        )


def test_a_worker_can_be_stopped_from_another_process(tmp_path: pathlib.Path) -> None:
    """Signalling the worker's group from outside ends it."""
    with _launched(tmp_path) as (receipt, _):
        stop_group(receipt.pid)

        assert not is_running(receipt.pid), "a signalled worker must stop"


def test_a_forbidden_program_stops_the_launcher_before_a_worker_exists(
    tmp_path: pathlib.Path,
) -> None:
    """The allowlist refuses the launch in the launcher, and nothing survives it."""
    spec = _failing_spec(tmp_path, allow=False)

    completed = run_launcher(spec)

    assert completed.returncode != 0, "a refused launch must fail the launcher"
    assert "ForbiddenProgramError" in completed.stderr, (
        f"the refusal must be reported as a refusal, got: {completed.stderr}"
    )
    assert not spec.receipt_path.exists(), "a refused launch must produce no receipt"
    assert not _report_path(tmp_path).exists(), "a refused launch must create no worker"


def test_a_missing_executable_stops_the_launcher(tmp_path: pathlib.Path) -> None:
    """A worker that cannot be started surfaces as an error, not a receipt."""
    spec = _failing_spec(tmp_path, bind=str(tmp_path / "absent-tool"))

    completed = run_launcher(spec)

    assert completed.returncode != 0, "an unstartable worker must fail the launcher"
    assert "FileNotFoundError" in completed.stderr, (
        f"the start-up failure must name the error, got: {completed.stderr}"
    )
    assert not spec.receipt_path.exists(), "a failed launch must produce no receipt"
    assert not _report_path(tmp_path).exists(), "a failed launch must create no worker"


def _failing_spec(
    tmp_path: pathlib.Path,
    *,
    allow: bool = True,
    bind: str | None = None,
) -> LaunchSpec:
    """Build a spec whose launch is expected to be refused or to fail."""
    return LaunchSpec(
        argv=("-c", WORKER_SOURCE, str(_report_path(tmp_path))),
        receipt_path=tmp_path / "receipt.json",
        allow=allow,
        bind=bind,
    )


def _report_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return the path the worker reports to in *tmp_path*."""
    return tmp_path / "worker.json"
