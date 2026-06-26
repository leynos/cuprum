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
    threads = [threading.Thread(target=target) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
