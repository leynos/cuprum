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
    import collections.abc as cabc

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecHook
    from cuprum.sh import SafeCmd


class PythonCommandFixture(typ.TypedDict):
    """Typed catalogue and command builder supplied by the BDD fixture.

    Attributes
    ----------
    catalogue : ProgramCatalogue
        Catalogue whose allowlist authorizes the Python command.
    builder : cabc.Callable[..., SafeCmd]
        Callable that constructs the allowlisted Python command.
    """

    catalogue: ProgramCatalogue
    builder: cabc.Callable[..., SafeCmd]


class HookFixture(typ.TypedDict):
    """Typed execution hook supplied by an adapter BDD fixture.

    Attributes
    ----------
    hook : ExecHook
        Execution hook installed while the fixture command runs.
    """

    hook: ExecHook


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
    python_cmd_fixture: PythonCommandFixture,
    hook: ExecHook,
    script: str,
) -> None:
    """Execute a Python command with the given hook and store the result."""
    catalogue = python_cmd_fixture["catalogue"]
    builder = python_cmd_fixture["builder"]
    cmd = builder("-c", script)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = cmd.run_sync()

    behaviour_state["result"] = result


def _run_command_with_hook(
    behaviour_state: dict[str, object],
    python_cmd_fixture: PythonCommandFixture,
    hook_fixture: HookFixture,
    script: str,
) -> None:
    """Run a Python command with a hook extracted from a fixture."""
    hook = hook_fixture["hook"]
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, script)
