"""Registration and emission for per-stream capture outcome events.

Capture hooks live on their own :class:`~contextvars.ContextVar`, mirroring
:mod:`cuprum.echo_observation` and :mod:`cuprum.pump_observation`: adding the
channel cannot alter how an existing caller's commands execute, because a
caller opts in by registering a hook and a caller that does not pays nothing.
Registration follows the repository's token-restoration discipline, so a
capture registration nests and detaches like every other one.

See :mod:`cuprum.capture_events` for the event types this channel carries.
"""

from __future__ import annotations

import inspect
import logging
import typing as typ
from contextvars import ContextVar

if typ.TYPE_CHECKING:
    from contextvars import Token

    from cuprum.capture_events import CaptureEvent, CaptureHook

_LOGGER = logging.getLogger(__name__)

_capture_hooks: ContextVar[tuple[CaptureHook, ...]] = ContextVar(
    "cuprum_capture_hooks",
    default=(),
)


class CaptureHookRegistration:
    """Registration handle for a per-stream capture observation hook.

    Supports ``detach()`` and context-manager use. The handle captures a
    :class:`~contextvars.Token` when it installs the extended hook tuple, and
    :meth:`detach` restores the exact tuple that was current when the handle
    was created — the same discipline
    :class:`cuprum.echo_observation.EchoHookRegistration` applies. Detach in
    the :class:`~contextvars.Context` that created the registration; resetting
    a ``ContextVar`` with a token from another context raises
    :class:`ValueError`.

    Prefer ``with`` blocks, which detach in last-in-first-out order; detaching
    out of order restores a tuple that discards later registrations.
    """

    __slots__ = ("_detached", "_hook", "_token")

    def __init__(self, hook: CaptureHook) -> None:
        """Append ``hook`` to the current context's capture hooks."""
        self._hook = hook
        self._detached = False
        self._token: Token[tuple[CaptureHook, ...]] = _capture_hooks.set((
            *_capture_hooks.get(),
            hook,
        ))

    def detach(self) -> None:
        """Restore the capture hooks that preceded this registration."""
        if self._detached:
            return
        _capture_hooks.reset(self._token)
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the context manager; the hook is already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach the registration on scope exit."""
        self.detach()


def observe_capture(hook: CaptureHook) -> CaptureHookRegistration:
    """Register a hook for capture outcome events in the current context.

    Parameters
    ----------
    hook:
        Synchronous callable invoked with each
        :class:`~cuprum.capture_events.CaptureEvent`.

    Returns
    -------
    CaptureHookRegistration
        A handle that can be detached or used as a context manager.

    Examples
    --------
    Count the bytes a bounded capture discards::

        events: list[CaptureEvent] = []
        with observe_capture(events.append):
            result = cmd.run_sync(
                output=RunOutputOptions(
                    capture_stdout=BoundedCapture(max_bytes=1024),
                ),
            )
        assert events[0].category is CaptureEventCategory.TRUNCATED
    """
    return CaptureHookRegistration(hook)


def current_capture_hooks() -> tuple[CaptureHook, ...]:
    """Return the capture hooks registered in the current context.

    Returns
    -------
    tuple[CaptureHook, ...]
        The registered hooks in registration order, empty when none are
        registered.
    """
    return _capture_hooks.get()


def _emit_capture_event(event: CaptureEvent) -> None:
    """Deliver ``event`` to every registered capture hook.

    Hook-failure policy
    -------------------
    A hook that raises :class:`Exception` is reported at ``WARNING`` with its
    traceback and the remaining hooks still run, mirroring
    :func:`cuprum.echo_observation._emit_echo_event`. Both emission sites sit on
    paths whose contract is that they must complete: a bounded capture's
    truncation and a spill file's transition both occur inside a drain that is
    still reading the child, and propagating would let registering a metrics
    backend turn a run that would have captured its output into one that fails.
    The failure is recorded, not swallowed.

    Anything that is not an :class:`Exception` — ``SystemExit``,
    ``KeyboardInterrupt``, ``asyncio.CancelledError`` — propagates untouched.

    With no hooks registered this returns before touching the event, so a
    caller that has not opted in pays nothing and behaves exactly as before.
    """
    hooks = _capture_hooks.get()
    if not hooks:
        return
    for hook in hooks:
        _invoke_capture_hook(hook, event)


def _invoke_capture_hook(hook: CaptureHook, event: CaptureEvent) -> None:
    """Invoke one capture hook, reporting rather than propagating failure."""
    try:
        result = hook(event)
    except Exception as exc:
        _LOGGER.warning(
            "capture_observer_failed stream=%s error=%s",
            event.stream,
            type(exc).__name__,
            exc_info=True,
            extra={
                "cuprum_action": "capture_observer_failed",
                "cuprum_stream": str(event.stream),
                "cuprum_error_type": type(exc).__name__,
            },
        )
        return
    if result is not None:
        _discard_capture_hook_result(result, event)


def _discard_capture_hook_result(result: object, event: CaptureEvent) -> None:
    """Report a non-``None`` hook return, closing coroutines it left behind.

    Capture hooks are synchronous by contract. An unawaited coroutine would
    otherwise surface later as an unrelated ``coroutine was never awaited``
    warning, so it is closed here and reported against the event that produced
    it.
    """
    _LOGGER.warning(
        "capture_observer_returned_value stream=%s result_type=%s",
        event.stream,
        type(result).__name__,
        extra={
            "cuprum_action": "capture_observer_returned_value",
            "cuprum_stream": str(event.stream),
            "cuprum_result_type": type(result).__name__,
        },
    )
    if inspect.iscoroutine(result):
        result.close()


__all__ = [
    "CaptureHookRegistration",
    "current_capture_hooks",
    "observe_capture",
]
