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
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_process_with_wait
from cuprum._subprocess_timeout import _get_exit_code

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._process_handle.tasks import _HandleRun

__all__ = [
    "_deliver_signal",
    "_kill_shielded",
    "_terminate_shielded",
]


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

    Raises
    ------
    UnsupportedSignalError
        If the platform cannot deliver a caller-named signal.
    SignalAfterExitError
        If the child has already exited, carrying its recorded exit code.
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
        # The child exited between the check above and the delivery, so this is
        # the same outcome as finding it already gone. The probe is re-read
        # rather than reused: the exit happened after it ran, so this is the
        # first chance to learn how. It is compared against ``None`` and not
        # used as a truth value, because an exit code of zero is a real ending
        # that a falsy test would discard in favour of the ``-1`` placeholder.
        rechecked = _observed_exit_code(process)
        raise SignalAfterExitError(
            _get_exit_code(process) if rechecked is None else rechecked,
            process.pid,
        ) from None


async def _terminate_shielded(
    process: asyncio.subprocess.Process,
    grace: float,
) -> None:
    """Escalate from ``SIGTERM`` to ``SIGKILL`` where cancellation cannot interrupt.

    The shield is what makes teardown a guarantee rather than a best effort: a
    cancellation arriving mid-grace — a task group tearing down the block that
    owns the handle, say — must not leave the child running with nobody
    holding it. The helper waits through the cancellation and the caller's
    cancellation is re-raised afterwards.
    """
    await _shielded_cleanup(
        _terminate_process_with_wait(
            process,
            grace_period=grace,
            is_done=lambda: process.returncode is not None,
            wait_for_exit=lambda: _await_process_exit(process),
        ),
    )


async def _kill_shielded(process: asyncio.subprocess.Process) -> None:
    """Compel the child to exit, and reap it, where cancellation cannot interrupt.

    The delivery tolerates a child that died between the liveness check and the
    signal — the same race :func:`_deliver_signal` absorbs — and the wait still
    runs, because a child that exited just before the kill is a child that
    still has to be reaped. Only a process the transport no longer knows about
    short-circuits, and there is nothing left for the wait to observe in that
    case either.
    """
    if process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        # Gone already. Its exit code is recorded by whoever reaped it; this
        # helper only guarantees the child is not still running.
        return
    await _shielded_cleanup(_await_process_exit(process))
