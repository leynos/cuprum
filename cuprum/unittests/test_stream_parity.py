"""Unit tests for stream backend parity across edge cases.

These tests verify that both the Python and Rust stream backends produce
identical pipeline output for empty streams, partial UTF-8 at chunk
boundaries, broken pipes, and backpressure scenarios.

Example
-------
pytest cuprum/unittests/test_stream_parity.py
"""

from __future__ import annotations

import typing as typ

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


def test_empty_pipeline_produces_empty_stdout(
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

    assert result.stdout == ""
    assert result.ok is True
    assert len(result.stages) == 2
    assert all(s.exit_code == 0 for s in result.stages)


def test_utf8_two_byte_chars_survive_pipeline(
    stream_backend: str,
) -> None:
    """Pipeline preserves 2-byte UTF-8 characters (Latin accented).

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """
    # 5000 repetitions of e-acute (2 bytes each) = 10 KB.
    payload = "\u00e9" * 5000
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )
    pipeline, allowlist = _build_pipeline(script)
    result = run_parity_pipeline(pipeline, allowlist)

    assert result.stdout == payload
    assert result.ok is True


def test_utf8_three_byte_chars_survive_pipeline(
    stream_backend: str,
) -> None:
    """Pipeline preserves 3-byte UTF-8 characters (snowman).

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """
    # 3000 snowmen (3 bytes each) = 9 KB, exceeding _READ_SIZE.
    payload = "\u2603" * 3000
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )
    pipeline, allowlist = _build_pipeline(script)
    result = run_parity_pipeline(pipeline, allowlist)

    assert result.stdout == payload
    assert result.ok is True


def test_utf8_four_byte_chars_survive_pipeline(
    stream_backend: str,
) -> None:
    """Pipeline preserves 4-byte UTF-8 characters (emoji).

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """
    # 2500 grinning face emoji (4 bytes each) = 10 KB.
    payload = "\U0001f600" * 2500
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({payload!r}.encode('utf-8')); "
        "sys.stdout.buffer.flush()"
    )
    pipeline, allowlist = _build_pipeline(script)
    result = run_parity_pipeline(pipeline, allowlist)

    assert result.stdout == payload
    assert result.ok is True


def test_mixed_utf8_payload_survives_pipeline(
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

    assert result.stdout == payload
    assert result.ok is True


def test_broken_pipe_downstream_early_exit(
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
    pipeline = python_cmd("-c", upstream_script) | python_cmd("-c", downstream_script)
    allowlist = frozenset([python_prog])
    result = run_parity_pipeline(pipeline, allowlist)

    # The pipeline must complete (no deadlock). If it hangs,
    # pytest-timeout kills the test. The downstream reads 10 bytes.
    assert result.stages is not None
    assert len(result.stages) == 2


def test_backpressure_large_payload_three_stages(
    stream_backend: str,
) -> None:
    """Three-stage pipeline preserves a 1 MB payload.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """
    size = 1024 * 1024
    # Generate payload inside the subprocess to stay within the OS
    # command-line length limit.
    script = f"import sys; sys.stdout.write('x' * {size})"
    pipeline, allowlist = _build_pipeline(script, stages=3)
    result = run_parity_pipeline(pipeline, allowlist)

    assert result.stdout == "x" * size
    assert result.ok is True
    assert len(result.stages) == 3
    assert all(s.exit_code == 0 for s in result.stages)


def test_backpressure_large_payload_two_stages(
    stream_backend: str,
) -> None:
    """Two-stage pipeline preserves a 1 MB payload.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """
    size = 1024 * 1024
    # Generate payload inside the subprocess to stay within the OS
    # command-line length limit.
    script = f"import sys; sys.stdout.write('x' * {size})"
    pipeline, allowlist = _build_pipeline(script)
    result = run_parity_pipeline(pipeline, allowlist)

    assert result.stdout == "x" * size
    assert result.ok is True
    assert len(result.stages) == 2
    assert all(s.exit_code == 0 for s in result.stages)
