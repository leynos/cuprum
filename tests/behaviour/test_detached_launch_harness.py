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
"""

from __future__ import annotations

import dataclasses as dc
import pathlib
import typing as typ

from tests.helpers.detached import (
    WORKER_SOURCE,
    LaunchSpec,
    Receipt,
    WorkerReport,
    is_running,
    read_receipt,
    read_worker_report,
    run_launcher,
    stop_group,
    wait_for_file,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

#: Arguments carrying the shapes a naive argument hand-off would split, expand,
#: or strip. The worker reports its own ``sys.argv``, so the comparison is made
#: against what the child actually received.
_ARGUMENTS = ("café; rm -rf /tmp/x", "$HOME", "'quoted'", "a b", "*")

#: A variable the environment cases set, and one they remove.
_PRESENT = "CUPRUM_DETACHED_MARKER"
_ABSENT = "CUPRUM_DETACHED_ABSENT"


@dc.dataclass(frozen=True, slots=True)
class _LaunchOptions:
    """The execution options a launch case varies from the default."""

    env: cabc.Mapping[str, str | None] = dc.field(default_factory=dict)
    env_mode: str = "overlay"
    cwd: pathlib.Path | None = None
    allow: bool = True
    bind: str | None = None


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
    )
    completed = run_launcher(spec)
    assert completed.returncode == 0, (
        f"the launcher failed: {completed.stderr}\n{completed.stdout}"
    )
    wait_for_file(report_path)
    return read_receipt(receipt_path), read_worker_report(report_path)


def test_the_worker_outlives_the_launcher_that_started_it(
    tmp_path: pathlib.Path,
) -> None:
    """The worker keeps running after the process that launched it has exited."""
    receipt, report = _launch(tmp_path)
    try:
        assert receipt.pid == report.pid, (
            "the receipt must name the process that actually ran"
        )
        assert is_running(receipt.pid), "the worker must survive its launcher"
    finally:
        stop_group(receipt.pid)


def test_the_worker_leads_its_own_session_and_process_group(
    tmp_path: pathlib.Path,
) -> None:
    """Detachment places the worker outside the launcher's session and group."""
    receipt, report = _launch(tmp_path)
    try:
        assert report.sid == receipt.pid, (
            f"the worker must lead its own session, got sid={report.sid}"
        )
        assert report.pgid == receipt.pid, (
            f"the worker must lead its own process group, got pgid={report.pgid}"
        )
    finally:
        stop_group(receipt.pid)


def test_the_worker_holds_no_launcher_owned_stream(tmp_path: pathlib.Path) -> None:
    """All three streams are the kernel's null device, and stdin is at EOF."""
    receipt, report = _launch(tmp_path)
    try:
        assert report.stdin_eof, (
            "the worker must read end-of-file on stdin, not a launcher pipe"
        )
        for fd, target in report.fd_targets.items():
            if target is None:
                continue
            assert target == "/dev/null", (
                f"fd {fd} must be /dev/null, but the worker saw {target}"
            )
    finally:
        stop_group(receipt.pid)


def test_the_worker_receives_the_requested_arguments_and_directory(
    tmp_path: pathlib.Path,
) -> None:
    """Argument boundaries and the working directory survive a real spawn."""
    work_directory = tmp_path / "work"
    work_directory.mkdir()
    receipt, report = _launch(tmp_path, _LaunchOptions(cwd=work_directory))
    try:
        # ``python -c`` puts the source in the command line but not in
        # ``sys.argv``, so the child's own view starts at the first argument.
        expected_arguments = (str(_report_path(tmp_path)), *_ARGUMENTS)
        assert report.argv[1:] == expected_arguments, (
            f"argument boundaries did not survive the spawn: {report.argv!r}"
        )
        assert pathlib.Path(report.cwd).resolve() == work_directory.resolve(), (
            f"the worker ran in {report.cwd!r}, not {work_directory}"
        )
    finally:
        stop_group(receipt.pid)


def test_the_worker_receives_a_replaced_environment(tmp_path: pathlib.Path) -> None:
    """A ``REPLACE`` overlay reaches the worker without the launcher's own."""
    receipt, report = _launch(
        tmp_path,
        _LaunchOptions(env={_PRESENT: "1"}, env_mode="replace"),
    )
    try:
        assert report.env.get(_PRESENT) == "1", (
            f"the overlay did not reach the worker, got {report.env!r}"
        )
        assert "PATH" not in report.env, (
            f"a replaced environment must discard the launcher's, got {report.env!r}"
        )
    finally:
        stop_group(receipt.pid)


def test_an_unset_variable_is_withheld_from_the_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``UNSET`` overlay value removes an inherited variable."""
    monkeypatch.setenv(_ABSENT, "inherited")
    receipt, report = _launch(tmp_path, _LaunchOptions(env={_ABSENT: None}))
    try:
        assert _ABSENT not in report.env, (
            f"an unset variable must not reach the worker, got {report.env!r}"
        )
        assert "PATH" in report.env, (
            "an overlay must keep the rest of the launcher's environment"
        )
    finally:
        stop_group(receipt.pid)


def test_a_worker_can_be_stopped_from_another_process(tmp_path: pathlib.Path) -> None:
    """Signalling the worker's group from outside ends it."""
    receipt, _ = _launch(tmp_path)

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
