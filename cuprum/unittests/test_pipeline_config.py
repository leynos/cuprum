"""Unit tests for pipeline stream configuration."""

from __future__ import annotations

import io
import typing as typ

import pytest

from cuprum._pipeline_config import _PipelineRunConfig
from cuprum.sh import ExecutionContext


@pytest.mark.parametrize(
    ("stream", "expected_echo", "sink_name"),
    [
        pytest.param("stdout", True, "stdout", id="stdout"),
        pytest.param("stderr", False, "stderr", id="stderr"),
    ],
)
def test_stream_config_uses_requested_stream_settings(
    stream: typ.Literal["stdout", "stderr"],
    expected_echo: bool,
    sink_name: typ.Literal["stdout", "stderr"],
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
    )

    stream_config = config.stream_config(stream)
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
