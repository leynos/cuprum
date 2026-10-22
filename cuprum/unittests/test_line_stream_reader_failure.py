"""Reader failures must survive the post-exit drain's deadline handling.

A reader that ends with its own ``TimeoutError`` — an ``on_line`` hook using
``asyncio.timeout`` internally, for instance — is not the run's deadline. The
post-exit drain reconciles the run, then hands the reader's exception to the
caller unchanged, so the caller can still inspect the error that actually ended
the run.

Which reader's failure the caller sees is also decided here. Two readers that
fail in the same loop iteration complete together, and the run reports the one
that finished first rather than whichever one an unordered collection happened
to yield: the error a caller reads is what names the defect, so it must not
depend on task hashing.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, StdinStream, observe_line_stream
from cuprum._line_stream.drain import _await_consumers
from cuprum._subprocess_rendezvous import (
    _await_exit_or_writer_failure as _real_rendezvous,
)
from cuprum.line_stream_events import LineStreamEvent, LineStreamPhase
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum.lines import LineEvent
    from cuprum.sh import SafeCmd


class _ReaderTimeoutError(TimeoutError):
    """Sentinel reader failure that is also a ``TimeoutError``."""


class _WriterTimeoutError(TimeoutError):
    """Sentinel stdin-writer failure that is also a ``TimeoutError``."""

    def __init__(self, *, after_exit: bool = False) -> None:
        """Name which route raised the sentinel.

        The message lives on the class rather than at each raise site so the
        sentinel stays a plain marker: what a test asserts about is the
        exception's identity, not its text.
        """
        super().__init__(
            "writer timed out after the exit settled"
            if after_exit
            else "writer timed out"
        )


# RUF029: ``StdinStream`` requires an async iterable, and the reader that
# drives it is a real coroutine; nothing here ever pulls a chunk because
# every patched writer fails first.
async def _no_chunks() -> cabc.AsyncIterator[bytes]:  # ruff: ignore[unused-async]
    """Yield nothing: the patched writer fails before it pulls a chunk."""
    yield b""


# How many times the simultaneous-failure trial is repeated. Two readers that
# fail in the same loop step are the only case where attribution is at issue,
# and there the defect is nondeterminacy rather than a fixed wrong answer: a
# single trial is a coin toss, while twenty-five of them report both answers
# with probability 1 - 0.6**25 if selection is still driven by task hashing.
_SIMULTANEOUS_TRIALS = 25


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


async def _fail_with(error: BaseException) -> _CapturedDrain:
    """Raise *error* from a reader task, standing in for a failed reader."""
    # A checkpoint before raising is what puts two readers' failures in the
    # same loop step: both tasks are started by the wait, both suspend here,
    # and both resume and raise before the wait is released.
    await asyncio.sleep(0)
    raise error


async def _report_failing_reader(sentinels: cabc.Sequence[BaseException]) -> str:
    """Fail both readers at once and name the failure the wait reports.

    Returns
    -------
    str
        ``"stdout"`` or ``"stderr"``, naming which reader's sentinel was
        returned rather than raised.
    """
    stdout_task: asyncio.Task[_CapturedDrain] = asyncio.create_task(
        _fail_with(sentinels[0]),
    )
    stderr_task: asyncio.Task[_CapturedDrain] = asyncio.create_task(
        _fail_with(sentinels[1]),
    )
    try:
        returned = await _await_consumers((stdout_task, stderr_task), None)
    finally:
        # Both exceptions are retrieved here: the reported one is returned
        # rather than raised, so the loser would otherwise be reported as never
        # retrieved when the loop closes.
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    assert returned is not None, "a failing reader must yield its failure"
    return "stdout" if returned is sentinels[0] else "stderr"


def test_two_readers_failing_together_are_attributed_repeatably() -> None:
    """Which of two simultaneous failures is reported never changes.

    Two readers that fail in the same loop step both reach the post-exit wait
    already finished, so the wait reports them as one finished batch. Choosing
    the reported failure by iterating that batch makes the answer depend on
    task hashing, and the run then names a different defect from one process to
    the next — the caller reads the exception to learn what broke, so the
    choice has to come from the order the readers actually completed in.

    The trial is repeated rather than asserted once because the defect is
    nondeterminacy: a single run can pass by luck, and only a set of runs that
    disagrees with itself shows the hashing.
    """
    sentinels = [ValueError("stdout reader"), ValueError("stderr reader")]
    reported = {
        asyncio.run(_report_failing_reader(sentinels))
        for _ in range(_SIMULTANEOUS_TRIALS)
    }

    assert len(reported) == 1, (
        "two readers failing in one step must always be attributed the same "
        f"way, but the run reported {sorted(reported)!r} across "
        f"{_SIMULTANEOUS_TRIALS} trials"
    )


def test_lines_reader_timeout_is_not_reported_as_the_run_deadline(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A reader's own ``TimeoutError`` reaches the caller unchanged.

    The run configures no ``timeout=``, so there is no deadline that could
    legitimately expire. Before the fix the deadline arm caught the reader's
    exception, emitted the ``timeout`` phase, and — with no configured timeout
    to report — replaced it with an invariant failure.
    """
    command = python_builder("-c", "print('reader')")
    sentinel = _ReaderTimeoutError("reader timed out")
    seen: list[LineStreamEvent] = []

    def fail_reader(_event: LineEvent) -> None:
        """Fail the run from an ``on_line`` hook with a ``TimeoutError``."""
        raise sentinel

    async def collect() -> None:
        """Iterate until the reader hook fails."""
        async for _event in command.lines(
            output=RunOutputOptions(on_line=fail_reader),
        ):
            pass

    with (
        observe_line_stream(seen.append),
        pytest.raises(_ReaderTimeoutError) as caught,
    ):
        asyncio.run(collect())

    assert caught.value is sentinel, (
        f"the caller must receive the reader's own exception, got {caught.value!r}"
    )
    phases = [event.phase for event in seen]
    assert LineStreamPhase.TIMEOUT not in phases, (
        f"a reader failure must not report the run's deadline, got {phases!r}"
    )
    assert [(event.sink, event.error_type) for event in seen if event.sink] == [
        ("callback", "_ReaderTimeoutError"),
    ], f"the reader failure must remain observable, got {seen!r}"
    reconciled = {LineStreamPhase.TEARDOWN_STARTED, LineStreamPhase.TEARDOWN_COMPLETED}
    assert reconciled <= set(phases), (
        f"a reader failure must still reconcile the run, got {phases!r}"
    )


def _assert_writer_failure_survives(
    seen: cabc.Sequence[LineStreamEvent],
    caught: pytest.ExceptionInfo[_WriterTimeoutError],
) -> None:
    """Assert the run reported the writer's error rather than a deadline.

    The exception identity is what makes this an attribution assertion rather
    than a type one: the sentinel is raised from the writer and must arrive at
    the caller as that same object, not as anything a timeout path built.
    """
    assert isinstance(caught.value, _WriterTimeoutError), (
        f"the caller must receive the writer's own exception, got {caught.value!r}"
    )
    phases = [event.phase for event in seen]
    assert LineStreamPhase.TIMEOUT not in phases, (
        f"a writer failure must not report the run's deadline, got {phases!r}"
    )
    assert set(phases) >= {
        LineStreamPhase.TEARDOWN_STARTED,
        LineStreamPhase.TEARDOWN_COMPLETED,
    }, f"a writer failure must still reconcile the run, got {phases!r}"


def test_lines_writer_timeout_before_exit_is_not_the_run_deadline(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer failure raised before the exit is reported unchanged.

    This is the exit wait's arm. The exit rendezvous re-raises a failed writer
    unchanged, and a ``TimeoutError`` is an ``OSError`` subclass, so the arm
    that translates this run's expiry saw the producer's error instead. No
    ``timeout=`` is configured, so the translation had no deadline to report
    and replaced the caller's real error with an invariant failure.
    """

    # RUF029: the seam this replaces is awaited by the spawn path, so the
    # double must be a coroutine function even though it awaits nothing.
    async def _fail_before_writing(  # ruff: ignore[unused-async]
        *_: object,
        **__: object,
    ) -> None:
        """Fail the writer with a ``TimeoutError`` before it writes anything."""
        raise _WriterTimeoutError

    monkeypatch.setattr(
        "cuprum._subprocess_stdin_stream._write_stdin_stream",
        _fail_before_writing,
    )
    command = python_builder("-c", "print('reader')")
    seen: list[LineStreamEvent] = []

    async def collect() -> None:
        """Iterate until the writer's failure ends the run."""
        async for _event in command.lines(stdin=StdinStream(_no_chunks())):
            pass

    with (
        observe_line_stream(seen.append),
        pytest.raises(_WriterTimeoutError) as caught,
    ):
        asyncio.run(collect())

    _assert_writer_failure_survives(seen, caught)


def test_lines_writer_timeout_after_exit_is_not_the_run_deadline(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer failure raised after the exit is reported unchanged.

    This is the drain's arm, and it needs the failure to arrive late. The exit
    rendezvous returns the child's result without consulting a writer that is
    still pending, so a writer which fails only after that point is left to the
    post-exit settle — which re-raises it inside the drain's deadline scope.

    The rendezvous is wrapped rather than merely raced, because a writer that
    waits for the child's own ``returncode`` still loses: the return code is
    set before the exit wait resolves, so that writer is already failed by the
    time the rendezvous looks and this case would silently collapse into the
    one above. The wrapper sets its flag *after* the real rendezvous returns,
    which is the earliest moment the drain owns the writer.
    """
    rendezvous_returned = asyncio.Event()

    async def _fail_after_rendezvous(*_: object, **__: object) -> None:
        """Fail the writer once the exit rendezvous has handed the run on."""
        await rendezvous_returned.wait()
        raise _WriterTimeoutError(after_exit=True)

    async def _record_rendezvous(
        exit_wait: cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]],
        stdin_task: asyncio.Task[None] | None,
    ) -> tuple[int, float]:
        """Delegate to the real rendezvous, then hand the run to the drain."""
        result = await _real_rendezvous(exit_wait, stdin_task)
        rendezvous_returned.set()
        return result

    monkeypatch.setattr(
        "cuprum._subprocess_stdin_stream._write_stdin_stream",
        _fail_after_rendezvous,
    )
    # Resolved in the coordinator's globals, which is the seam the module
    # documents for tests: the drain reaches the writer through them.
    monkeypatch.setattr(
        "cuprum._line_stream.coordinator._await_exit_or_writer_failure",
        _record_rendezvous,
    )
    command = python_builder("-c", "print('reader')")
    seen: list[LineStreamEvent] = []

    async def collect() -> None:
        """Iterate until the writer's failure ends the run."""
        async for _event in command.lines(stdin=StdinStream(_no_chunks())):
            pass

    with (
        observe_line_stream(seen.append),
        pytest.raises(_WriterTimeoutError) as caught,
    ):
        asyncio.run(collect())

    assert rendezvous_returned.is_set(), (
        "the writer must fail after the exit rendezvous, or this case does not "
        "reach the drain's arm"
    )
    _assert_writer_failure_survives(seen, caught)


def test_run_reader_timeout_is_not_reported_as_the_run_deadline(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The streamed ``run()`` path preserves a reader's own ``TimeoutError``.

    It reaches the same post-exit collect step through a different route, so
    the boundary is asserted here too: without it the reader's failure is
    translated as the run's deadline and then replaced by the invariant
    failure a missing configured timeout produces.
    """
    command = python_builder("-c", "print('reader')")
    sentinel = _ReaderTimeoutError("reader timed out")

    def fail_reader(_event: LineEvent) -> None:
        """Fail the run from an ``on_line`` hook with a ``TimeoutError``."""
        raise sentinel

    with pytest.raises(_ReaderTimeoutError) as caught:
        asyncio.run(command.run(output=RunOutputOptions(on_line=fail_reader)))

    assert caught.value is sentinel, (
        f"the caller must receive the reader's own exception, got {caught.value!r}"
    )
