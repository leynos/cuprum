"""Exhaustive tests for the canonical pipeline stdio policy.

``_get_stage_stream_fds`` is the single source of truth for the
PIPE-versus-DEVNULL stdio selection used when spawning pipeline stages, and
``_cwd_arg`` is the shared optional-working-directory conversion used by both
the single-command and pipeline spawn sites. The input domain of both helpers
is small and finite, so these tests cover the full product rather than
sampling it.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from cuprum._pipeline_stage_streams import _get_stage_stream_fds
from cuprum._subprocess_context import _cwd_arg

_PIPE = asyncio.subprocess.PIPE
_DEVNULL = asyncio.subprocess.DEVNULL

# The realistic stage-count bound for exhaustive coverage: one-, two-, and
# three-stage pipelines exercise the first/middle/last positional roles.
_MAX_LAST_IDX = 2


def _expected_stdin(idx: int) -> int:
    """Return the expected stdin FD flag for stage *idx*."""
    return _DEVNULL if idx == 0 else _PIPE


def _expected_stdout(idx: int, last_idx: int, *, consumes_stdout: bool) -> int:
    """Return the expected stdout FD flag for stage *idx*."""
    if idx != last_idx:
        return _PIPE
    return _PIPE if consumes_stdout else _DEVNULL


def _expected_stderr(*, consumes_stderr: bool) -> int:
    """Return the expected stderr FD flag for any stage."""
    return _PIPE if consumes_stderr else _DEVNULL


def _stage_positions() -> list[tuple[int, int]]:
    """Enumerate every (idx, last_idx) pair up to the coverage bound."""
    return [
        (idx, last_idx)
        for last_idx in range(_MAX_LAST_IDX + 1)
        for idx in range(last_idx + 1)
    ]


@pytest.mark.parametrize(("idx", "last_idx"), _stage_positions())
@pytest.mark.parametrize(
    ("consumes_stdout", "consumes_stderr"),
    list(itertools.product([False, True], repeat=2)),
)
def test_stage_stream_fds_full_domain(
    idx: int,
    last_idx: int,
    *,
    consumes_stdout: bool,
    consumes_stderr: bool,
) -> None:
    """Property: the canonical policy holds across the full input domain."""
    fds = _get_stage_stream_fds(
        idx,
        last_idx,
        consumes_stdout=consumes_stdout,
        consumes_stderr=consumes_stderr,
    )
    context = (
        f"idx={idx}, last_idx={last_idx}, consumes_stdout={consumes_stdout}, "
        f"consumes_stderr={consumes_stderr}"
    )

    assert fds.stdin == _expected_stdin(idx), f"stdin mismatch for {context}"
    assert fds.stdout == _expected_stdout(
        idx,
        last_idx,
        consumes_stdout=consumes_stdout,
    ), f"stdout mismatch for {context}"
    assert fds.stderr == _expected_stderr(consumes_stderr=consumes_stderr), (
        f"stderr mismatch for {context}"
    )


@pytest.mark.parametrize(
    ("capture", "echo"), list(itertools.product([False, True], repeat=2))
)
def test_final_stage_agrees_with_single_process_policy(
    *,
    capture: bool,
    echo: bool,
) -> None:
    """Example: the final pipeline stage matches the single-command policy.

    ``_spawn_subprocess`` selects ``PIPE`` for stdout and stderr exactly when
    ``capture or echo`` and ``DEVNULL`` otherwise; the final pipeline stage
    must agree on those overlapping cases.
    """
    consume = capture or echo
    single_process_flag = _PIPE if consume else _DEVNULL

    fds = _get_stage_stream_fds(
        0,
        0,
        consumes_stdout=consume,
        consumes_stderr=consume,
    )

    assert fds.stdout == single_process_flag, (
        f"final-stage stdout mismatch for capture={capture}, echo={echo}, "
        f"single_process_flag={single_process_flag!r}"
    )
    assert fds.stderr == single_process_flag, (
        f"final-stage stderr mismatch for capture={capture}, echo={echo}, "
        f"single_process_flag={single_process_flag!r}"
    )


def test_intermediate_stage_always_pipes_stdout() -> None:
    """Example: intermediate stages pipe stdout regardless of capture/echo."""
    for consume in (False, True):
        # Three-stage pipeline: stage 1 is intermediate between 0 and 2.
        fds = _get_stage_stream_fds(
            1,
            2,
            consumes_stdout=consume,
            consumes_stderr=consume,
        )
        assert fds.stdout == _PIPE, (
            f"intermediate-stage stdout mismatch for consumes={consume}"
        )


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        (None, None),
        ("/srv/data", "/srv/data"),
        (Path("/srv/data"), str(Path("/srv/data"))),
        (Path("relative/dir"), str(Path("relative/dir"))),
        ("", ""),
    ],
)
def test_cwd_arg_conversion(cwd: str | Path | None, expected: str | None) -> None:
    """Example: ``_cwd_arg`` renders optional working directories uniformly."""
    assert _cwd_arg(cwd) == expected, (
        f"cwd conversion mismatch for cwd={cwd!r}, expected={expected!r}"
    )
