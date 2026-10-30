"""Scenario scaffolding for the opt-in exit-status guard (#671).

Each scenario runs a generated probe script under a real pipe. The probe is a
cuprum adopter: it relays an allowlisted child with capture, stdout echo, and
``BrokenPipePolicy.BEST_EFFORT``, prints once more after the run returns, and
exits with the child's status. The child writes a readiness line, waits for a
marker file, then floods stdout and exits with a requested status, so the
scenario controls exactly when the reader leaves.

The probe is generated rather than committed so the guard is present in one
variant and absent in the control without two near-identical scripts drifting
apart.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

_LINE_COUNT = 2000
_READY_LINE = "ready"

# The child cannot outlive the scenario's patience; a missed marker is a
# harness bug rather than something to wait out.
_MARKER_DEADLINE_SECONDS = 10.0

_CHILD_TEMPLATE = """\
import sys
import time
from pathlib import Path

sys.stdout.write({ready!r} + chr(10))
sys.stdout.flush()
marker = Path(sys.argv[1])
deadline = time.monotonic() + {deadline}
while not marker.exists():
    if time.monotonic() > deadline:
        raise SystemExit(97)
    time.sleep(0.01)
for index in range({lines}):
    print("line", index)
raise SystemExit({status})
"""

_GUARD_BLOCK = """\
import cuprum

cuprum.install_exit_status_guard()
"""

_PROBE_TEMPLATE = """\
from __future__ import annotations

import sys

{guard}
from cuprum import (
    BrokenPipePolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    RunOutputOptions,
    ScopeConfig,
    scoped,
    sh,
)

_CHILD = {child!r}

python = Program(sys.executable)
catalogue = ProgramCatalogue.from_project(
    ProjectSettings(name="exit-status-probe", programs=(python,))
)
with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
    result = sh.make(python, catalogue=catalogue)(
        "-c", _CHILD, sys.argv[1]
    ).run_sync(
        output=RunOutputOptions(
            capture=True,
            echo_stdout=True,
            broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
        ),
    )
print("probe finished")
raise SystemExit(result.exit_code)
"""

# Generous against a healthy probe and short enough that a hung child is
# reported as a failed scenario within the suite's own timeout.
PROBE_WAIT_SECONDS = 20.0

# The child prints this once it is polling for the marker.
READY_LINE = _READY_LINE


def probe_source(*, install_guard: bool, child_status: int) -> str:
    """Return the generated probe script for one scenario.

    Parameters
    ----------
    install_guard : bool
        Whether the probe arms :func:`cuprum.install_exit_status_guard` before
        running the child.
    child_status : int
        The status the child exits with, which the probe must preserve.

    Returns
    -------
    str
        The complete Python source of the probe.
    """
    child = _CHILD_TEMPLATE.format(
        ready=_READY_LINE,
        deadline=_MARKER_DEADLINE_SECONDS,
        lines=_LINE_COUNT,
        status=child_status,
    )
    guard = _GUARD_BLOCK if install_guard else ""
    return _PROBE_TEMPLATE.format(guard=guard, child=child)


def write_probe(
    directory: Path,
    *,
    install_guard: bool,
    child_status: int,
) -> Path:
    """Write the generated probe into *directory* and return its path."""
    path = directory / "probe.py"
    path.write_text(
        probe_source(install_guard=install_guard, child_status=child_status),
        encoding="utf-8",
    )
    return path
