"""A pipeline's spill files outlive neither its result nor its failure.

The pipeline owns more capture lifetime than a single run does: the final
stage's stdout is the stream a caller receives, and a run that raises never
returns that stream at all. This module witnesses a spill on the returning
path and on the after-hook failure path, and asserts the spill directory is
empty afterwards.

The after-hook case is the one that motivated the module. That failure is
raised from inside the step that runs once all output has been collected, so a
pipeline that removed its files only where it builds a result would leave them
behind with nothing left to name them.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, scoped
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import Pipeline, RunOutputOptions, SafeCmd
from tests.helpers.catalogue import PythonCatalogue, build_python_catalogue_env

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum import AfterHook
    from cuprum.sh import BytesCommandResult, CommandResult

# Comfortably longer than the spill threshold, so a stage that reaches its
# first flush has guaranteed the accumulator something to spill.
_PAYLOAD_BYTES = 512
_THRESHOLD = 8

_SPILL_THEN_EXIT = (
    f"import sys; sys.stdout.write('z' * {_PAYLOAD_BYTES}); sys.stdout.flush()"
)
# Named rather than inlined because this stage is what makes the pipeline a
# pipeline: the final stage's own capture policy is the one the run reports, so
# the first stage's output has to reach it.
_COPY_STDIN = "import sys; sys.stdout.write(sys.stdin.read())"


class _AfterHookError(Exception):
    """Raised by the failing pipeline after-hook in this module."""


class _SpillWitness:
    """Record every capture event the run emits."""

    def __init__(self) -> None:
        """Start with no events seen."""
        self.categories: list[CaptureEventCategory] = []

    def __call__(self, event: CaptureEvent) -> None:
        """Record one capture event's category."""
        self.categories.append(event.category)

    def spilled(self) -> None:
        """Assert at least one stream really spilled."""
        assert CaptureEventCategory.SPILLED_TO_FILE in self.categories, (
            "the run never spilled, so the removal assertion below would hold "
            f"for the wrong reason; events seen: {self.categories}"
        )


def _options(directory: pathlib.Path) -> RunOutputOptions:
    """Capture the final stage's stdout into *directory*'s spill files.

    Only the final stage's stdout is reported to the caller, so that is the
    stream whose ownership this run takes on; stderr is left uncaptured, which
    keeps the case about one file rather than about counting them.

    Returns
    -------
    RunOutputOptions
        Output options whose stdout policy spills into *directory*.
    """
    return RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=_THRESHOLD,
            directory=directory,
        ),
        capture_stderr=False,
    )


def _pipeline(env: PythonCatalogue) -> Pipeline:
    """Return a two-stage pipeline running this interpreter twice.

    The first stage writes more than the spill threshold and the second copies
    its input through, so the run both spills and exercises a real inter-stage
    stream. Both stages use ``env.program``, which is what lets one allowlist
    entry admit the whole pipeline.

    Returns
    -------
    Pipeline
        The two-stage pipeline, ready to run under *env*'s allowlist.
    """
    first = env.builder("-c", _SPILL_THEN_EXIT)
    second = env.builder("-c", _COPY_STDIN)
    return first | second


def _scope(
    env: PythonCatalogue,
    *,
    after_hooks: tuple[AfterHook, ...] = (),
) -> ScopeConfig:
    """Return the scope that admits *env*'s program and installs *after_hooks*.

    Returns
    -------
    ScopeConfig
        A scope allowlisting the interpreter and carrying *after_hooks*.
    """
    return ScopeConfig(
        allowlist=frozenset([env.program]),
        after_hooks=after_hooks,
    )


def _assert_directory_clean(directory: pathlib.Path, *, outcome: str) -> None:
    """Assert *outcome* left no spill file behind."""
    remaining = sorted(entry.name for entry in directory.iterdir())
    assert not remaining, (
        f"{outcome} must leave no spill file behind; found {remaining}"
    )


def test_a_pipeline_removes_its_spill_file_before_returning_the_result(
    tmp_path: pathlib.Path,
) -> None:
    """A completed pipeline hands back no file, because it has removed it.

    The witness is what makes this a statement about a spill that happened
    rather than about one that never did: without it the empty directory would
    also be satisfied by a policy that silently never spilled.
    """
    env = build_python_catalogue_env()
    witness = _SpillWitness()

    with observe_capture(witness), scoped(_scope(env)):
        result = _pipeline(env).run_sync(output=_options(tmp_path))

    witness.spilled()
    assert result.stdout is None, (
        "a file-backed capture reports no text on the result, so the payload "
        "must be absent rather than a reading of the file"
    )
    _assert_directory_clean(tmp_path, outcome="a completed pipeline")


def test_a_failing_after_hook_still_removes_the_pipeline_spill_file(
    tmp_path: pathlib.Path,
) -> None:
    """A run that raises owes the same removal as one that returns.

    The after-hook runs after every stage has finished and its output has been
    collected, so this is the exit where the cleanup has the least state left
    to fall back on. The run is expected to raise: the case is only meaningful
    if the hook really failed the run.
    """

    def failing_after_hook(
        _cmd: SafeCmd,
        _result: CommandResult | BytesCommandResult,
    ) -> None:
        """Fail once the pipeline reaches its after-hooks."""
        raise _AfterHookError

    env = build_python_catalogue_env()
    witness = _SpillWitness()

    scope = _scope(env, after_hooks=(failing_after_hook,))
    with (
        observe_capture(witness),
        scoped(scope),
        pytest.raises(_AfterHookError),
    ):
        _pipeline(env).run_sync(output=_options(tmp_path))

    witness.spilled()
    _assert_directory_clean(tmp_path, outcome="a pipeline whose after-hook failed")
