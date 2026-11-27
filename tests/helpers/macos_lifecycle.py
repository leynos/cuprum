"""Run the POSIX process-lifecycle subset on the macOS advisory leg.

Linux CI runs every Python test on hosts that have procfs, so the behaviour
that exists only for POSIX hosts *without* procfs is never executed on the
platform it was written for. That gap hid the ``ps`` route of the zombie-aware
exit probe (``tests/helpers/process_exit.py``) from the platform which depends
on it, and it left the session and process-group promises unexecuted on macOS.
This module is the manifest the advisory leg runs: the modules it selects, the
cases it deliberately removes with a reason, and the node IDs whose passing is
the point of the exercise.

The run is guarded by ``tests.helpers.required_outcomes``, which fails on a
skip, an xfail, an empty selection, or any other outcome that would let the leg
report success without proving the behaviour. Run it on a Mac with::

    uv sync --locked --group dev
    uv run python -m tests.helpers.macos_lifecycle

The leg deliberately does not build the compiled extension. Every module here
exercises the Python backend, so ``CUPRUM_STREAM_BACKEND=python`` is fixed
before pytest imports the repository's conftest, and any ambient
``CUPRUM_REQUIRE_RUST_EXTENSION`` is removed rather than allowed to turn a
deliberate Python-only run into a usage error.
"""

from __future__ import annotations

import dataclasses as dc
import os
import platform
import shutil
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "DESELECTED",
    "REQUIRED_PASSING",
    "TARGETS",
    "Deselection",
    "main",
)


@dc.dataclass(frozen=True, slots=True)
class Deselection:
    """One case the leg deliberately does not run, and the reason it does not."""

    node_id: str
    reason: str


#: The modules the advisory leg runs. The first two carry the acceptance
#: criteria: the probe's ``ps`` route and the issue #720 descendant regression.
#: The rest pin the POSIX promises around them — process-group ownership,
#: cancellation, deadline teardown, and detached launch.
TARGETS: typ.Final[tuple[str, ...]] = (
    "cuprum/unittests/test_process_exit_probe.py",
    "cuprum/unittests/test_cancelled_run_descendant_tree.py",
    "cuprum/unittests/test_process_group_policy.py",
    "cuprum/unittests/test_process_group_ownership.py",
    "cuprum/unittests/test_process_group_liveness.py",
    "cuprum/unittests/test_direct_start_ownership.py",
    "cuprum/unittests/test_owned_group_start_failure.py",
    "cuprum/unittests/test_group_settlement_duration.py",
    "cuprum/unittests/test_pipeline_process_group_ownership.py",
    "cuprum/unittests/test_pipeline_process_group_run.py",
    "cuprum/unittests/test_process_signal_identity.py",
    "cuprum/unittests/test_process_signal_failure_reporting.py",
    "cuprum/unittests/test_stream_run_owned_group_cancellation.py",
    "cuprum/unittests/test_stream_run_owned_group_deadline.py",
    "cuprum/unittests/test_detached_launch.py",
    "cuprum/unittests/test_detached_launch_lifecycle.py",
    "cuprum/unittests/test_pipeline_post_exit_deadline.py",
    "cuprum/unittests/test_run_no_stream_post_exit_deadline.py",
    "cuprum/unittests/test_safe_cmd_lines_post_exit_deadline.py",
    "cuprum/unittests/test_safe_cmd_lines_paused_timeout.py",
)

_PROBE_MODULE = "cuprum/unittests/test_process_exit_probe.py"
_CANCELLED_MODULE = "cuprum/unittests/test_cancelled_run_descendant_tree.py"
_DIRECT_START_MODULE = "cuprum/unittests/test_direct_start_ownership.py"
_PIPELINE_DEADLINE_MODULE = "cuprum/unittests/test_pipeline_post_exit_deadline.py"
_DETACHED_LIFECYCLE_MODULE = "cuprum/unittests/test_detached_launch_lifecycle.py"

_NO_PROCFS = (
    "Darwin has no Linux procfs, so this route parameter skips by "
    "construction. The ps parameter of the same case runs, is required to "
    "pass, and is the route that exists on macOS; portable process-readiness "
    "helpers are tracked in issue #738."
)
_EXTENSION_ABSENT = (
    "the Rust-backend arm needs the compiled extension, which this leg "
    "deliberately does not build: it exercises the Python backend on macOS. "
    "The Python-backend arm of the same case runs and is guarded."
)
_DESCRIPTOR_COUNT = (
    "counts /proc/self/fd, which Darwin does not provide; the portable "
    "readiness helper that would replace it is tracked in issue #738."
)

#: ``(module, case, parameter)`` for every Rust-backend arm the leg removes.
#: The extension is absent by design, so each of these would skip.
_EXTENSION_CASES: typ.Final[tuple[tuple[str, str, str], ...]] = (
    (
        _DIRECT_START_MODULE,
        "test_start_hook_failure_reaps_the_child_before_it_propagates",
        "rust-backend-stdin-direct",
    ),
    (
        _DIRECT_START_MODULE,
        "test_start_hook_failure_reaps_the_child_before_it_propagates",
        "rust-backend-stdin-streamed",
    ),
    (
        _DIRECT_START_MODULE,
        "test_start_hook_failure_reaps_the_child_before_it_propagates",
        "rust-backend-no-stdin-direct",
    ),
    (
        _DIRECT_START_MODULE,
        "test_start_hook_failure_reaps_the_child_before_it_propagates",
        "rust-backend-no-stdin-streamed",
    ),
    (
        _DIRECT_START_MODULE,
        "test_streamed_setup_failure_reclaims_the_started_consumer",
        "rust-backend",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_success_route_drain_is_bounded_by_the_configured_deadline",
        "run-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_success_route_drain_is_bounded_by_the_configured_deadline",
        "run_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_success_route_drain_is_bounded_by_the_configured_deadline",
        "run_bytes-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_success_route_drain_is_bounded_by_the_configured_deadline",
        "run_bytes_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group",
        "run-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group",
        "run_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group",
        "run_bytes-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group",
        "run_bytes_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_under_an_inherited_group",
        "run-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_under_an_inherited_group",
        "run_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_under_an_inherited_group",
        "run_bytes-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_wait_timeout_drain_is_bounded_under_an_inherited_group",
        "run_bytes_sync-rust",
    ),
    (
        _PIPELINE_DEADLINE_MODULE,
        "test_uncaptured_success_route_does_not_wait_on_the_descendant",
        "rust",
    ),
)


def _node(module: str, case: str) -> str:
    """Return the node ID pytest gives *case* in *module*."""
    return f"{module}::{case}"


def _extension_deselections() -> tuple[Deselection, ...]:
    """Return one reasoned removal for every Rust-backend arm."""
    return tuple(
        Deselection(_node(module, f"{case}[{parameter}]"), _EXTENSION_ABSENT)
        for module, case, parameter in _EXTENSION_CASES
    )


#: Cases removed before pytest starts. Every entry is passed to pytest twice:
#: once as ``--deselect`` and once as ``--expected-deselect``, so the guard
#: fails when a predicted removal no longer matches and when anything else
#: removes a case.
DESELECTED: typ.Final[tuple[Deselection, ...]] = (
    Deselection(
        _node(_PROBE_MODULE, "test_a_live_process_is_reported_running[procfs]"),
        _NO_PROCFS,
    ),
    Deselection(
        _node(
            _PROBE_MODULE,
            "test_an_unreaped_zombie_is_reported_exited[procfs]",
        ),
        _NO_PROCFS,
    ),
    Deselection(
        _node(_PROBE_MODULE, "test_a_reaped_process_is_reported_exited[procfs]"),
        _NO_PROCFS,
    ),
    Deselection(
        _node(
            _DETACHED_LIFECYCLE_MODULE,
            "test_the_launch_leaves_no_descriptor_behind",
        ),
        _DESCRIPTOR_COUNT,
    ),
    *_extension_deselections(),
)

#: The node IDs the leg promises to execute and pass. Each is the case a
#: macOS-specific defect in signal delivery, reaping, or pipe behaviour would
#: change, so a skip here is a gap rather than a result.
REQUIRED_PASSING: typ.Final[tuple[str, ...]] = (
    _node(_PROBE_MODULE, "test_a_live_process_is_reported_running[ps]"),
    _node(_PROBE_MODULE, "test_an_unreaped_zombie_is_reported_exited[ps]"),
    _node(_PROBE_MODULE, "test_a_reaped_process_is_reported_exited[ps]"),
    _node(
        _CANCELLED_MODULE,
        "test_cancelled_run_returns_while_a_descendant_holds_its_pipes[inherit]",
    ),
    _node(
        _CANCELLED_MODULE,
        "test_cancelled_run_returns_while_a_descendant_holds_its_pipes[own-group]",
    ),
    _node(
        _CANCELLED_MODULE,
        "test_an_unobservable_descendant_is_skipped_not_passed[inherit]",
    ),
    _node(
        _CANCELLED_MODULE,
        "test_an_unobservable_descendant_is_skipped_not_passed[own-group]",
    ),
)


def _host_report() -> tuple[str, ...]:
    """Return the host facts a triage needs, one line per fact."""
    ps_path = shutil.which("ps") or "<absent>"
    waitid = "available" if hasattr(os, "waitid") else "absent"
    return (
        f"python: {sys.version.split()[0]} ({sys.executable})",
        f"platform: {sys.platform} ({platform.platform()})",
        f"waitid: {waitid}",
        f"ps: {ps_path}",
        "stream backend: python (compiled extension absent by design)",
    )


def _pytest_arguments(extra: cabc.Sequence[str]) -> list[str]:
    """Return the guarded pytest argv for this manifest."""
    arguments = [
        "-v",
        "-rA",
        # The leg must be serial: the guard cannot observe another worker, and
        # the process-lifecycle cases contend for the same process table.
        "-p",
        "no:xdist",
        "-p",
        "tests.helpers.required_outcomes",
    ]
    for node_id in REQUIRED_PASSING:
        arguments.extend(("--required-outcome", node_id))
    for entry in DESELECTED:
        arguments.extend(("--deselect", entry.node_id))
        arguments.extend(("--expected-deselect", entry.node_id))
    arguments.extend(TARGETS)
    arguments.extend(extra)
    return arguments


def main(extra: cabc.Sequence[str] = ()) -> int:
    """Run the guarded selection and return pytest's exit status.

    Parameters
    ----------
    extra:
        Additional pytest arguments, for a developer's local triage. They are
        appended after the manifest's own arguments so they cannot displace
        the selection or the guard.

    Returns
    -------
    int
        Pytest's exit status: zero only when every selected case passed and
        every required outcome was observed.
    """
    # Fixed before pytest imports the repository conftest, which imports
    # cuprum and resolves the stream backend at import time.
    os.environ["CUPRUM_STREAM_BACKEND"] = "python"
    os.environ.pop("CUPRUM_REQUIRE_RUST_EXTENSION", None)

    import pytest

    for line in _host_report():
        print(f"[macos-lifecycle] {line}")
    return int(pytest.main(_pytest_arguments(tuple(extra))))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
