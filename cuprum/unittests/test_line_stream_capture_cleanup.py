"""A ``lines()`` run removes the spill files its result still claims it owns.

``SafeCmd.lines()`` reaches the same drain results ``run()`` does, but through
its own coordinator, so it owes the same removal. Without it a file-backed
capture would hand back a record reporting ``owned=True`` for a file that
outlives the run, and nothing would be left holding the path to remove it.

The witness is what makes the assertion about a spill that happened rather than
about a policy that silently never spilled: an empty directory is also what a
run that never spilled would leave behind.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import ScopeConfig, scoped
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import RunOutputOptions, SafeCmd
from cuprum.unittests._capture_witness_support import _SpillWitness
from tests.helpers.catalogue import PythonCatalogue, build_python_catalogue_env

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum.sh import CommandResult

# Comfortably longer than the spill threshold, so the accumulator spills rather
# than finalizing from the heap.
_PAYLOAD_BYTES = 512
_THRESHOLD = 8

_ONE_LINE_THEN_BULK = (
    f"import sys; print('first', flush=True); "
    f"sys.stdout.write('z' * {_PAYLOAD_BYTES}); sys.stdout.flush()"
)


def _options(directory: pathlib.Path) -> RunOutputOptions:
    """Capture stdout into *directory*'s spill files, leaving stderr uncaptured.

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


def _scope(env: PythonCatalogue) -> ScopeConfig:
    """Return the scope that admits *env*'s program.

    Returns
    -------
    ScopeConfig
        A scope allowlisting the interpreter.
    """
    return ScopeConfig(allowlist=frozenset([env.program]))


def _iterate(command: SafeCmd, directory: pathlib.Path) -> list[str]:
    """Iterate *command*'s lines to completion and return their text.

    Returns
    -------
    list[str]
        The text of every line event, in arrival order.
    """

    async def collect() -> list[str]:
        """Drive the line stream to its sentinel."""
        stream = command.lines(output=_options(directory))
        return [event.text async for event in stream]

    return asyncio.run(collect())


def test_lines_remove_their_spill_file_before_returning_the_result(
    tmp_path: pathlib.Path,
) -> None:
    """A completed ``lines()`` run leaves no file behind.

    Iteration reads one line before the bulk of the payload arrives, so the
    case witnesses the spill on the same path a caller actually uses: the
    accumulator crosses its threshold mid-iteration, and the result is
    assembled after the sentinel has been consumed.
    """
    env = build_python_catalogue_env()
    witness = _SpillWitness()
    command = env.builder("-c", _ONE_LINE_THEN_BULK)

    with observe_capture(witness), scoped(_scope(env)):
        lines = _iterate(command, tmp_path)

    witness.spilled()
    assert lines, "the case needs a line the iterator actually delivered"
    remaining = sorted(entry.name for entry in tmp_path.iterdir())
    assert not remaining, (
        f"a completed lines() run must leave no spill file behind; found {remaining}"
    )


def test_lines_record_reports_the_spill_as_already_removed(
    tmp_path: pathlib.Path,
) -> None:
    """The capture record names a path that has already been unlinked.

    This is the observable form of the removal: the record has to say what
    happened to the file, and the file's absence is what a caller can check.
    A record reporting ``owned=True`` for a file still on disk would mean the
    run had forgotten its own cleanup.
    """
    env = build_python_catalogue_env()
    command = env.builder("-c", _ONE_LINE_THEN_BULK)

    async def collect() -> CommandResult | None:
        """Iterate to completion and return the stream's result."""
        stream = command.lines(output=_options(tmp_path))
        async for _event in stream:
            pass
        return stream.result

    with scoped(_scope(env)):
        result = asyncio.run(collect())

    assert result is not None, "a completed line stream must expose its result"
    assert len(result.captures) == 1, (
        "only stdout was captured, so the result reports exactly one record; "
        f"got {result.captures!r}"
    )
    stdout_capture = result.captures[0]
    assert stdout_capture.storage == "file", (
        "the payload crossed the threshold, so the capture had to spill"
    )
    assert stdout_capture.path is not None, "a spilled capture names its file"
    assert not stdout_capture.path.exists(), (
        "the run must remove its own spill file before handing it back; "
        f"found {stdout_capture.path}"
    )
