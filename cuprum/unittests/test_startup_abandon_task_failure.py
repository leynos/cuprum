"""A task the failed setup started may fail on its own, and must be reported.

The reclaim cancels the tasks a failed setup left running, then gathers them.
The cancellation it asked for is expected and silent. A task that had already
failed before the cancel arrived is a different matter: the setup raised before
it could await that task, so the reclaim is the only place its error can
surface. Dropping it would leave the caller with a startup failure that hides
whatever the task actually hit.

Both halves are pinned here against the real gather: a done task carrying an
exception and a task that the reclaim's own cancel stops.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ

from cuprum import _subprocess_startup_abandon as reclaim
from cuprum.capture_policy import FileBackedCapture
from cuprum.unittests._startup_abandon_support import _execution

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    import pytest


def _fake_process() -> asyncio.subprocess.Process:
    """Return the minimum the settle step reads from a child."""
    return typ.cast(
        "asyncio.subprocess.Process",
        types.SimpleNamespace(pid=4242, stdin=None, stdout=None, stderr=None),
    )


def _record_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, BaseException]]:
    """Record each reported operation and the error it described.

    Returns
    -------
    list[tuple[str, BaseException]]
        One ``(operation, error)`` pair per report, in the order they arrived.
    """
    reported: list[tuple[str, BaseException]] = []

    def report(
        _process: object,
        _execution: object,
        *,
        operation: str,
        error: BaseException,
    ) -> None:
        """Record the attributed operation and the suppressed error."""
        reported.append((operation, error))

    monkeypatch.setattr(reclaim, "_report_reclaim_failure", report)
    return reported


async def _settle_with(
    tmp_path: pathlib.Path,
    tasks: cabc.Sequence[asyncio.Task[object]],
) -> None:
    """Settle *tasks* after each has had its first scheduling turn."""
    # The turn matters: a waiting task must reach its park before the cancel
    # the settlement step sends, or the case would test a task that never ran.
    await asyncio.sleep(0)
    await reclaim._settle_recorded_tasks(
        _fake_process(),
        _execution(FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path)),
        list(tasks),
    )


def test_a_task_that_had_already_failed_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A task's own failure must not be swallowed as an expected cancellation.

    The setup raised before it awaited this task, so nothing else holds the
    error. Cancelling a task that has already finished does nothing, and the
    gather returns that task's exception — which is exactly the case that has
    to be reported rather than read as the cancellation the step requested.
    """
    reported = _record_reports(monkeypatch)
    task_error = ValueError("the consumer failed on its own")

    async def run_case() -> None:
        """Settle a task that has provably already finished with an error."""

        async def failing() -> None:
            """Fail as a broken consumer would, once scheduled."""
            await asyncio.sleep(0)
            raise task_error

        task = asyncio.ensure_future(failing())
        # Waiting for the task, rather than only giving it a turn, makes the
        # premise of this case exact: it has already failed when the reclaim
        # meets it, so the cancel cannot be what ends it.
        await asyncio.wait([task])
        await _settle_with(tmp_path, [task])

    asyncio.run(run_case())

    assert len(reported) == 1, (
        f"one failed task must produce one report, got {reported!r}"
    )
    operation, error = reported[0]
    assert operation == "task_settle", (
        f"a failed task is attributed to settlement, got {operation!r}"
    )
    assert error is task_error, (
        f"the task's own error must be reported unchanged, got {error!r}"
    )


def test_the_cancellation_the_step_requested_is_not_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A task stopped by the reclaim's own cancel is expected, not a failure.

    Reporting it would turn every abandoned startup into a reclaim warning,
    since the reclaim always cancels the tasks it is settling. The gather
    yields a ``CancelledError``, which is a ``BaseException`` and therefore
    must not be mistaken for the ordinary task failure above.
    """
    reported = _record_reports(monkeypatch)
    parked = asyncio.Event()

    async def run_case() -> None:
        """Settle a task that is still waiting when the cancel arrives."""

        async def waiting() -> None:
            """Wait to be cancelled, as a live consumer would."""
            await parked.wait()

        await _settle_with(tmp_path, [asyncio.ensure_future(waiting())])

    asyncio.run(run_case())

    assert not reported, (
        f"the cancellation the step itself requested must stay silent, got {reported!r}"
    )
