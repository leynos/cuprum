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


def _expected_stdout(idx: int, last_idx: int, *, capture_or_echo: bool) -> int:
    """Return the expected stdout FD flag for stage *idx*."""
    if idx != last_idx:
        return _PIPE
    return _PIPE if capture_or_echo else _DEVNULL


def _expected_stderr(*, capture_or_echo: bool) -> int:
    """Return the expected stderr FD flag for any stage."""
    return _PIPE if capture_or_echo else _DEVNULL


def _stage_positions() -> list[tuple[int, int]]:
    """Enumerate every (idx, last_idx) pair up to the coverage bound."""
    return [
        (idx, last_idx)
        for last_idx in range(_MAX_LAST_IDX + 1)
        for idx in range(last_idx + 1)
    ]


@pytest.mark.parametrize(("idx", "last_idx"), _stage_positions())
@pytest.mark.parametrize("capture_or_echo", [False, True])
def test_stage_stream_fds_full_domain(
    idx: int,
    last_idx: int,
    *,
    capture_or_echo: bool,
) -> None:
    """Property: the canonical policy holds across the full input domain."""
    fds = _get_stage_stream_fds(idx, last_idx, capture_or_echo=capture_or_echo)

    assert fds.stdin == _expected_stdin(idx)
    assert fds.stdout == _expected_stdout(
        idx,
        last_idx,
        capture_or_echo=capture_or_echo,
    )
    assert fds.stderr == _expected_stderr(capture_or_echo=capture_or_echo)


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
    capture_or_echo = capture or echo
    single_process_flag = _PIPE if capture_or_echo else _DEVNULL

    fds = _get_stage_stream_fds(0, 0, capture_or_echo=capture_or_echo)

    assert fds.stdout == single_process_flag
    assert fds.stderr == single_process_flag


def test_intermediate_stage_always_pipes_stdout() -> None:
    """Example: intermediate stages pipe stdout regardless of capture/echo."""
    for capture_or_echo in (False, True):
        # Three-stage pipeline: stage 1 is intermediate between 0 and 2.
        fds = _get_stage_stream_fds(1, 2, capture_or_echo=capture_or_echo)
        assert fds.stdout == _PIPE


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
    assert _cwd_arg(cwd) == expected
