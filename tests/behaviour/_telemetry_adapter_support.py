"""Command-execution helpers for the telemetry adapter behaviour tests.

Extracted so the collected behaviour module stays within the project's
per-file line limit. The leading underscore keeps pytest from collecting
it as a test module.
"""

from __future__ import annotations

import typing as typ

from cuprum import sh
from cuprum.context import ScopeConfig, scoped

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook

_STDOUT_STDERR_SCRIPT = "\n".join(
    (
        "import sys",
        "print('stdout-line')",
        "print('stderr-line', file=sys.stderr)",
    ),
)

_SUCCESS_SCRIPT = "print('ok')"
_FAILURE_SCRIPT = "import sys; sys.exit(1)"
_OUTPUT_SCRIPT = "\n".join(
    (
        "import sys",
        "print('traced-output')",
        "print('traced-error', file=sys.stderr)",
    ),
)


def _execute_python_command(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    hook: ExecHook,
    script: str,
) -> None:
    """Execute a Python command with the given hook and store the result.

    This helper extracts catalogue and builder from python_cmd_fixture, builds
    a command with the given script, runs it inside scoped/observe contexts,
    and stores the result in behaviour_state.
    """
    catalogue = typ.cast("typ.Any", python_cmd_fixture["catalogue"])
    builder = typ.cast("typ.Any", python_cmd_fixture["builder"])
    cmd = builder("-c", script)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = cmd.run_sync()

    behaviour_state["result"] = result


def _run_command_with_hook(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    hook_fixture: dict[str, object],
    script: str,
) -> None:
    """Run a Python command with a hook extracted from a fixture.

    Parameters
    ----------
    behaviour_state:
        Shared mutable state dictionary.
    python_cmd_fixture:
        Python command builder fixture.
    hook_fixture:
        Fixture containing a hook under the "hook" key.
    script:
        Python script to execute with -c flag.

    """
    hook = typ.cast("ExecHook", hook_fixture["hook"])
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, script)
