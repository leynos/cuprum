"""Shared run-level setup for file-backed capture ownership tests.

The cleanup and retention suites exercise the same public ``SafeCmd.run``
boundary. Keeping their child commands, policy options, and spill assertions
here makes both suites prove their outcomes against the same run contract.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum.capture_policy import CapturedStream, FileBackedCapture
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.sh import SafeCmd
    from cuprum.unittests._capture_witness_support import _SpillWitness

# Longer than the spill threshold below, so a run that reaches the child's
# first flush has guaranteed the accumulator something to spill.
_PAYLOAD_BYTES = 512
_THRESHOLD = 8

_SPILL_THEN_EXIT = (
    f"import sys; sys.stdout.write('z' * {_PAYLOAD_BYTES}); "
    "sys.stdout.flush(); sys.exit({code})"
)
_SPILL_THEN_BLOCK = (
    f"import sys, time; sys.stdout.write('z' * {_PAYLOAD_BYTES}); "
    "sys.stdout.flush(); time.sleep(300)"
)
_UNDER_THRESHOLD = "import sys; sys.stdout.write('tiny'); sys.stdout.flush()"

# Long enough to let the child write before timing out, short enough that its
# 300-second sleep cannot finish first.
_RUN_TIMEOUT_S = 5.0
_WITNESS_TIMEOUT_S = 10.0


def _builder() -> cabc.Callable[..., SafeCmd]:
    """Return a builder for the current interpreter, allowlisted."""
    return build_python_catalogue_env().builder


def _options(directory: pathlib.Path, *, keep_file: bool = False) -> RunOutputOptions:
    """Capture stdout into *directory*'s spill files, and stderr not at all."""
    return RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=_THRESHOLD,
            directory=directory,
            keep_file=keep_file,
        ),
        capture_stderr=False,
    )


def _only_spill(captures: tuple[CapturedStream, ...]) -> CapturedStream:
    """Return the single capture record, asserting there is exactly one."""
    assert len(captures) == 1, (
        f"this case expects one captured stream, set by _options; got {captures!r}"
    )
    return captures[0]


def _reported_path(record: CapturedStream) -> pathlib.Path:
    """Return the path *record* names, asserting it names one at all."""
    assert record.storage == "file", (
        f"the record must report the stream as spilled; got {record.storage!r}"
    )
    assert record.path is not None, "a spilled record must name its file"
    return record.path


def _directory_is_clean(directory: pathlib.Path, *, outcome: str) -> None:
    """Assert *outcome* left no spill file behind."""
    remaining = list(directory.iterdir())
    assert not remaining, (
        f"{outcome} must leave no spill file behind; found {remaining}"
    )


def _cleanup_kept_spill(path: pathlib.Path) -> None:
    """Remove a kept spill as its test caller, then verify the hand-back."""
    path.unlink(missing_ok=True)
    assert not path.exists(), f"the test caller must remove its kept spill at {path}"


async def _await_witness(witness: _SpillWitness) -> None:
    """Fail unless *witness* sees a spill before the give-up deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WITNESS_TIMEOUT_S
    while loop.time() < deadline:
        if witness.snapshots:
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        f"the child never spilled before {_WITNESS_TIMEOUT_S}s; "
        f"events seen: {witness.categories}"
    )
