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


def _run_in_threads(target: cabc.Callable[[], None], *, workers: int = 4) -> None:
    """Run a target callable in a fixed number of threads."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    join_timeout_s = 5.0
    thread_name_prefix = f"adapter-test-worker-{id(errors)}-"

    def run_target() -> None:
        """Run the target and preserve failures for the main thread."""
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - surface worker failures.
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(
            target=run_target,
            name=f"{thread_name_prefix}{index}",
            daemon=True,
        )
        for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=join_timeout_s)
    alive_threads = [thread for thread in threads if thread.is_alive()]
    if alive_threads:
        msg = f"{len(alive_threads)} worker thread(s) did not finish"
        raise TimeoutError(msg)
    if errors:
        raise errors[0]
