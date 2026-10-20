"""Reader failures must survive the post-exit drain's deadline handling.

A reader that ends with its own ``TimeoutError`` — an ``on_line`` hook using
``asyncio.timeout`` internally, for instance — is not the run's deadline. The
post-exit drain reconciles the run, then hands the reader's exception to the
caller unchanged, so the caller can still inspect the error that actually ended
the run.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, observe_line_stream
from cuprum.line_stream_events import LineStreamEvent, LineStreamPhase
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineEvent
    from cuprum.sh import SafeCmd


class _ReaderTimeoutError(TimeoutError):
    """Sentinel reader failure that is also a ``TimeoutError``."""


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


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

    def fail_reader(_line: str) -> None:
        """Fail the run from an ``on_line`` hook with a ``TimeoutError``."""
        raise sentinel

    with pytest.raises(_ReaderTimeoutError) as caught:
        asyncio.run(command.run(output=RunOutputOptions(on_line=fail_reader)))

    assert caught.value is sentinel, (
        f"the caller must receive the reader's own exception, got {caught.value!r}"
    )
