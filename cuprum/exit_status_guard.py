"""Preserve the interpreter exit status across a closed output pipe.

CPython runs every ``atexit`` callback and then flushes ``sys.stdout`` and
``sys.stderr`` while finalizing. If that final flush fails because the process
at the other end of the pipe has gone away, ``Py_FinalizeEx`` reports failure
and the interpreter exits with status 120 instead of the status the
application passed to ``sys.exit``. The failure happens after every Python
callback has run, so it cannot be observed or handled when it occurs; a
process that cares about its exit status has to arm a guard beforehand.

:func:`install_exit_status_guard` is that guard. It registers one ``atexit``
hook which flushes each standard stream and, when the flush reports a closed
reader, points the stream's descriptor at the null device so finalization's
own flush succeeds. A stream with no descriptor is replaced with a sink whose
writes and flush cannot fail.

The helper is opt-in and has no import-time side effect: an application that
never calls it behaves exactly as before. It belongs at a command-line entry
point rather than in a library or a long-running service, because only the
former chooses its own exit status.

Notes
-----
The hook treats only ``BrokenPipeError`` as "the reader is gone". A stream the
application closed deliberately raises ``ValueError``, which is a different
defect and is left alone.

Writes the application makes while running are not covered. Cuprum's own echo
path is covered separately by
:attr:`~cuprum.echo_events.BrokenPipePolicy.BEST_EFFORT`; an application's own
``print`` calls, a stream object captured elsewhere such as an import-time
``rich.Console``, interpreters whose shutdown semantics differ, and processes
that call ``os._exit`` all remain the application's concern.

Examples
--------
Install the guard once, before producing any output, then exit with the status
the run decided::

    import cuprum

    cuprum.install_exit_status_guard()
    ...
    raise SystemExit(status)

"""

from __future__ import annotations

import atexit
import contextlib
import io
import os
import sys
import threading
import typing as typ

__all__ = ["install_exit_status_guard"]

# The streams CPython flushes during finalization. Order matters only for
# readability: the hook treats each one independently.
_STANDARD_STREAM_NAMES: typ.Final = ("stdout", "stderr")

# One registration per process, and the flag is set only after
# ``atexit.register`` succeeds, so a failed registration stays retryable.
_install_lock = threading.Lock()
_is_installed = False


def install_exit_status_guard() -> None:
    """Install a process-level guard that preserves the exit status.

    Registers one ``atexit`` hook which redirects a standard stream whose
    reader has gone away, so the interpreter's final flush cannot replace the
    application's exit status with 120. Repeated calls are idempotent.

    Notes
    -----
    Call this once at a command-line entry point, before writing any output.
    ``atexit`` runs handlers in reverse registration order, so a handler
    registered afterwards runs before the guard and receives no protection for
    a write it flushes itself; such a handler must handle ``BrokenPipeError``.
    The guard protects only output still buffered when its callback runs, and
    a handler holding a reference to a descriptorless stream object cannot be
    protected at all.

    This is not part of :class:`cuprum.echo_events.BrokenPipePolicy`. That
    policy is per run and covers cuprum's echo path; this concern is per
    process and only matters during finalization.

    Examples
    --------
    >>> import cuprum
    >>> cuprum.install_exit_status_guard()
    """
    global _is_installed
    with _install_lock:
        if _is_installed:
            return
        atexit.register(_guard_standard_streams)
        _is_installed = True


def _guard_standard_streams() -> None:
    """Redirect every standard stream whose reader has gone away."""
    for name in _STANDARD_STREAM_NAMES:
        # One stream's failure must not cost the other its guard, and the hook
        # runs during finalization where an escaping exception changes the
        # exit status this helper exists to protect.
        with contextlib.suppress(Exception):
            _guard_stream(name, getattr(sys, name))


def _guard_stream(name: str, stream: typ.TextIO | None) -> None:
    """Redirect *stream* if and only if flushing it finds a closed reader."""
    if stream is None:
        return
    try:
        stream.flush()
    except BrokenPipeError:
        _redirect_stream(name, stream)


def _redirect_stream(name: str, stream: typ.TextIO) -> None:
    """Point *stream*'s descriptor at the null device, or replace the stream.

    A stream with no usable descriptor cannot be redirected, and CPython will
    still flush it during finalization, so it is replaced with a sink that
    cannot fail. A fallback replacement keeps the same promise when opening or
    duplicating the null device fails.
    """
    try:
        descriptor = stream.fileno()
    except OSError:
        setattr(sys, name, _NullTextSink())
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_descriptor, descriptor)
        finally:
            os.close(null_descriptor)
    except OSError:
        setattr(sys, name, _NullTextSink())


class _NullTextSink(io.TextIOBase):
    """A text sink whose writes and flush cannot fail.

    CPython flushes the standard streams during finalization even after the
    application has replaced them, so a dead descriptorless stream needs a
    stand-in that cannot raise a second time.
    """

    @typ.override
    def writable(self) -> bool:
        """Return ``True``: the sink accepts text."""
        return True

    @typ.override
    def write(self, text: str) -> int:
        """Discard *text* and report it as written."""
        return len(text)

    @typ.override
    def flush(self) -> None:
        """Discard buffered output; there is none to write."""
