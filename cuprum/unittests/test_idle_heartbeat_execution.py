"""Public-API coverage for the idle heartbeat over real single commands.

The state machine and its rendering are pinned without a clock in
``test_idle_heartbeat.py``; what is left to prove here is the wiring: that a
genuinely quiet child is reported, that every shape of real output silences the
heartbeat again, and that the diagnostic never reaches a capture buffer or a
line observer. The exit paths -- callbacks that fail, deadlines, cancellation,
abandoned pipes -- live in ``test_idle_heartbeat_lifecycle.py``, and the
multi-command cases in ``test_idle_heartbeat_coordination.py``.

Intervals are fractions of a second and children synchronize on their own
sleeps, so the assertions are about *ordering*: was the deadline pushed out,
did a notification follow a reset. Exact wall-clock timings are never asserted,
because spawn latency can only make an observation later, never earlier.
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

_INTERVAL = 0.12
_MAX_LINE_BYTES = 512
# Several notifications long, so a child's resumed output is unambiguous: the
# idle age it ends is measured in whole intervals.
_QUIET_SPELL = 0.4


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def test_quiet_child_is_reported_in_bounded_lines(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A child silent for an interval is reported, once per further interval."""
    sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(0.45); print('done')")

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.1),
            context=ExecutionContext(stderr_sink=sink),
        ),
    )

    lines = keepalives(sink)
    assert len(lines) >= 2, f"a quiet child must be reported for lines={lines!r}"
    for line in lines:
        assert line.startswith("[cuprum] still running"), f"unexpected line {line!r}"
        assert line.isascii(), f"the keepalive must survive an ASCII sink: {line!r}"
        assert len(line.encode("ascii")) <= _MAX_LINE_BYTES, (
            f"unbounded keepalive {line!r}"
        )
    assert result.exit_code == 0, f"the child must still exit cleanly: {result!r}"
    assert result.stdout == "done\n", f"capture must be unaffected: {result.stdout!r}"
    assert "[cuprum]" not in (result.stdout or ""), (
        "the keepalive must never enter captured stdout"
    )
    assert "[cuprum]" not in (result.stderr or ""), (
        "the keepalive must never enter captured stderr"
    )


def test_resumed_output_resets_the_reported_idle_age(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Output after a quiet spell resets the interval for the next spell."""
    recorder = IdleRecorder()
    command = python_builder(
        "-c",
        "import time; time.sleep(0.3); print('tick', flush=True); time.sleep(0.3)",
    )

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    assert len(recorder.seen) >= 2, f"too few notifications: {recorder.seen!r}"
    assert recorder.reset_after_output(), (
        "resumed output must reset the reported idle age for "
        f"notifications={recorder.seen!r}"
    )


@pytest.mark.parametrize("stream", ["sys.stdout", "sys.stderr"])
def test_single_stream_output_resets_the_interval(
    python_builder: cabc.Callable[..., SafeCmd],
    stream: str,
) -> None:
    """Either stream, on its own, counts as the child talking."""
    recorder = IdleRecorder()
    source = (
        "import sys, time; time.sleep(0.25); "
        f"print('tick', file={stream}, flush=True); time.sleep(0.35)"
    )

    asyncio.run(
        python_builder("-c", source).run(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    assert recorder.reset_after_output(), (
        f"output on {stream} must reset the interval for {recorder.seen!r}"
    )


def test_alternating_streams_share_one_activity_clock(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A read on either stream defers the deadline the other one armed."""
    recorder = IdleRecorder()
    command = python_builder(
        "-c",
        f"import sys, time; time.sleep({_QUIET_SPELL}); print('out', flush=True); "
        f"time.sleep({_QUIET_SPELL}); print('err', file=sys.stderr, flush=True); "
        f"time.sleep({_QUIET_SPELL})",
    )

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    # Each stream's write ends a quiet spell, and every ending shows up as a
    # notification reporting less idle time than the one before it. A per-stream
    # clock could still produce one of those; two of them, one per stream, can
    # only come from both streams feeding the same deadline.
    assert recorder.resets() >= 2, (
        "each stream's output must defer the shared deadline for "
        f"notifications={recorder.seen!r}"
    )


@pytest.mark.parametrize(
    "write",
    [
        "sys.stdout.write('partial'); sys.stdout.flush()",
        "sys.stdout.buffer.write(b'\\xe2\\x82'); sys.stdout.buffer.flush()",
    ],
    ids=["partial-line", "split-multibyte"],
)
def test_output_that_completes_nothing_still_resets_the_interval(
    python_builder: cabc.Callable[..., SafeCmd],
    write: str,
) -> None:
    """A line-less or half-decoded write is still the child producing output."""
    recorder = IdleRecorder()
    source = (
        f"import sys, time; time.sleep({_QUIET_SPELL}); {write}; "
        f"time.sleep({_QUIET_SPELL})"
    )

    asyncio.run(
        python_builder("-c", source).run(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        ),
    )

    assert recorder.seen, "the quiet tail must still be reported"
    assert recorder.reset_after_output(), (
        "the write must have pushed the deadline out rather than been ignored, "
        f"for notifications={recorder.seen!r}"
    )


@pytest.mark.parametrize(
    ("capture", "echo"), list(itertools.product([False, True], repeat=2))
)
def test_capture_and_echo_combinations_keep_the_keepalive_out_of_the_result(
    python_builder: cabc.Callable[..., SafeCmd],
    *,
    capture: bool,
    echo: bool,
) -> None:
    """Watching for silence neither forces capture nor disappears with it."""
    sink = io.StringIO()
    command = python_builder("-c", "import time; time.sleep(0.35); print('done')")

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=capture, echo=echo, idle_after=0.1),
            context=ExecutionContext(stderr_sink=sink, stdout_sink=io.StringIO()),
        ),
    )

    assert len(keepalives(sink)) >= 1, (
        f"capture={capture}, echo={echo} lost its keepalive for {sink.getvalue()!r}"
    )
    if capture:
        assert result.stdout == "done\n", f"capture lost output: {result.stdout!r}"
    else:
        assert result.stdout is None, f"capture was forced on: {result.stdout!r}"
    assert "[cuprum]" not in (result.stderr or ""), (
        "the keepalive must never be captured as child output"
    )


def test_a_caller_callback_displaces_the_built_in_renderer(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Supplying a callback replaces the diagnostic rather than joining it."""
    sink = io.StringIO()
    recorder = IdleRecorder()
    command = python_builder("-c", "import time; time.sleep(0.3); print('done')")

    asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.08, on_idle=recorder),
            context=ExecutionContext(stderr_sink=sink),
        ),
    )

    assert recorder.seen, "the caller's callback must have been invoked"
    assert not keepalives(sink), (
        f"both renderers ran for sink={sink.getvalue()!r}; the callback replaces it"
    )
