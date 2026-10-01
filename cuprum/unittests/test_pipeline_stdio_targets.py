"""Pipeline stdio-target rejection, mirroring ``SafeCmd``'s own rules.

``RunOutputOptions`` carries ``stdin``/``stdout``/``stderr`` targets wherever
it is accepted, but only a single-command run honours them: a pipeline's
stdio policy lives on the capture/echo/sink fields, and the stage wiring that
would carry a target to the right stage does not exist.

A pipeline that accepted a target would be worse than one that refused it. The
run would succeed, the exit code would be ``0``, and the file the caller named
would never be created — the exact silent loss ``SafeCmd``'s own validation
exists to prevent. These cases pin the refusal, so acceptance cannot come back
without someone first deciding what a pipeline target should *mean*.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ECHO, Program, ScopeConfig, scoped, sh
from cuprum.sh import (
    Pipeline,
    PipelineResult,
    RunOutputOptions,
    StdioTarget,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path


def _identity_pipeline() -> Pipeline:
    """Build a two-stage pipeline that forwards stdin to stdout."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)
    return echo("-n", "echoed") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )


def _allowlist() -> frozenset[Program]:
    """Build the set of programs the pipeline's stages need admitted.

    Returns
    -------
    frozenset[Program]
        The programs ``ECHO`` and the current interpreter, admitted.
    """
    _, python_program = python_catalogue()
    return frozenset([ECHO, python_program])


def _run_pipeline(
    *,
    output: RunOutputOptions,
    sync: bool = False,
) -> PipelineResult:
    """Run the identity pipeline under a scope admitting its own programs."""
    pipeline = _identity_pipeline()
    with scoped(ScopeConfig(allowlist=_allowlist())):
        if sync:
            return pipeline.run_sync(output=output)
        return asyncio.run(pipeline.run(output=output))


# One case per stream, each carrying its own keyword name rather than a
# lookup keyed by that name. The keyword differs per row because
# ``RunOutputOptions`` takes three separate parameters and building the
# argument by unpacking a string-keyed mapping would ask the type checker to
# bind every parameter at once. The variants differ in kind on purpose: the
# guard must key on "a target was named", not on one variant being special.
_NAMED_TARGET_CASES = (
    pytest.param("stdin", StdioTarget.pipe(), id="stdin"),
    pytest.param("stdout", StdioTarget.inherit(), id="stdout"),
    pytest.param("stderr", StdioTarget.inherit(), id="stderr"),
)


def test_a_stdout_path_target_is_refused_rather_than_ignored(
    tmp_path: Path,
) -> None:
    """A target a pipeline cannot honour is refused, not silently dropped.

    This is the shape that used to run: the pipeline succeeded, exited ``0``,
    and the caller's log file was never created. The file assertion is the
    point — an exit code alone cannot distinguish "redirected" from "ignored".
    """
    log = tmp_path / "pipeline.log"

    with pytest.raises(ValueError, match="does not support"):
        _run_pipeline(
            output=RunOutputOptions(capture=False, stdout=StdioTarget.path(log)),
        )

    assert not log.exists(), (
        "a refused run must not have created the target file; the caller acts "
        "on the refusal, and a half-written log would hide it"
    )


def _options_with(stream: str, target: StdioTarget) -> RunOutputOptions:
    """Build options that name *target* for *stream*, capture off.

    ``capture=False`` is what lets each case reach the *pipeline* guard. With
    capture on, a redirected stdout or stderr would trip
    ``RunOutputOptions``' own pre-existing capture/redirect rule first, and a
    test asserting only that the run was refused would pass without the
    pipeline guard existing at all.

    Returns
    -------
    RunOutputOptions
        The options carrying *target* on the stream named by *stream*.
    """
    match stream:
        case "stdin":
            return RunOutputOptions(capture=False, stdin=target)
        case "stdout":
            return RunOutputOptions(capture=False, stdout=target)
        case _:
            return RunOutputOptions(capture=False, stderr=target)


@pytest.mark.parametrize(("stream", "target"), _NAMED_TARGET_CASES)
def test_every_stream_is_refused_and_named(
    stream: str,
    target: StdioTarget,
) -> None:
    """All three streams are refused, and the message names the one given."""
    with pytest.raises(ValueError, match=f"does not support.*{stream}"):
        _run_pipeline(output=_options_with(stream, target))


def test_run_sync_refuses_a_named_target(tmp_path: Path) -> None:
    """``run_sync`` refuses the same combination through its shared entry point.

    Asserted separately because the sync path is a distinct public entry point;
    a guard placed only inside the async coroutine would leave this accepted.
    """
    with pytest.raises(ValueError, match="does not support"):
        _run_pipeline(
            output=RunOutputOptions(
                capture=False,
                stderr=StdioTarget.path(tmp_path / "err.log"),
            ),
            sync=True,
        )


def test_a_pipeline_without_targets_still_runs() -> None:
    """The negative control: the guard must not refuse an ordinary pipeline.

    Without this case the refusals above would be consistent with a guard that
    rejected every pipeline run, which is a different and much worse defect.
    """
    result = _run_pipeline(output=RunOutputOptions(capture=True))

    assert result.ok is True, "an ordinary pipeline must succeed"
    assert result.stdout == "echoed", (
        "the pipeline must still capture the final stage's output"
    )


def test_a_single_command_still_honours_the_same_target(tmp_path: Path) -> None:
    """The guard is scoped to pipelines, not to stdio targets as a whole.

    ``SafeCmd`` is where a target is meaningful, so the refusal must not leak
    into it. This is the paired positive case for the refusal above, and it is
    what keeps that assertion from being satisfied by rejecting targets
    everywhere.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    log = tmp_path / "single.log"
    command = python("-c", "print('redirected')")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = asyncio.run(
            command.run(
                output=RunOutputOptions(capture=False, stdout=StdioTarget.path(log))
            )
        )

    assert result.exit_code == 0, "a single command should honour its target"
    assert "redirected" in log.read_text(), (
        "the single-command path is what must keep working; a guard that broke "
        "it would be a regression, not a fix"
    )
