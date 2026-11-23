"""Run one pytest child, forward termination signals, and report how it ended.

The driver in `scripts/pytest_batches.py` compares how many processes the suite
uses, so the one thing it must not do is lose a child's outcome. Two details
matter more than they look.

A child killed by a signal has no exit status. `Popen.wait` reports ``-N``, and a
shell would report ``128+N``; the driver reports the shell form, because that is
what CI and a reader expect to see beside a failing step.

SIGINT and SIGTERM arriving here are forwarded to the child, which is then
reaped normally. Reaping rather than dying alongside it matters: the child's own
teardown releases its file handles and its Cargo locks, and a run that is
interrupted mid-build must not leave the next run waiting on a stale lock.
"""

from __future__ import annotations

import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - running pytest is the job.
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

__all__ = ("Outcome", "spawn")


class Outcome(typ.NamedTuple):
    """How a child process ended.

    Attributes
    ----------
    status : int
        Exit status, in shell form: ``128 + N`` when signal ``N`` killed it.
    signal_number : int or None
        The signal that killed the child, or ``None`` for an ordinary exit.
    interrupted : bool
        Whether this process received a termination signal while the child ran,
        so the measurement is partial rather than finished.
    """

    status: int
    signal_number: int | None
    interrupted: bool


def spawn(argv: cabc.Sequence[str], log_path: pth.Path | None) -> Outcome:
    """Run a child and report how it ended.

    Parameters
    ----------
    argv : Sequence of str
        The full argument vector to run.
    log_path : pathlib.Path or None
        Where to capture combined output, or ``None`` to inherit this process's
        streams so an interactive run streams exactly as it always did.

    Returns
    -------
    Outcome
        The child's status, any signal that killed it, and whether this process
        was signalled while waiting.

    Notes
    -----
    Output is captured to a file rather than a pipe. A pipe that nobody drains
    would block a chatty child once the buffer filled, which is the classic way
    a measurement harness deadlocks the run it is measuring; a file never fills.
    """
    if log_path is None:
        return _run(argv, None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as handle:
        return _run(argv, handle)


def _run(argv: cabc.Sequence[str], handle: typ.IO[bytes] | None) -> Outcome:
    """Start the child with ``handle`` as its output and wait for it.

    The child is managed by a `with` block so its streams are closed and it is
    reaped even when a signal interrupts the wait. Reaping is the point: the
    child's own teardown releases its file handles and build locks, and a
    driver that died alongside it would leave the next run waiting on both.

    Parameters
    ----------
    argv : Sequence of str
        The full argument vector to run.
    handle : IO of bytes or None
        Capture file, or ``None`` to inherit this process's streams.

    Returns
    -------
    Outcome
        The child's status, any signal that killed it, and whether this process
        was signalled while waiting.
    """
    interrupted = False
    previous: dict[signal.Signals, typ.Any] = {}
    # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
    with subprocess.Popen(
        list(argv),
        stdout=handle,
        stderr=subprocess.STDOUT if handle is not None else None,
    ) as child:

        def forward(signum: int, _frame: object) -> None:
            """Pass a termination signal on to the child."""
            nonlocal interrupted
            interrupted = True
            child.send_signal(signum)

        previous = {
            number: signal.signal(number, forward)
            for number in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            returncode = child.wait()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    if returncode < 0:
        return Outcome(128 - returncode, -returncode, interrupted)
    return Outcome(returncode, None, interrupted)
