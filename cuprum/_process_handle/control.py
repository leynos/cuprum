"""Signal delivery and termination for an owned running command.

The operations here are ordered by how much they ask of the child.
:func:`_deliver_signal` asks for exactly what the caller named.
:func:`_terminate_shielded` asks for the cooperative stop: ``SIGTERM``, a
grace period, then ``SIGKILL`` if the child is still there.
:func:`_kill_shielded` skips the request entirely and compels the child to go.

Kill is a separate path rather than the escalation with a zero grace, because
the two answer different questions. Escalating with a zero grace still sends
``SIGTERM`` first, so a child that dies from it reports ``-15`` — the signal
that happened to end it, not the one the caller asked for. A caller who
chooses ``kill()`` has already decided the child is not to be asked, so the
forced signal is what is delivered, and ``-9`` is what the outcome reports.
Either way the child is reaped before the helper returns.

Signalling distinguishes two refusals, because the caller's remedy differs. A
child that has already exited is an ordinary racing outcome and is reported
with the exit code the handle recorded; a platform that cannot deliver a
caller-named signal is a limitation no retry can fix. Both carry the facts a
caller needs to choose what to do next, so they never have to re-read the
handle to find out what happened.

Every path here stops the process before the caller reconciles its stream
tasks, never after — that ordering lives with the caller, which owns the
reconciliation point. A consumer cancelled first would stop reading pipes the
child is still writing to, turning a clean shutdown into a broken pipe.
"""

from __future__ import annotations

import sys
import typing as typ

from cuprum._process_exit import _await_process_exit, _observed_exit_code
from cuprum._process_handle_errors import (
    SignalAfterExitError,
    UnsupportedSignalError,
    _arbitrary_signals_supported,
)
from cuprum._process_lifecycle import (
    _shielded_cleanup,
    _terminate_process,
    _TerminationOutcome,
)
from cuprum._process_signal import _escalate_after_grace, _SettlementTarget
from cuprum._subprocess_timeout import _get_exit_code
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_types import _StageObservation
    from cuprum._process_handle.tasks import _HandleRun

__all__ = [
    "_deliver_signal",
    "_kill_shielded",
    "_terminate_shielded",
]

_NO_STOP = _TerminationOutcome(
    delivered=False,
    escalated=False,
    waiter_completed=False,
)
"""What this module reports when a stop delivered nothing and waited for nothing.

Named rather than repeated positionally so the three facts stay readable at
the call site: a reader checking whether a stop escalated should not have to
count arguments to find the ``escalated`` slot.
"""

_KILLED_AND_REAPED = _TerminationOutcome(
    delivered=True,
    escalated=False,
    waiter_completed=True,
)
"""A forced signal that reached the child and whose exit this helper then waited for."""


def _deliver_signal(run: _HandleRun, signal_number: int) -> None:
    """Deliver one caller-named signal to the owned child.

    Synchronous because delivery is: ``send_signal`` hands the number to the
    operating system and returns, so there is nothing here to await. The
    handle's public ``signal`` is asynchronous all the same, so a caller never
    has to know which of its operations happen to block.

    The platform is checked before the child's state, so a caller on a
    platform that cannot deliver the signal at all learns that, rather than
    being told the child happened to be gone.

    The child's state has to be read through :func:`_observed_exit_code`
    rather than from ``returncode`` alone. A handle holds a spawned child
    whose status is published only when its owning reaper runs, and no reaper
    is running while the caller merely holds the handle: a child that exited
    on its own is a zombie with ``returncode`` still ``None``. Trusting that
    attribute would deliver a signal into a child that is already gone and
    report success for a delivery that no longer means anything — the exact
    race the handle exists to remove from the caller's plate.

    The probe and the delivery are both needed, because each covers a case the
    other cannot. The probe catches a child that exited *before* this call —
    a zombie whose status the owning reaper has not yet collected — and it is
    the only check that can report that child's ending while its status is
    still uncollected. The ``ProcessLookupError`` catches a child that was
    reaped *concurrently*: once someone collects the status, the kernel has no
    child at that PID, so the probe can no longer observe how it ended and
    ``os.kill`` is the call that discovers the process is gone. On the
    ``wait4`` backend the refusal may also arrive from the guard inside
    ``send_signal``, which asks the kernel that same question before it kills;
    either way it reaches this handler as the same error, and the child is
    gone. Both are ordinary racing outcomes rather than misuse, and both are
    reported the same way.

    Raises
    ------
    UnsupportedSignalError
        If the platform cannot deliver a caller-named signal.
    SignalAfterExitError
        If the child has already exited, carrying the exit code the handle
        recorded, or the best reading available when a concurrent reap
        collected the status first.
    """
    if not _arbitrary_signals_supported():
        raise UnsupportedSignalError(signal_number, sys.platform)
    process = run.process
    observed = _observed_exit_code(process)
    if observed is not None:
        raise SignalAfterExitError(observed, process.pid)
    try:
        process.send_signal(signal_number)
    except ProcessLookupError:
        # The child exited between the check above and the delivery, or was
        # reaped concurrently and so is no longer observable, so this is the
        # same outcome as finding it already gone. The probe is re-read rather
        # than reused: the exit happened after it ran, so this is the first
        # chance to learn how. It is compared against ``None`` and not used as
        # a truth value, because an exit code of zero is a real ending that a
        # falsy test would discard in favour of the ``-1`` placeholder. The
        # re-read still fails to see a status a concurrent reaper collected,
        # and only then does the published attribute stand in — a reaper that
        # collected the child has published its code by the time it returns.
        rechecked = _observed_exit_code(process)
        raise SignalAfterExitError(
            _get_exit_code(process) if rechecked is None else rechecked,
            process.pid,
        ) from None


def _settlement_target(process: asyncio.subprocess.Process) -> _SettlementTarget:
    """Return the direct child as this module's teardown target.

    The handle owns one child rather than a pipeline, so the target is always
    the same one: the child is done when its status is published, and waiting
    for it is the reap ``_await_process_exit`` performs. It is named once
    because both stop paths build it, and an ownership-aware teardown that
    disagreed with itself about what it was settling would be the defect the
    process-group policy exists to prevent.

    Returns
    -------
    _SettlementTarget
        The direct child as a target, done when its status is published and
        waited on through the same reap the exit wait performs.
    """
    return _SettlementTarget(
        is_done=lambda: process.returncode is not None,
        wait_for_exit=lambda: _await_process_exit(process),
    )


async def _terminate_shielded(
    process: asyncio.subprocess.Process,
    grace: float,
    *,
    owns_group: bool = False,
    observation: _StageObservation | None = None,
) -> _TerminationOutcome:
    """Escalate from ``SIGTERM`` to ``SIGKILL`` where cancellation cannot interrupt.

    The shield is what makes teardown a guarantee rather than a best effort: a
    cancellation arriving mid-grace — a task group tearing down the block that
    owns the handle, say — must not leave the child running with nobody
    holding it. The helper waits through the cancellation and the caller's
    cancellation is re-raised afterwards.

    *owns_group* forwards the policy the child was spawned under, so a handle
    started with ``OWN_GROUP`` stops its whole group rather than the leader
    alone. That distinction is the point of the policy: a descendant the child
    left behind is not reachable by any signal aimed at the leader, and it is
    exactly what holds the run's inherited pipes open. Defaulting to ``False``
    keeps every existing caller on the direct-child route it already had,
    rather than silently widening a stop that was never asked to be a group
    stop.

    *observation* correlates a target that could not be signalled with the run
    it belongs to, so the refusal is reported against that run's span and
    metric rather than as an unattributed event. It is optional because a
    handle is not always settling as part of an observed stage.

    The outcome is returned rather than discarded so an observing caller can
    tell a child that stopped when asked from one that had to be compelled.
    The shield does not swallow it: ``_shielded_cleanup`` re-raises the
    caller's cancellation after the stop completes, and that cancellation
    still wins over the outcome here.

    Returns
    -------
    _TerminationOutcome
        What the escalation did to the child, and how the wait for its exit
        ended.
    """
    policy = _TeardownPolicy(grace, owns_group=owns_group)
    return await _shielded_cleanup(
        _terminate_process(process, policy, observation=observation)
    )


async def _kill_shielded(
    process: asyncio.subprocess.Process,
    *,
    owns_group: bool = False,
    observation: _StageObservation | None = None,
) -> _TerminationOutcome:
    """Compel the child to exit, and reap it, where cancellation cannot interrupt.

    The delivery tolerates a child that died between the liveness check and the
    signal — the same race :func:`_deliver_signal` absorbs — and the wait still
    runs, because a child that exited just before the kill is a child that
    still has to be reaped. Only a process the transport no longer knows about
    short-circuits, and there is nothing left for the wait to observe in that
    case either.

    A forced stop never escalates: it *is* the forced signal, so there is no
    grace window for it to run out of. An outcome that had to report
    ``escalated`` here would describe a stop that sent ``SIGTERM`` first, which
    is the operation this one exists to avoid.

    An owning handle compels the whole group through
    :func:`~cuprum._process_signal._escalate_after_grace`, which asks the group
    again before signalling and waits for it afterwards. That is what makes
    ``kill()`` on an owning handle a group kill rather than a leader kill. An
    inherited-group handle keeps ``process.kill()``: it has no group to reach,
    so going through the group route would construct a target it would only
    ever resolve back to the same direct child.

    Returns
    -------
    _TerminationOutcome
        Whether the forced signal reached the child, whether a grace window was
        exhausted, and whether this caller waited for the exit.
    """
    if owns_group:
        delivered = await _shielded_cleanup(
            _escalate_after_grace(
                process,
                _settlement_target(process),
                observation=observation,
                owns_group=True,
            )
        )
        return _KILLED_AND_REAPED if delivered else _NO_STOP
    if process.returncode is not None:
        return _NO_STOP
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        # Gone already. Its exit code is recorded by whoever reaped it; this
        # helper only guarantees the child is not still running.
        return _NO_STOP
    await _shielded_cleanup(_await_process_exit(process))
    return _KILLED_AND_REAPED
