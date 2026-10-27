"""Run bounded Cargo commands used while preparing shared cache snapshots."""

from __future__ import annotations

import os
import typing as typ

from cuprum import (
    ExecutionContext,
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    scoped,
    sh,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult

_CARGO = Program(os.environ.get("CARGO", "cargo"))
_CATALOGUE = ProgramCatalogue.from_programs(
    _CARGO,
    name="cargo-state-preparation",
    documentation_locations=("docs/developers-guide.md",),
)
_CARGO_COMMAND = sh.make(_CARGO, catalogue=_CATALOGUE)
_CARGO_TIMEOUT_SECONDS = 900


def _run_cargo(
    arguments: tuple[str, ...],
    working_directory: Path,
    environment: dict[str, str],
) -> CommandResult:
    """Run Cargo with a finite timeout and terminate its owned process group."""
    context = ExecutionContext(
        cwd=working_directory,
        env=environment,
        timeout=_CARGO_TIMEOUT_SECONDS,
        process_group=ProcessGroupPolicy.OWN_GROUP,
    )
    with scoped(ScopeConfig(allowlist=frozenset({_CARGO}))):
        return _CARGO_COMMAND(*arguments).run_sync(context=context)
