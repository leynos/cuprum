"""Logging hooks for emitting structured start and exit events."""

from __future__ import annotations

import dataclasses as dc
import logging
import threading
import time
import typing as typ
from weakref import WeakKeyDictionary

from cuprum.context import HookRegistration, after, before

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, SafeCmd


@dc.dataclass(slots=True)
class LoggingHookRegistration:
    """Registration handle for paired logging hooks.

    Detaches both the start (before) and exit (after) hooks together to avoid
    leaking state across tests or calling code. Detach order is reversed from
    registration to respect ContextVar token stacking.
    """

    start_registration: HookRegistration | None
    exit_registration: HookRegistration | None
    _detached: bool = False

    def detach(self) -> None:
        """Detach both logging hooks idempotently."""
        if self._detached:
            return
        # Detach in reverse registration order to satisfy ContextVar token use.
        if self.exit_registration is not None:
            self.exit_registration.detach()
        if self.start_registration is not None:
            self.start_registration.detach()
        self._detached = True
        self.exit_registration = None  # type: ignore[assignment]
        self.start_registration = None  # type: ignore[assignment]

    def __enter__(self) -> LoggingHookRegistration:
        """Return self to support context manager usage."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach hooks when leaving a context manager block."""
        self.detach()


def logging_hook(
    *,
    logger: logging.Logger | None = None,
    start_level: int = logging.INFO,
    exit_level: int = logging.INFO,
) -> LoggingHookRegistration:
    """Register paired hooks that log start and exit events.

    Parameters
    ----------
    logger:
        Logger instance to emit records to. Defaults to ``logging.getLogger(
        "cuprum")`` when omitted.
    start_level:
        Logging level for the start event (default ``logging.INFO``).
    exit_level:
        Logging level for the exit event (default ``logging.INFO``).

    Returns
    -------
    LoggingHookRegistration
        Handle for detaching the hooks manually or via context manager usage.

    """
    logger = logger or logging.getLogger("cuprum")
    on_start, on_exit = _build_logging_hooks(
        logger=logger,
        start_level=start_level,
        exit_level=exit_level,
    )
    start_registration = before(on_start)
    exit_registration = after(on_exit)
    return LoggingHookRegistration(start_registration, exit_registration)


def _build_logging_hooks(
    *,
    logger: logging.Logger,
    start_level: int,
    exit_level: int,
) -> tuple[
    typ.Callable[[SafeCmd], None],
    typ.Callable[[SafeCmd, CommandResult], None],
]:
    """Create before/after hooks that log start and exit events."""
    start_times: WeakKeyDictionary[SafeCmd, float] = WeakKeyDictionary()
    lock = threading.Lock()

    def on_start(cmd: SafeCmd) -> None:
        started_at = time.perf_counter()
        with lock:
            start_times[cmd] = started_at
        if logger.isEnabledFor(start_level):
            logger.log(
                start_level,
                "cuprum.start program=%s argv=%r",
                cmd.program,
                cmd.argv_with_program,
            )

    def on_exit(cmd: SafeCmd, result: CommandResult) -> None:
        if not logger.isEnabledFor(exit_level):
            return
        with lock:
            started_at = start_times.pop(cmd, None)
        duration_str = (
            f"{time.perf_counter() - started_at:.6f}"
            if started_at is not None
            else "unknown"
        )
        logger.log(
            exit_level,
            (
                "cuprum.exit program=%s pid=%s exit_code=%s duration_s=%s "
                "stdout_len=%s stderr_len=%s"
            ),
            result.program,
            result.pid,
            result.exit_code,
            duration_str,
            len(result.stdout) if result.stdout is not None else 0,
            len(result.stderr) if result.stderr is not None else 0,
        )

    return on_start, on_exit


__all__ = ["LoggingHookRegistration", "logging_hook"]
