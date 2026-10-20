"""Per-stream echo routing through pipeline execution."""

from __future__ import annotations

import dataclasses as dc
import io
import typing as typ

import pytest

from cuprum import RunOutputOptions, ScopeConfig, scoped
from cuprum.sh import ExecutionContext, PipelineResult

if typ.TYPE_CHECKING:
    from tests.helpers.catalogue import PythonCatalogue


def _run_per_stream_echo_pipeline(
    env: PythonCatalogue,
    *,
    capture: bool,
    stream: str,
) -> tuple[PipelineResult, io.StringIO, io.StringIO]:
    """Run a two-stage pipeline echoing exactly one of its streams.

    Parameters
    ----------
    env:
        Catalogue bundle supplying the interpreter builder and allowlist entry.
    capture:
        Whether the pipeline should capture each stage's stdio.
    stream:
        The stream selected for echo; the other stream stays muted.

    Returns
    -------
    tuple[PipelineResult, io.StringIO, io.StringIO]
        The pipeline result together with the stdout and stderr echo sinks,
        both injected via ``ExecutionContext`` so neither reaches the parent.
    """
    python = env.builder
    # The producer writes to both streams so each echo gate can be observed
    # independently; the consumer relays stdin so the pipeline is two stages.
    producer = python(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )
    consumer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )
    echo_stdout = stream == "stdout"
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()

    with scoped(ScopeConfig(allowlist=frozenset([env.program]))):
        result = (producer | consumer).run_sync(
            output=RunOutputOptions(
                capture=capture,
                echo_stdout=echo_stdout,
                echo_stderr=not echo_stdout,
            ),
            context=ExecutionContext(stdout_sink=stdout_sink, stderr_sink=stderr_sink),
        )

    return result, stdout_sink, stderr_sink


def _assert_only_selected_stream_echoed(
    stream: str,
    stdout_sink: io.StringIO,
    stderr_sink: io.StringIO,
) -> None:
    """Assert the selected stream reached its sink and the other stayed silent.

    Parameters
    ----------
    stream:
        The stream selected for echo; every other stream must be empty.
    stdout_sink:
        Sink that received stdout echo output.
    stderr_sink:
        Sink that received stderr echo output.
    """
    if stream == "stdout":
        assert "out" in stdout_sink.getvalue(), (
            "stdout echo must follow echo_stdout=True"
        )
        assert not stderr_sink.getvalue(), (
            "stderr must stay silent while only stdout echoes"
        )
    else:
        assert "err" in stderr_sink.getvalue(), (
            "stderr echo must follow echo_stderr=True"
        )
        assert not stdout_sink.getvalue(), (
            "stdout must stay silent while only stderr echoes"
        )


@dc.dataclass(frozen=True, slots=True)
class _EchoRoutingCase:
    """Expected capture values for one pipeline echo-routing run."""

    capture: bool
    expected_stdout: str | None
    expected_stderr: str | None


class TestPipelineEchoRouting:
    """Verify per-stream echo routing through pipeline execution."""

    @staticmethod
    @pytest.mark.usefixtures("stream_backend")
    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _EchoRoutingCase(
                    capture=True,
                    expected_stdout="out\n",
                    expected_stderr="",
                ),
                id="capture",
            ),
            pytest.param(
                _EchoRoutingCase(
                    capture=False,
                    expected_stdout=None,
                    expected_stderr=None,
                ),
                id="echo-only",
            ),
        ],
    )
    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_pipeline_per_stream_echo_respects_capture_setting(
        python_catalogue_env: PythonCatalogue,
        *,
        case: _EchoRoutingCase,
        stream: str,
    ) -> None:
        """Pipeline echo routing preserves the selected capture contract."""
        result, stdout_sink, stderr_sink = _run_per_stream_echo_pipeline(
            python_catalogue_env,
            capture=case.capture,
            stream=stream,
        )

        assert result.ok is True, "the pipeline should succeed"
        assert result.final.stdout == case.expected_stdout, (
            f"capture={case.capture}: final stdout mismatch"
        )
        assert result.final.stderr == case.expected_stderr, (
            f"capture={case.capture}: final stderr mismatch"
        )
        _assert_only_selected_stream_echoed(stream, stdout_sink, stderr_sink)
