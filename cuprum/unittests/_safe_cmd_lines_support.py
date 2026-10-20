"""Shared subprocess helpers for SafeCmd line tests."""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent, LineStreamName
    from cuprum.sh import SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _two_stream_script() -> str:
    """Script emitting interleaved stdout and stderr lines."""
    return (
        "import sys\n"
        "print('o1', flush=True)\n"
        "print('e1', file=sys.stderr, flush=True)\n"
        "print('o2', flush=True)\n"
        "print('e2', file=sys.stderr, flush=True)\n"
        "print('o1', flush=True)\n"
        "print('e1', file=sys.stderr, flush=True)\n"
    )


def _assert_line_event_order(
    events: cabc.Sequence[LineEvent],
    stream: LineStreamName,
    expected: cabc.Sequence[str],
) -> None:
    """Assert the decoded line order for one output stream."""
    assert [event.text for event in events if event.stream == stream] == list(
        expected
    ), f"line events must preserve {stream} order, got {events!r}"


def _assert_exec_event_order(
    events: cabc.Sequence[ExecEvent],
    stream: LineStreamName,
    expected: cabc.Sequence[str],
) -> None:
    """Assert the observed output-line order for one stream."""
    assert [event.line for event in events if event.phase == stream] == list(
        expected
    ), f"execution events must preserve {stream} order, got {events!r}"


_BLOCKING_CHILD = "\n".join((
    "import os, pathlib, time",
    "pid_file = pathlib.Path(os.environ['PID'])",
    "pid_tmp = pid_file.with_name(pid_file.name + '.tmp')",
    "pid_tmp.write_text(str(os.getpid()))",
    "pid_tmp.replace(pid_file)",
    "print('ready', flush=True)",
    "time.sleep(300)",
))


def _blocking_command(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> tuple[SafeCmd, Path]:
    """Build a command that writes its pid, prints a line, then blocks."""
    script = tmp_path / "child.py"
    script.write_text(_BLOCKING_CHILD, encoding="utf-8")
    return python_builder(str(script)), tmp_path / "pid"
