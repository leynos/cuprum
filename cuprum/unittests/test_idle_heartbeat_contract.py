"""What a reported run still returns, and what its diagnostic still says.

Idle reporting is opt-in, so enabling it must leave every *other* documented
behaviour exactly as it was: the same captured bytes as the same run without a
heartbeat, the same mirroring, the same result from the synchronous entry
point, and nothing new in any of them. The generated line itself has a contract
too -- bounded, ASCII-safe, control-safe, and free of the arguments the run was
given -- which is what makes it safe to write to a destination whose encoding
and buffering are not the parent's to choose.

The exit paths, and what they clean up, live in
``test_idle_heartbeat_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.idle import IdleRecorder, keepalives

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd

_MAX_LINE_BYTES = 512
_QUIET_SECONDS = 0.35


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def test_keepalive_reaches_a_narrow_encoding_buffered_destination(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A destination with neither our encoding nor our buffering is safe."""
    buffer = io.BytesIO()
    sink = io.TextIOWrapper(buffer, encoding="cp1252", newline="")
    command = python_builder("-c", f"import time; time.sleep({_QUIET_SECONDS})")

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.05),
            context=ExecutionContext(stderr_sink=typ.cast("typ.IO[str]", sink)),
        ),
    )

    written = buffer.getvalue().decode("cp1252")
    lines = [line for line in written.splitlines() if line.startswith("[cuprum]")]
    assert lines, f"the narrow sink must have received a keepalive: {written!r}"
    for line in lines:
        assert len(line.encode("cp1252")) <= _MAX_LINE_BYTES, (
            f"the line must survive the sink's encoding: {line!r}"
        )


def test_keepalive_never_carries_the_arguments_it_reports_on(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The diagnostic names the programme, never the argv that ran it."""
    argv_marker = "an-argument-that-must-not-be-quoted-back"
    sink = io.StringIO()
    command = python_builder(
        "-c",
        f"import time; time.sleep({_QUIET_SECONDS})",
        argv_marker,
    )

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.05),
            context=ExecutionContext(stderr_sink=sink),
        ),
    )

    lines = keepalives(sink)
    assert lines, f"the run must have been reported: {sink.getvalue()!r}"
    for line in lines:
        assert argv_marker not in line, f"argv leaked into the keepalive: {line!r}"
        assert line.isprintable(), f"the line must not inject control codes: {line!r}"


def test_sync_entry_point_reports_like_its_async_twin(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``run_sync`` owns and settles the same watchdog."""
    sink = io.StringIO()
    command = python_builder("-c", f"import time; time.sleep({_QUIET_SECONDS})")

    result = command.run_sync(
        output=RunOutputOptions(idle_after=0.08),
        context=ExecutionContext(stderr_sink=sink),
    )

    assert result.stdout is None or "[cuprum]" not in result.stdout, (
        "the keepalive must never be captured"
    )
    assert keepalives(sink), f"the sync run must report for sink={sink.getvalue()!r}"


def test_caller_callback_sees_its_own_idle_ages(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The documented ``(total, idle)`` contract holds for a real child."""
    recorder = IdleRecorder()
    command = python_builder("-c", f"import time; time.sleep({_QUIET_SECONDS})")

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.05, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    assert recorder.seen, "the callback must have been invoked"
    for total, idle in recorder.seen:
        assert 0 < idle <= total, (
            f"a notification must report 0 < idle <= total, got {(total, idle)!r}"
        )


@pytest.mark.parametrize("capture", [False, True])
def test_idle_reporting_returns_exactly_what_a_quiet_run_returns(
    python_builder: cabc.Callable[..., SafeCmd],
    *,
    capture: bool,
) -> None:
    """Watching for silence changes neither the capture nor its absence."""
    source = (
        "import sys, time; time.sleep(0.25); print('done'); "
        "print('problem', file=sys.stderr)"
    )
    command = python_builder("-c", source)
    context = ExecutionContext(stderr_sink=io.StringIO())

    quiet = asyncio.run(command.run(output=RunOutputOptions(capture=capture)))
    watching = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=capture, idle_after=0.05),
            context=context,
        ),
    )

    assert (quiet.stdout, quiet.stderr) == (watching.stdout, watching.stderr), (
        "idle reporting changed the result for "
        f"capture={capture}: {quiet!r} versus {watching!r}"
    )
    if capture:
        assert watching.stdout == "done\n", (
            f"the capture must be complete for capture={capture}: {watching.stdout!r}"
        )
    else:
        assert watching.stdout is None, (
            f"capture must not be forced on for capture={capture}: {watching.stdout!r}"
        )
    assert keepalives(context.stderr_sink), (
        f"the diagnostic must have gone to the parent's own sink for capture={capture}"
    )


@pytest.mark.parametrize(
    ("capture", "echo"), list(itertools.product([False, True], repeat=2))
)
def test_idle_reporting_never_enters_a_mirrored_stream(
    python_builder: cabc.Callable[..., SafeCmd],
    *,
    capture: bool,
    echo: bool,
) -> None:
    """Neither capture nor echo may see a generated diagnostic."""
    stdout_sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(0.3)")

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=capture, echo=echo, idle_after=0.05),
            context=ExecutionContext(
                stdout_sink=stdout_sink,
                stderr_sink=io.StringIO(),
            ),
        ),
    )

    assert "[cuprum]" not in stdout_sink.getvalue(), (
        f"a keepalive reached the stdout mirror for echo={echo}"
    )
    assert "[cuprum]" not in (result.stderr or ""), (
        f"a keepalive reached captured stderr for capture={capture}"
    )
