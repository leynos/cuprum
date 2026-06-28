"""Shared test support for telemetry adapter unit tests."""

from __future__ import annotations

import sys
import threading
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _python_builder(
    *, project_name: str = "adapter-tests"
) -> tuple[cabc.Callable[..., sh.SafeCmd], ProgramCatalogue]:
    """Build a Python command builder and catalogue for adapter tests."""
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name=project_name,
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    return sh.make(python_program, catalogue=catalogue), catalogue


def _is_worker_thread(args: threading.ExceptHookArgs, prefix: str) -> bool:
    """Return whether exception-hook args came from one of our workers."""
    thread = args.thread
    return thread is not None and thread.name.startswith(prefix)


def _make_thread_exception_recorder(
    prefix: str,
    errors: list[BaseException],
    errors_lock: threading.Lock,
    fallback: cabc.Callable[[threading.ExceptHookArgs], None],
) -> cabc.Callable[[threading.ExceptHookArgs], None]:
    """Build an exception hook that records named worker failures."""

    def record_worker_exception(args: threading.ExceptHookArgs) -> None:
        """Record named worker failures and delegate unrelated failures."""
        exc = args.exc_value
        if exc is None:
            fallback(args)
            return
        if not _is_worker_thread(args, prefix):
            fallback(args)
            return
        with errors_lock:
            errors.append(exc)

    return record_worker_exception


def _run_in_threads(target: cabc.Callable[[], None], *, workers: int = 4) -> None:
    """Run a target callable in a fixed number of threads."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    join_timeout_s = 5.0
    thread_name_prefix = f"adapter-test-worker-{id(errors)}-"
    previous_excepthook = threading.excepthook

    def fallback_excepthook(args: threading.ExceptHookArgs) -> None:
        """Delegate unrelated worker failures to the previous hook."""
        previous_excepthook(args)

    record_thread_exception = _make_thread_exception_recorder(
        thread_name_prefix,
        errors,
        errors_lock,
        fallback_excepthook,
    )

    def run_target() -> None:
        """Run the target in a named worker thread."""
        target()

    threads = [
        threading.Thread(target=run_target, name=f"{thread_name_prefix}{index}")
        for index in range(workers)
    ]
    threading.excepthook = record_thread_exception
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=join_timeout_s)
    finally:
        threading.excepthook = previous_excepthook
    alive_threads = [thread for thread in threads if thread.is_alive()]
    if alive_threads:
        msg = f"{len(alive_threads)} worker thread(s) did not finish"
        raise TimeoutError(msg)
    if errors:
        raise errors[0]
