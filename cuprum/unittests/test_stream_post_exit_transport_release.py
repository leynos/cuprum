"""The streamed run's own post-exit collection must release its transports.

``SafeCmd.run()`` with capture has a second place a run can stop before its
streams drained: once the child has exited, the collection awaits the stdin
writer and gathers the consumers under what remains of the deadline. A
descendant the child left behind holding the inherited write ends means no
consumer ever reaches end-of-file, so the collection expires or is cancelled
with both parent-side descriptors still open.

Settling the readers is not closing them: a reader cancelled before
end-of-file keeps its descriptor with the transport that owns it. Each case
drives a real child on both spawn backends and asks the descriptors whether
they were released while the descendant is still alive.

Only ``INHERIT`` is exercised. Under ``OWN_GROUP`` the collection's group
teardown signals the descendant, the pipes reach end-of-file, and a closure
assertion could not tell the run's release from the transport closing itself,
so the arm would be no evidence at all.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S
from cuprum._subprocess_stdin_reclaim import _STDIN_RECLAIM_BUDGET_S
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
)
from cuprum.unittests._post_exit_teardown_support import (
    RUN_BUDGET_S,
    owned_builder,
    reap_descendant,
    run_to_its_own_deadline,
    settle,
)
from cuprum.unittests._rendezvous_support import record_rendezvous
from cuprum.unittests._startup_abandon_support import _posix_only
from cuprum.unittests._transport_release_support import (
    BackendTraversal,
    SpawnRecord,
    assert_released_before_the_descendant_ends,
    record_spawn,
    spawn_backend,
)
from tests.helpers.execution import _RunKwargs
from tests.helpers.timeouts import descendant_then_exit_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    # Only the fixture annotations name ``pytest``; every mark this module uses
    # is a value it imports, so the package is not needed at run time.
    import pytest

# Both fixtures are imported rather than defined here; declaring them records
# that pytest resolves the names at collection time rather than leaving them to
# be pruned as unused imports.
__all__ = ["owned_builder", "spawn_backend"]

pytestmark = _posix_only

#: Long enough for the child to fork and exit, short enough that a case which
#: waited for the descendant could not finish inside its own watchdog.
_DEADLINE_S = 1.0

#: Printed by the child before it spawns its descendant, so a case can assert
#: the text the run had already captured survives the expiry.
_MARKER = "before-the-descendant"


def _run_kwargs(*, timeout: float | None = None) -> _RunKwargs:
    """Build the keyword arguments every case drives ``run()`` with.

    Returns
    -------
    _RunKwargs
        The captured, inherited-group invocation this module needs.
    """
    return _RunKwargs(
        output=RunOutputOptions(capture=True, echo=False),
        timeout=timeout,
        context=ExecutionContext(process_group=ProcessGroupPolicy.INHERIT),
    )


def _descendant_leaving_command(
    builder: cabc.Callable[..., SafeCmd],
    pid_file: pathlib.Path,
) -> SafeCmd:
    """Build a child that announces itself, forks, and exits successfully."""
    return builder(
        *descendant_then_exit_argv(
            pid_file,
            exit_code=0,
            stdout_chunks=(f"{_MARKER}\n",),
        )
    )


def test_a_post_exit_deadline_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-exit collection releases the pipes its expiry stopped reading.

    The route preserves what it captured for the ``TimeoutExpired`` it raises,
    and that drain is exactly where a close could be forgotten: the output has
    been taken, so nothing further reads the pipe. Both descriptors must still
    be closed while the descendant holds the write ends.
    """
    context = f"a post-exit expiry on {spawn_backend.requested}"
    pid_file = tmp_path / f"run-{spawn_backend.requested}.pid"
    command = _descendant_leaving_command(owned_builder, pid_file)
    record: SpawnRecord = record_spawn(monkeypatch, "cuprum._subprocess_spawn")
    handoff = record_rendezvous(
        monkeypatch, "cuprum._subprocess_stream_exit._await_exit_or_writer_failure"
    )

    async def run_case() -> None:
        """Run to the post-exit deadline and read what the run left behind."""
        descendant_pid: int | None = None
        try:
            expired = await run_to_its_own_deadline(
                command.run(**_run_kwargs(timeout=_DEADLINE_S)),
                cleanup_allowance_s=_STDIN_RECLAIM_BUDGET_S + _CAPTURE_EOF_GRACE_S,
            )
            assert handoff.is_set(), (
                "the deadline must expire in the post-exit collection, but the "
                "exit rendezvous never settled, so the expiry was reached one "
                "phase earlier and this case proves nothing about it"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
            # Checked for text before membership: the payload is widened for
            # bytes-mode runs, and `str in bytes` is not a meaningful question.
            expired_text = expired.output
            assert isinstance(expired_text, str), (
                "a text-mode run must report its captured text as str, got "
                f"{type(expired_text).__name__}"
            )
            assert _MARKER in expired_text, (
                "the expiry must carry the output the run had already captured, "
                f"got {expired_text!r}"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_post_exit_cancellation_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation after the exit settled must release the abandoned pipes.

    The gate is the real rendezvous returning rather than the child's
    ``returncode``: a cancellation issued before the race settled would prove
    something about the exit wait rather than about the collection this case
    names.
    """
    context = f"a post-exit cancellation on {spawn_backend.requested}"
    pid_file = tmp_path / f"run-cancel-{spawn_backend.requested}.pid"
    command = _descendant_leaving_command(owned_builder, pid_file)
    record: SpawnRecord = record_spawn(monkeypatch, "cuprum._subprocess_spawn")
    handoff = record_rendezvous(
        monkeypatch, "cuprum._subprocess_stream_exit._await_exit_or_writer_failure"
    )

    async def run_case() -> None:
        """Cancel once the exit has settled, then read the descriptors."""
        descendant_pid: int | None = None
        run_task = asyncio.ensure_future(command.run(**_run_kwargs()))
        try:
            await asyncio.wait_for(handoff.wait(), RUN_BUDGET_S)
            run_task.cancel()
            await settle(run_task)
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
