"""Run bounded Cargo commands used while preparing shared cache snapshots."""

from __future__ import annotations

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
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult

#: End-to-end bound for one preparation command. Dependency preparation runs
#: before any timed workload, so this limit protects the run rather than a test
#: budget.
CARGO_TIMEOUT_SECONDS = 900


def cargo_program(environment: cabc.Mapping[str, str]) -> Program:
    """Return the Cargo executable named by an explicit environment mapping.

    Reading the mapping here, rather than at module import, keeps the ambient
    process environment out of the command boundary: a caller decides which
    Cargo runs.

    Returns
    -------
    Program
        The executable named by ``CARGO``, defaulting to ``cargo``.
    """
    return Program(environment.get("CARGO", "cargo"))


class CargoCommand:
    """Run one Cargo program under a scope that admits nothing else."""

    def __init__(self, program: Program) -> None:
        """Create the command and its single-program catalogue."""
        self._program = program
        self._command = sh.make(
            program,
            catalogue=ProgramCatalogue.from_programs(
                program,
                name="cargo-state-preparation",
                documentation_locations=("docs/developers-guide.md",),
            ),
        )

    def __call__(
        self,
        arguments: tuple[str, ...],
        working_directory: Path,
        environment: cabc.Mapping[str, str],
    ) -> CommandResult:
        """Run Cargo with a finite timeout and an owned process group."""
        context = ExecutionContext(
            cwd=working_directory,
            env=dict(environment),
            timeout=CARGO_TIMEOUT_SECONDS,
            process_group=ProcessGroupPolicy.OWN_GROUP,
        )
        with scoped(ScopeConfig(allowlist=frozenset({self._program}))):
            return self._command(*arguments).run_sync(context=context)


def run_cargo(
    arguments: tuple[str, ...],
    working_directory: Path,
    environment: cabc.Mapping[str, str],
) -> CommandResult:
    """Run the caller's Cargo program with a bounded, owned process group."""
    return CargoCommand(cargo_program(environment))(
        arguments, working_directory, environment
    )
