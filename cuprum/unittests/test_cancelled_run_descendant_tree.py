"""An externally cancelled run whose child is alive beside a pipe-holding descendant.

This is the shape issue #720 reported against cuprum 0.1.0: a caller bounds a
run with ``asyncio.wait_for``, the direct child is still running at the
deadline, and a descendant it started has inherited the run's stdout and
stderr. Signalling the direct child alone leaves the descendant holding the
write ends of both pipes, so a teardown that waited for end-of-file blocked for
as long as the descendant lived, and the descendant outlived the call.

Two separate promises answer that, and each test arm isolates one of them:

- Under ``OWN_GROUP`` the teardown signals the whole group, so the descendant
  is reclaimed — escalated to ``SIGKILL`` after ``cancel_grace``, because the
  descendant here ignores ``SIGTERM``.
- Under ``INHERIT`` no group signal exists to close the pipes, so the call's
  prompt return can only come from the drain being bounded. The descendant is
  asserted to survive, which is what proves the bound and not a signal ended
  the wait.

The sibling ``test_stream_run_owned_group_cancellation`` covers the other
process tree, where the leader has already exited before the cancellation
lands. The runs here configure no ``timeout``: the deadline is the caller's.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

import pytest

from cuprum._process_group import _POST_KILL_SETTLEMENT_S
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._post_exit_teardown_support import (
    RUN_BUDGET_S,
    owned_builder,
    reap_descendant,
)
from tests.helpers.timeouts import (
    pipe_holding_child_argv,
    process_is_running,
    wait_for_pid_file,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

# The fixture is imported rather than defined here; declaring it records that
# pytest resolves the name at collection time rather than leaving it to be
# pruned as an unused import.
__all__ = ["owned_builder"]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

_CALLER_DEADLINE_S = 0.3
_CANCEL_GRACE_S = 0.2
# What a loaded host may add on top of the documented allowances before the
# return stops being "bounded". It is deliberately far below the descendant's
# 300 s lifetime, which is how long an unbounded drain would hold the call.
_SCHEDULING_MARGIN_S = 5.0
_RETURN_BOUND_S = (
    _CALLER_DEADLINE_S
    + _CANCEL_GRACE_S
    + _POST_KILL_SETTLEMENT_S
    + _SCHEDULING_MARGIN_S
)


async def _cancel_at_caller_deadline(
    command: SafeCmd,
    policy: ProcessGroupPolicy,
    pid_file: Path,
) -> tuple[int, float]:
    """Run *command* past the caller's deadline; return the descendant and delay.

    The deadline clock starts only once the descendant has announced itself, so
    the cancellation always lands on the tree under test rather than on a child
    that has not yet handed its pipes on.

    Returns
    -------
    tuple[int, float]
        The descendant's pid, and the seconds the caller's ``wait_for`` took to
        return once the deadline clock had started.
    """
    run_task = asyncio.ensure_future(
        command.run(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(
                process_group=policy,
                cancel_grace=_CANCEL_GRACE_S,
            ),
        ),
    )
    descendant_pid = await asyncio.to_thread(
        wait_for_pid_file,
        pid_file,
        context="the descendant holding the cancelled run's pipes",
    )
    started = time.monotonic()
    # ``wait_for`` awaits the task it cancelled, so it is itself watched from
    # outside: a teardown that never settles is then reported as the defect it
    # is rather than as the test runner's own timeout. ``asyncio.run`` cancels
    # whatever such a failure leaves pending.
    bounded_call = asyncio.ensure_future(
        asyncio.wait_for(run_task, _CALLER_DEADLINE_S),
    )
    done, _ = await asyncio.wait([bounded_call], timeout=RUN_BUDGET_S)
    elapsed = time.monotonic() - started
    if not done:
        pytest.fail(
            f"the cancelled run had not returned after {RUN_BUDGET_S}s: its "
            "teardown is waiting on pipes the descendant still holds open"
        )
    error = bounded_call.exception()
    assert isinstance(error, TimeoutError), (
        "the caller's deadline must surface as the TimeoutError that "
        f"asyncio.wait_for raises, but the call ended with {error!r}"
    )
    return descendant_pid, elapsed


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param(ProcessGroupPolicy.INHERIT, id="inherit"),
        pytest.param(ProcessGroupPolicy.OWN_GROUP, id="own-group"),
    ],
)
def test_cancelled_run_returns_while_a_descendant_holds_its_pipes(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    policy: ProcessGroupPolicy,
) -> None:
    """A cancelled run returns within its cleanup bound under either policy.

    The descendant sleeps for minutes and ignores ``SIGTERM``, so nothing but a
    bounded drain (``INHERIT``) or a group ``SIGKILL`` (``OWN_GROUP``) lets the
    call return before the bound asserted here.
    """
    pid_file = tmp_path / f"cancelled-tree-{policy.value}.pid"
    command = owned_builder(*pipe_holding_child_argv(pid_file))

    async def run_case() -> None:
        """Cancel the run at the caller's deadline and check both promises."""
        descendant_pid: int | None = None
        try:
            descendant_pid, elapsed = await _cancel_at_caller_deadline(
                command, policy, pid_file
            )
            assert elapsed < _RETURN_BOUND_S, (
                "a cancelled run must not wait on pipes a descendant holds "
                f"open: returned after {elapsed:.3f}s, bound {_RETURN_BOUND_S:.3f}s"
            )
            if policy is ProcessGroupPolicy.OWN_GROUP:
                await asyncio.to_thread(
                    wait_for_process_death,
                    descendant_pid,
                    context="a cancelled owned run's group teardown",
                )
            is_running = process_is_running(descendant_pid)
            assert is_running is (policy is ProcessGroupPolicy.INHERIT), (
                f"under {policy.value} the pipe-holding descendant must "
                f"{'survive' if policy is ProcessGroupPolicy.INHERIT else 'die'}"
                f" a cancelled run, but running={is_running}"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
