"""One real three-stage pipeline whose first stage fails, and its events.

Two modules read what the observe hooks receive from a genuine fail-fast run:
`test_pipeline_fail_fast_wiring` checks that a run publishes the event at all,
and `test_pipeline_fail_fast_tag_shadowing` checks that what it publishes is
the coordinator's own reckoning rather than the caller's tags. Both need the
same three subprocesses and the same care over completion batching, so the run
lives here rather than in either of them.
"""

from __future__ import annotations

import os
import tempfile
import typing as typ
from pathlib import Path

from cuprum import ScopeConfig, bind_executable, scoped, sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.program import Program
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import ExecutionContext

_READ_STDIN = """import select
import sys

sys.stdout.write(sys.stdin.read())
sys.stdout.flush()
with open(sys.argv[1], "rb", buffering=0) as gate:
    ready, _, _ = select.select([gate], [], [], 5.0)
if not ready:
    raise SystemExit("fail-fast event was not observed")
"""


def run_failing_pipeline(
    context: ExecutionContext | None = None,
) -> tuple[ExecEvent, ...]:
    """Run a three-stage pipeline whose first stage fails, capturing events.

    The run has to reach the fail-fast decision, and that is not free.
    ``asyncio.wait(..., FIRST_COMPLETED)`` returns every task that settled
    before the waiter was resumed, not just the first, so three stages that all
    exit promptly can arrive in one batch. Stage 0 is then processed after its
    siblings have already finished; nothing is left to terminate, and the run
    latches a failure index without emitting an event. The downstream stages
    therefore block after their stdin closes until the observing hook has
    received the fail-fast event. Each downstream stage waits on its own FIFO,
    whose five-second ``select`` timeout is a watchdog: a regression fails the
    test rather than hanging it.

    ``context`` is passed to the run unchanged, which is how a caller injects
    the execution tags whose shadowing is under test.

    Parameters
    ----------
    context : ExecutionContext | None
        Context passed through unchanged to ``Pipeline.run_sync``.

    Returns
    -------
    tuple[ExecEvent, ...]
        Events published by the pipeline, in publication order.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    events: list[ExecEvent] = []

    with tempfile.TemporaryDirectory() as directory:
        gates = [Path(directory) / f"stage-{index}.gate" for index in range(2)]
        for gate in gates:
            os.mkfifo(gate)
        gate_fds = [os.open(gate, os.O_RDWR | os.O_NONBLOCK) for gate in gates]

        def observe(event: ExecEvent) -> None:
            """Capture an event and release the downstream fail-fast gates."""
            events.append(event)
            if event.phase == "pipeline_fail_fast":
                for gate_fd in gate_fds:
                    os.write(gate_fd, b"1")

        pipeline = (
            python("-c", "import sys; sys.exit(3)")
            | python("-c", _READ_STDIN, str(gates[0]))
            | python("-c", _READ_STDIN, str(gates[1]))
        )

        try:
            with (
                scoped(ScopeConfig(allowlist=frozenset([python_program]))),
                sh.observe(observe),
            ):
                pipeline.run_sync(
                    output=RunOutputOptions(capture=True, echo=False),
                    context=context,
                )
        finally:
            for gate_fd in gate_fds:
                os.close(gate_fd)

    return tuple(events)


def run_bound_failing_pipeline(
    failing_executable: Path,
) -> tuple[ExecEvent, ...]:
    """Run a pipeline whose *bound*, distinct first stage fails, capturing events.

    This exists because a binding is keyed by logical program, and the ordinary
    failing pipeline runs all three stages under one interpreter program. A
    binding for that program would therefore bind every stage: the downstream
    stages would run the failing script too, all three would settle in one
    batch, and the run would never emit a fail-fast decision at all. Probing
    that arrangement is what showed the mistake — the events contained no
    ``pipeline_fail_fast`` phase, only three ``exit 3`` events.

    The failing stage is consequently given a program of its own, which is the
    only stage bound. The two downstream stages stay on the catalogued
    interpreter and keep the FIFO gate that makes the decision reachable.

    Parameters
    ----------
    failing_executable : Path
        Executable the first stage is bound to. It must exit non-zero so the
        pipeline latches this stage as the failure.

    Returns
    -------
    tuple[ExecEvent, ...]
        Events published by the pipeline, in publication order.
    """
    catalogue, python_program = python_catalogue()
    failing_program = Program("bound-failing-stage")
    catalogue = ProgramCatalogue.from_programs(
        python_program,
        failing_program,
        name="fail-fast-binding-tests",
    )
    python = sh.make(python_program, catalogue=catalogue)
    failing = sh.make(failing_program, catalogue=catalogue)
    events: list[ExecEvent] = []

    with tempfile.TemporaryDirectory() as directory:
        gates = [Path(directory) / f"stage-{index}.gate" for index in range(2)]
        for gate in gates:
            os.mkfifo(gate)
        gate_fds = [os.open(gate, os.O_RDWR | os.O_NONBLOCK) for gate in gates]

        def observe(event: ExecEvent) -> None:
            """Capture an event and release the downstream fail-fast gates."""
            events.append(event)
            if event.phase == "pipeline_fail_fast":
                for gate_fd in gate_fds:
                    os.write(gate_fd, b"1")

        pipeline = (
            failing()
            | python("-c", _READ_STDIN, str(gates[0]))
            | python("-c", _READ_STDIN, str(gates[1]))
        )

        try:
            with (
                scoped(
                    ScopeConfig(
                        allowlist=frozenset([python_program, failing_program]),
                    )
                ),
                bind_executable(failing_program, str(failing_executable)),
                sh.observe(observe),
            ):
                pipeline.run_sync(output=RunOutputOptions(capture=True, echo=False))
        finally:
            for gate_fd in gate_fds:
                os.close(gate_fd)

    return tuple(events)


def phase(events: cabc.Sequence[ExecEvent], name: str) -> list[ExecEvent]:
    """Return the events of one phase, in the order they were published.

    Parameters
    ----------
    events : Sequence[ExecEvent]
        Published events to filter.
    name : str
        Phase name to select.

    Returns
    -------
    list[ExecEvent]
        Matching events in their original publication order.
    """
    return [event for event in events if event.phase == name]
