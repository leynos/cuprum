"""Readiness-independent workers for execution-timeout behaviour coverage."""

from __future__ import annotations

import typing as typ

from cuprum import sh
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from tests.behaviour._execution_runtime_support import WorkerCommand


def given_unpublished_pid_worker(tmp_path: Path) -> WorkerCommand:
    """Build a worker which deliberately never creates its PID file.

    Returns
    -------
    WorkerCommand
        A blocking command and a path which remains absent.
    """
    catalogue, program = python_catalogue()
    command = sh.make(program, catalogue=catalogue)(
        "-c", "import time; time.sleep(300)"
    )
    return {"command": command, "pid_file": tmp_path / "unpublished.pid"}
