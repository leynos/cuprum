"""Unit tests for pipeline stream configuration."""

from __future__ import annotations

import io
import typing as typ

import pytest

from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._streams import _StreamConfig


def _stdout_config(config: _PipelineRunConfig) -> _StreamConfig:
    """Return the stdout stream configuration, naming the access route."""
    return config.stream_config


def _stderr_config(config: _PipelineRunConfig) -> _StreamConfig:
    """Return the stderr stream configuration, naming the access route."""
    return config.stderr_stream_config


@pytest.mark.parametrize(
    ("stream", "expected_echo", "sink_name", "accessor"),
    [
        pytest.param("stdout", True, "stdout", _stdout_config, id="stdout"),
        pytest.param("stderr", False, "stderr", _stderr_config, id="stderr"),
    ],
)
def test_stream_config_uses_requested_stream_settings(
    stream: typ.Literal["stdout", "stderr"],
    expected_echo: bool,
    sink_name: typ.Literal["stdout", "stderr"],
    accessor: cabc.Callable[[_PipelineRunConfig], _StreamConfig],
) -> None:
    """Each stream keeps its own echo route while sharing decode settings."""
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()
    context = ExecutionContext(encoding="latin-1", errors="ignore")
    config = _PipelineRunConfig(
        ctx=context,
        capture=True,
        echo_stdout=True,
        echo_stderr=False,
        max_echo_line_bytes=123,
        timeout=None,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
        # No presentation session: the run's mirrored streams fall back to the
        # sinks supplied above, which is what this test asserts they retain.
        sink_bracket=_SinkBracket(None),
    )

    stream_config = accessor(config)
    expected_sink = stdout_sink if sink_name == "stdout" else stderr_sink

    assert stream_config.echo_output is expected_echo, (
        f"{stream} must retain its configured echo setting"
    )
    assert stream_config.sink is expected_sink, (
        f"{stream} must retain its configured output sink"
    )
    assert stream_config.capture_output is True, (
        "capture must be shared by both streams"
    )
    assert stream_config.echo_max_line_bytes == 123, (
        "the echo line bound must be shared by both streams"
    )
    assert stream_config.encoding == "latin-1", (
        "the decode encoding must be shared by both streams"
    )
    assert stream_config.errors == "ignore", (
        "the decode error policy must be shared by both streams"
    )
