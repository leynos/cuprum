"""One real three-stage pipeline whose first stage fails, and its events.

Two modules read what the observe hooks receive from a genuine fail-fast run:
`test_pipeline_fail_fast_wiring` checks that a run publishes the event at all,
and `test_pipeline_fail_fast_tag_shadowing` checks that what it publishes is
the coordinator's own reckoning rather than the caller's tags. Both need the
same three subprocesses and the same care over completion batching, so the run
lives here rather than in either of them.
"""

from __future__ import annotations

import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import ExecutionContext

# Downstream stages linger briefly after their stdin closes so they cannot
# settle in the same ``asyncio.wait`` batch as the failing stage; see
# `run_failing_pipeline`.
_SETTLE_DELAY_S = 0.2
_READ_STDIN = (
    "import sys, time; sys.stdout.write(sys.stdin.read()); "
    f"time.sleep({_SETTLE_DELAY_S})"
)


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
    therefore sleep for ``_SETTLE_DELAY_S`` after their stdin closes, which puts
    them in a later batch than the failing stage.

    ``context`` is passed to the run unchanged, which is how a caller injects
    the execution tags whose shadowing is under test.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    events: list[ExecEvent] = []

    pipeline = (
        python("-c", "import sys; sys.exit(3)")
        | python("-c", _READ_STDIN)
        | python("-c", _READ_STDIN)
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
    ):
        pipeline.run_sync(
            output=RunOutputOptions(capture=True, echo=False),
            context=context,
        )

    return tuple(events)


def phase(events: cabc.Sequence[ExecEvent], name: str) -> list[ExecEvent]:
    """Return the events of one phase, in the order they were published."""
    return [event for event in events if event.phase == name]
