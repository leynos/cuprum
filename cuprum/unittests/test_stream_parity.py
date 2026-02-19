"""Unit tests for stream backend parity across edge cases.

These tests verify that both the Python and Rust stream backends produce
identical pipeline output for empty streams, partial UTF-8 at chunk
boundaries, broken pipes, and backpressure scenarios.

Example
-------
pytest cuprum/unittests/test_stream_parity.py
"""

# ruff: noqa: PLR6301

from __future__ import annotations

import typing as typ

import pytest

from cuprum import sh
from tests.helpers.parity import (
    parity_catalogue,
    run_parity_pipeline,
    utf8_stress_payload,
)

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import Pipeline


def _build_pipeline(
    script: str,
    *,
    stages: int = 2,
) -> tuple[Pipeline, frozenset[Program]]:
    """Build a Python-to-cat pipeline for parity testing.

    Parameters
    ----------
    script : str
        Python script for the first stage.
    stages : int
        Total pipeline stages (2 or 3). Defaults to ``2``.

    Returns
    -------
    tuple[Pipeline, frozenset[Program]]
        The pipeline and the allowlist of programmes required.
    """
    catalogue, python_prog, cat_prog, _ = parity_catalogue()
    python_cmd = sh.make(python_prog, catalogue=catalogue)
    cat_cmd = sh.make(cat_prog, catalogue=catalogue)

    pipeline: Pipeline = python_cmd("-c", script) | cat_cmd()
    if stages == 3:
        pipeline |= cat_cmd()

    allowlist = frozenset([python_prog, cat_prog])
    return pipeline, allowlist


class TestStreamParity:
    """Parity tests for Python and Rust stream backends."""

    def test_empty_pipeline_produces_empty_stdout(
        self,
        stream_backend: str,
    ) -> None:
        """Pipeline with no upstream output produces empty stdout.

        Parameters
        ----------
        stream_backend : str
            The active stream backend (injected by fixture).
        """
        pipeline, allowlist = _build_pipeline("pass")
        result = run_parity_pipeline(pipeline, allowlist)

        assert result.stdout == "", "expected empty stdout"
        assert result.ok is True, "pipeline should succeed"
        assert len(result.stages) == 2, "pipeline should have exactly two stages"
        assert all(s.exit_code == 0 for s in result.stages), (
            "every stage should exit with code 0"
        )

    @pytest.mark.parametrize(
        ("char", "count", "description"),
        [
            pytest.param(
                "\u00e9",
                5000,
                "2-byte UTF-8 characters (Latin accented)",
                id="utf8-2byte",
            ),
            pytest.param(
                "\u2603",
                3000,
                "3-byte UTF-8 characters (snowman)",
                id="utf8-3byte",
            ),
            pytest.param(
                "\U0001f600",
                2500,
                "4-byte UTF-8 characters (emoji)",
                id="utf8-4byte",
            ),
        ],
    )
    def test_utf8_chars_survive_pipeline(
        self,
        stream_backend: str,
        char: str,
        count: int,
        description: str,
    ) -> None:
        """Pipeline preserves {description}.

        Parameters
        ----------
        stream_backend : str
            The active stream backend (injected by fixture).
        char : str
            The Unicode character to repeat.
        count : int
            Number of repetitions.
        description : str
            Human-readable description of the character class.
        """
        payload = char * count
        script = (
            "import sys; "
            f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
            "sys.stdout.buffer.flush()"
        )
        pipeline, allowlist = _build_pipeline(script)
        result = run_parity_pipeline(pipeline, allowlist)

        assert result.stdout == payload, f"{description} should survive pipeline"
        assert result.ok is True, "pipeline should succeed"

    def test_mixed_utf8_payload_survives_pipeline(
        self,
        stream_backend: str,
    ) -> None:
        """Pipeline preserves a large mixed-width UTF-8 payload.

        Parameters
        ----------
        stream_backend : str
            The active stream backend (injected by fixture).
        """
        payload = utf8_stress_payload()
        script = (
            "import sys; "
            f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
            "sys.stdout.buffer.flush()"
        )
        pipeline, allowlist = _build_pipeline(script)
        result = run_parity_pipeline(pipeline, allowlist)

        assert result.stdout == payload, "mixed UTF-8 payload should survive pipeline"
        assert result.ok is True, "pipeline should succeed"

    def test_broken_pipe_downstream_early_exit(
        self,
        stream_backend: str,
    ) -> None:
        """Pipeline handles downstream early exit without deadlock.

        Parameters
        ----------
        stream_backend : str
            The active stream backend (injected by fixture).
        """
        catalogue, python_prog, _, _ = parity_catalogue()
        python_cmd = sh.make(python_prog, catalogue=catalogue)

        upstream_script = (
            "import sys, signal; "
            "signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
            "sys.stdout.buffer.write(b'A' * 65536); "
            "sys.stdout.buffer.flush()"
        )
        downstream_script = "import sys; sys.stdout.write(sys.stdin.read(10))"
        pipeline = python_cmd("-c", upstream_script) | python_cmd(
            "-c", downstream_script
        )
        allowlist = frozenset([python_prog])
        result = run_parity_pipeline(pipeline, allowlist)

        # The pipeline must complete (no deadlock). If it hangs,
        # pytest-timeout kills the test. The downstream reads 10 bytes.
        assert result.stages is not None, "pipeline should have produced stage results"
        assert len(result.stages) == 2, "pipeline should have exactly two stages"
        assert result.stdout == "A" * 10, (
            "downstream should have captured the first 10 bytes from upstream"
        )

    @pytest.mark.parametrize(
        "stages",
        [pytest.param(2, id="two-stages"), pytest.param(3, id="three-stages")],
    )
    def test_backpressure_large_payload(
        self,
        stream_backend: str,
        stages: int,
    ) -> None:
        """Pipeline with {stages} stages preserves a 1 MB payload.

        Parameters
        ----------
        stream_backend : str
            The active stream backend (injected by fixture).
        stages : int
            Number of pipeline stages to use.
        """
        size = 1024 * 1024
        # Generate payload inside the subprocess to stay within the OS
        # command-line length limit.
        script = f"import sys; sys.stdout.write('x' * {size})"
        pipeline, allowlist = _build_pipeline(script, stages=stages)
        result = run_parity_pipeline(pipeline, allowlist)

        assert result.stdout == "x" * size, "payload should survive pipeline intact"
        assert result.ok is True, "pipeline should succeed"
        assert len(result.stages) == stages, (
            f"pipeline should have exactly {stages} stages"
        )
        assert all(s.exit_code == 0 for s in result.stages), (
            "every stage should exit with code 0"
        )
