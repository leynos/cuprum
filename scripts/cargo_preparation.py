"""Describe one Cargo preparation and retain its run-local records.

The preparation entry points share two optional inputs: the caller environment
they inherit and the run-local file that receives diagnostics. Carrying both in
:class:`PreparationContext` keeps those call surfaces small, and
:func:`run_recorded_cargo` writes the record beside the phase timings that
:func:`record_phase` appends to the same run directory.
"""

from __future__ import annotations

import dataclasses as dc
import json
import time
import typing as typ

from scripts.cargo_diagnostics import (
    CargoDiagnostic,
    record_cargo_diagnostic,
    redact_environment_values,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import CommandResult

__all__ = (
    "CargoInvocation",
    "PreparationContext",
    "record_phase",
    "run_recorded_cargo",
)


def record_phase(timings_path: Path, phase: str, seconds: float) -> None:
    """Append a monotonic phase duration without recording environment data.

    Parameters
    ----------
    timings_path:
        Run-local JSON-lines file receiving the phase record.
    phase:
        Stable name for the measured phase.
    seconds:
        Elapsed monotonic duration in seconds.

    Raises
    ------
    OSError
        If the timing file cannot be opened or written.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from Path.open and write.
    with timings_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"phase": phase, "seconds": seconds}) + "\n")


@dc.dataclass(frozen=True, slots=True)
class PreparationContext:
    """Describe the caller context one Cargo preparation runs within.

    Attributes
    ----------
    caller_environment:
        Environment used to resolve caller cache paths and Cargo settings.
        ``None`` selects the current process environment.
    diagnostics_path:
        Run-local JSON-lines file that receives a redacted record of each
        Cargo invocation. ``None`` retains nothing.
    """

    caller_environment: cabc.Mapping[str, str] | None = None
    diagnostics_path: Path | None = None


@dc.dataclass(frozen=True, slots=True)
class CargoInvocation:
    """Name one Cargo invocation so every retained record carries context.

    Attributes
    ----------
    arguments:
        Arguments passed after the program name.
    working_directory:
        Directory the process runs in.
    environment:
        Environment the process receives.
    operation:
        Stable operation name recorded beside the captured streams.
    """

    arguments: tuple[str, ...]
    working_directory: Path
    environment: cabc.Mapping[str, str]
    operation: str


def run_recorded_cargo(
    invocation: CargoInvocation,
    diagnostics_path: Path | None,
    run: cabc.Callable[[tuple[str, ...], Path, cabc.Mapping[str, str]], CommandResult],
) -> CommandResult:
    """Run one invocation and append a redacted record when a sink is configured.

    The record is written for successful invocations too, so a later failure
    can be read alongside the preparation steps that preceded it.

    Parameters
    ----------
    invocation:
        Command, working directory, environment and operation name to run.
    diagnostics_path:
        Run-local JSON-lines sink, or ``None`` to keep nothing.
    run:
        Callable that executes the command. The caller supplies its own seam
        so tests can observe preparation without spawning Cargo.

    Returns
    -------
    CommandResult
        Whatever ``run`` returned, unchanged.
    """
    started = time.monotonic()
    result = run(
        invocation.arguments,
        invocation.working_directory,
        invocation.environment,
    )
    if diagnostics_path is not None:
        record_cargo_diagnostic(
            diagnostics_path,
            CargoDiagnostic(
                operation=invocation.operation,
                working_directory=str(invocation.working_directory),
                cargo_home=invocation.environment.get("CARGO_HOME"),
                exit_code=result.exit_code,
                elapsed_seconds=time.monotonic() - started,
                stdout=redact_environment_values(
                    result.stdout or "", invocation.environment
                ),
                stderr=redact_environment_values(
                    result.stderr or "", invocation.environment
                ),
            ),
        )
    return result
