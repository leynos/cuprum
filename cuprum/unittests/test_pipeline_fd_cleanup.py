"""Descriptor-cleanup tests for the public ``Pipeline.run_sync`` entry point.

The Rust inter-stage pump borrows the raw pipe descriptors from asyncio for the
duration of a transfer, then restores their blocking mode, resumes the reader
transport, and closes the writer. The fault-injection suites drive those
partial-failure paths behind private seams; this module checks the property
they exist to protect, through the public API and real subprocesses. Repeated
runs of the same pipeline must transfer their payload intact and leave the
process holding no more descriptors than it started with, whichever backend
pumps the hops.
"""

from __future__ import annotations

import pathlib
import sys
import typing as typ

import pytest

from cuprum import ECHO, ScopeConfig, scoped, sh
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum import Program
    from cuprum.sh import Pipeline

_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="counting open descriptors requires /proc/self/fd",
)

_PAYLOAD = "descriptor-cleanup-payload"
_RUN_COUNT = 3


def _open_fd_count() -> int:
    """Count the descriptors this process currently holds open."""
    return sum(1 for _ in pathlib.Path("/proc/self/fd").iterdir())


def _upper_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a three-stage pipeline that upper-cases its payload.

    Three stages give two inter-stage hops, so a descriptor mishandled on
    either side of a hop shows up in the count.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    echo = sh.make(ECHO)
    pipeline = (
        echo("-n", _PAYLOAD)
        | python("-c", "import sys; sys.stdout.write(sys.stdin.read())")
        | python("-c", "import sys; sys.stdout.write(sys.stdin.read().upper())")
    )
    return pipeline, frozenset([ECHO, python_program])


@_linux_only
@pytest.mark.usefixtures("stream_backend")
def test_repeated_run_sync_transfers_payload_and_leaks_no_descriptors() -> None:
    """Repeated public runs transfer intact and return every descriptor.

    A leak here is cumulative rather than absolute, so the count is compared
    against a warmed-up baseline over several runs: a hop that returned one
    descriptor too few would drift upwards run by run, whereas a one-off
    allocation that legitimately outlives the first run would not.
    """
    pipeline, allowlist = _upper_pipeline()
    expected = _PAYLOAD.upper()

    with scoped(ScopeConfig(allowlist=allowlist)):
        # The first run opens event-loop and subprocess machinery that
        # legitimately stays open, which would otherwise read as a leak.
        warmup = pipeline.run_sync()
        assert warmup.stdout == expected, (
            f"the pipeline must transfer its payload intact; got {warmup.stdout!r}"
        )

        baseline = _open_fd_count()
        for run_index in range(_RUN_COUNT):
            result = pipeline.run_sync()
            assert result.stdout == expected, (
                f"run {run_index} must transfer its payload intact; "
                f"got {result.stdout!r}"
            )
            assert _open_fd_count() == baseline, (
                f"run {run_index} left descriptors open; "
                f"expected {baseline}, now {_open_fd_count()}"
            )
