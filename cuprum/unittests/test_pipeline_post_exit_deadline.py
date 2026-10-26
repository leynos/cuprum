"""A pipeline's post-exit drain must not outlive its configured deadline.

A pipeline stages its output gathering *after* the stage wait has finished:
``_collect_pipeline_inputs`` computes the remaining budget for the wait, then
awaits every stage's stderr consumer and the final stage's stdout consumer with
no bound of its own. A descendant that inherits those pipes and outlives its
stage therefore holds the gather open for as long as it likes, and the
configured ``timeout=`` never ends the run.

The success path is the discriminating route: the stage wait returns normally,
so no timeout handler runs and nothing has terminated the descendant that
keeps the pipes open. Three sibling cases cover the rest of the matrix, one
test each rather than a product: the wait-timeout route, which shares the same
handler; ``INHERIT``, because only an owning run may signal the stage's group
at all; and an uncaptured run, which has no consumer to bound. Each case fixes
its own policy and capture setting because they assert four different outcomes
— bounded expiry, reclamation, survival, and prompt completion — not one
outcome under four settings.

The whole module is POSIX-only. Three cases configure ``OWN_GROUP``, which the
policy rejects on Windows rather than emulating, and all four reap their
descendant with ``SIGKILL``. The module is one of ``EXTENSION_TEST_TARGETS``,
so the extension-gated CI job collects it on a Windows runner as well; the
skip below is what turns that collection into recorded skips rather than a
run of failures about a primitive the platform does not have.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._pipeline_deadline_support import (
    _HARNESS_BUDGET_S,
    DEADLINE_S,
    _context,
    _pipeline,
    _run_to_deadline,
    pipeline_run_fn,
    reset_stream_dispatch_test_hooks,
    stream_backend,
)
from cuprum.unittests._post_exit_teardown_support import reap_descendant
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import process_is_running, started_pids, wait_for_pid_file

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._backend import StreamBackend
    from cuprum.events import ExecEvent
    from cuprum.unittests._pipeline_deadline_support import _PipelineRunFn

__all__ = ["pipeline_run_fn", "reset_stream_dispatch_test_hooks", "stream_backend"]
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups are unavailable on Windows"
)


def test_success_route_drain_is_bounded_by_the_configured_deadline(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A descendant holding the pipes cannot extend a *successful* pipeline.

    No handler runs on this route: the stage wait returns normally, and the
    post-exit gather is the only thing that awaits the descendant's pipes. An
    unbounded gather therefore hangs here for as long as the descendant lives.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "success-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=False)
    descendant: int | None = None

    try:
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            expired, leaked = _run_to_deadline(
                pipeline,
                pipeline_run_fn,
                policy=ProcessGroupPolicy.OWN_GROUP,
                capture=True,
            )
            descendant = wait_for_pid_file(
                pid_file,
                context="the success-route group reclamation",
            )

        assert expired.timeout == DEADLINE_S, (
            f"the expiry must report the configured timeout {DEADLINE_S!r}, "
            f"got {expired.timeout!r}"
        )
        assert not leaked, f"the run left pending tasks behind: {leaked!r}"
        assert not process_is_running(descendant), (
            f"an owning run must reclaim descendant pid {descendant}, "
            "even when every stage exited on its own"
        )
    finally:
        # ``None`` makes the reaper read the pid file itself, so a run that
        # failed before recording a descendant still exits through one path.
        reap_descendant(pid_file, descendant)


def test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On the wait-timeout route the settlement is bounded and settles stages.

    ``OWN_GROUP`` is the arm that may signal the stage's group, so the
    descendant is expected to be gone when the run returns. The expiry itself
    must still report the configured deadline and preserve captured output.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "timeout-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=True)
    events: list[ExecEvent] = []
    descendant: int | None = None

    try:
        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(events.append),
        ):
            expired, leaked = _run_to_deadline(
                pipeline,
                pipeline_run_fn,
                policy=ProcessGroupPolicy.OWN_GROUP,
                capture=True,
            )
            descendant = wait_for_pid_file(
                pid_file,
                context="the pipeline deadline reclamation",
            )

        assert expired.timeout == DEADLINE_S, (
            f"the expiry must report the configured timeout {DEADLINE_S!r}, "
            f"got {expired.timeout!r}"
        )
        assert not leaked, f"the run left pending tasks behind: {leaked!r}"
        assert not process_is_running(descendant), (
            f"an owning run must reclaim descendant pid {descendant}, "
            f"stages={started_pids(events)!r}"
        )
    finally:
        reap_descendant(pid_file, descendant)


def test_wait_timeout_drain_is_bounded_under_an_inherited_group(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``INHERIT`` bounds the run without signalling a group it does not own.

    The descendant survives by design — the only process an inherited run may
    signal is the direct child, which has already exited — so what is asserted
    is a bounded settlement rather than a reclamation.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "inherit-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=True)
    descendant: int | None = None

    try:
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            expired, leaked = _run_to_deadline(
                pipeline,
                pipeline_run_fn,
                policy=ProcessGroupPolicy.INHERIT,
                capture=True,
            )
            descendant = wait_for_pid_file(
                pid_file,
                context="the inherited-group deadline settlement",
            )

        assert expired.timeout == DEADLINE_S, (
            f"the expiry must report the configured timeout {DEADLINE_S!r}, "
            f"got {expired.timeout!r}"
        )
        assert not leaked, f"the run left pending tasks behind: {leaked!r}"
        assert process_is_running(descendant), (
            f"an inherited run must leave descendant pid {descendant} alive"
        )
    finally:
        reap_descendant(pid_file, descendant)


def test_uncaptured_success_route_does_not_wait_on_the_descendant(
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pipeline that consumes no output has nothing to bound.

    Its stages get ``DEVNULL`` for the streams it does not read, so a
    descendant cannot hold the gather open and the run finishes on its own.
    The case is the no-capture counterpart of the capturing success route: if
    it ever starts blocking, the difference names the consumer rather than the
    deadline.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "uncaptured-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=False)

    descendant: int | None = None
    try:
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            result = asyncio.run(
                asyncio.wait_for(
                    pipeline.run(
                        timeout=DEADLINE_S,
                        output=RunOutputOptions(capture=False),
                        context=_context(ProcessGroupPolicy.OWN_GROUP),
                    ),
                    _HARNESS_BUDGET_S,
                ),
            )
            descendant = wait_for_pid_file(
                pid_file,
                context="the uncaptured success route",
            )

        assert [stage.exit_code for stage in result.stages] == [0, 0], (
            f"both stages must report a normal exit, got {result.stages!r}"
        )
    finally:
        # Inside the ``finally`` from the start: a run that raises, or a pid
        # file that never appears, must not leave a ``SIGTERM``-immune
        # descendant on the shared host. ``None`` makes the reaper read the pid
        # file itself, so a run that failed before recording a descendant still
        # exits through one path.
        reap_descendant(pid_file, descendant)
