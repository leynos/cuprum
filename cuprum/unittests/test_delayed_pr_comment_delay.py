"""Behaviour of ``delayed-pr-comment.yml``'s minutes-to-seconds conversion.

The checked-in step runs under Bash with a scratch ``GITHUB_OUTPUT``, so the
assertions cover what a dispatch actually executes: whole numbers up to the
180-minute cap convert, leading zeros included, and anything else fails
before any arithmetic can wrap and before any output is written.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest

from tests.helpers.ci_workflows import jobs
from tests.helpers.release_workflow import outputs, run_bash, step_script

if typ.TYPE_CHECKING:
    import pathlib as pth

_WORKFLOW = "delayed-pr-comment.yml"
_JOB = "delay_and_comment"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The workflow's steps run under Bash on Linux.",
)


def _convert(tmp_path: pth.Path, minutes: str) -> tuple[int, str, dict[str, str]]:
    """Run the conversion step; return its status, stderr, and outputs."""
    github_output = tmp_path / "github-output"
    completed = run_bash(
        step_script(_JOB, "Convert minutes to seconds", workflow=_WORKFLOW),
        tmp_path,
        {"DELAY_MINUTES": minutes, "GITHUB_OUTPUT": str(github_output)},
    )
    return completed.returncode, completed.stderr, outputs(github_output)


@pytest.mark.parametrize(
    ("minutes", "seconds"),
    [("0", "0"), ("1", "60"), ("007", "420"), ("180", "10800"), ("000", "0")],
)
def test_a_bounded_whole_number_converts(
    tmp_path: pth.Path, minutes: str, seconds: str
) -> None:
    """Whole numbers up to the cap convert, and leading zeros stay decimal."""
    status, stderr, written = _convert(tmp_path, minutes)

    assert status == 0, stderr
    assert written == {"secs": seconds}


@pytest.mark.parametrize(
    "minutes",
    ["181", "0181", "1000", "307445734561825861", "-1", "1.5", "1;id", "$(id)", ""],
    ids=[
        "over-cap",
        "padded-over-cap",
        "four-digits",
        "wraps-to-44",
        "negative",
        "fraction",
        "command-separator",
        "substitution",
        "empty",
    ],
)
def test_anything_else_fails_without_output(tmp_path: pth.Path, minutes: str) -> None:
    """Out-of-range or non-numeric input stops the job and writes nothing."""
    status, _, written = _convert(tmp_path, minutes)

    assert status != 0, f"{minutes!r} must be refused"
    assert written == {}, "a refused delay must not reach the sleep step"


def test_the_job_outlives_the_longest_accepted_sleep() -> None:
    """The cap's sleep must finish inside the job timeout, leaving a margin."""
    timeout = typ.cast("dict[str, object]", jobs(_WORKFLOW)[_JOB])["timeout-minutes"]

    assert timeout == 190, "the job needs ten minutes beyond the 180-minute cap"
