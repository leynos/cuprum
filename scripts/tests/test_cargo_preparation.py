"""Exercise the preparation context and the bounded Cargo command boundary."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import json
import typing as typ
from pathlib import Path

from cuprum import ExecutionContext, ProcessGroupPolicy, Program, ScopeConfig
from cuprum.sh import CommandResult
from scripts import cargo_state_command
from scripts.cargo_preparation import (
    CargoInvocation,
    PreparationContext,
    record_phase,
    run_recorded_cargo,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum import ProgramCatalogue


def _result(*, exit_code: int, stdout: str = "", stderr: str = "") -> CommandResult:
    """Build the Cargo result a stubbed boundary would return."""
    return CommandResult(Program("cargo"), (), exit_code, -1, stdout, stderr)


def _invocation(
    working_directory: Path,
    *,
    environment: dict[str, str],
) -> CargoInvocation:
    """Build one recorded fetch invocation for the given environment."""
    return CargoInvocation(
        arguments=("fetch", "--locked"),
        working_directory=working_directory,
        environment=environment,
        operation="cargo fetch --locked",
    )


def _records(diagnostics: Path) -> list[dict[str, str | float | int]]:
    """Parse the retained JSON-lines file."""
    return [
        json.loads(line)
        for line in diagnostics.read_text(encoding="utf-8").splitlines()
    ]


def test_recorded_cargo_returns_the_result_and_retains_redacted_context(
    tmp_path: Path,
) -> None:
    """The record names the invocation and hides secret environment values."""
    result = _result(exit_code=3, stdout="token=ghs-super-secret", stderr="error")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    environment = {
        "CARGO_HOME": str(tmp_path / "run-home"),
        "GH_TOKEN": "ghs-super-secret",
    }

    def run(
        arguments: tuple[str, ...],
        working_directory: Path,
        child_environment: cabc.Mapping[str, str],
    ) -> CommandResult:
        """Record the command the boundary would have executed."""
        calls.append((arguments, working_directory, dict(child_environment)))
        return result

    diagnostics = tmp_path / "cargo-diagnostics.jsonl"
    returned = run_recorded_cargo(
        _invocation(tmp_path, environment=environment),
        diagnostics,
        run,
    )

    assert returned is result, "the caller must receive Cargo's own result"
    assert calls == [(("fetch", "--locked"), tmp_path, environment)], (
        "the invocation must reach the runner unchanged"
    )
    [record] = _records(diagnostics)
    assert record["operation"] == "cargo fetch --locked"
    assert record["working_directory"] == str(tmp_path)
    assert record["cargo_home"] == str(tmp_path / "run-home")
    assert record["exit_code"] == 3
    assert record["stdout"] == "token=<redacted>", (
        "the retained stream must not carry the caller's credential"
    )
    assert record["stderr"] == "error"
    assert float(record["elapsed_seconds"]) >= 0, "the record must carry a duration"


def test_recorded_cargo_writes_nothing_without_a_sink(tmp_path: Path) -> None:
    """Omitting the diagnostics path keeps the run's filesystem untouched."""
    diagnostics = tmp_path / "cargo-diagnostics.jsonl"

    returned = run_recorded_cargo(
        _invocation(tmp_path, environment={}),
        None,
        lambda _arguments, _cwd, _environment: _result(exit_code=0),
    )

    assert returned.exit_code == 0
    assert not diagnostics.exists(), "no sink means no retained file"


def test_record_phase_appends_one_line_per_measured_phase(tmp_path: Path) -> None:
    """Phase timings accumulate in the run's JSON-lines file."""
    timings = tmp_path / "timings.jsonl"

    record_phase(timings, "dependency_preparation", 1.25)
    record_phase(timings, "workload", 2.5)

    assert [
        json.loads(line) for line in timings.read_text(encoding="utf-8").splitlines()
    ] == [
        {"phase": "dependency_preparation", "seconds": 1.25},
        {"phase": "workload", "seconds": 2.5},
    ]


def test_preparation_context_defaults_to_the_process_environment() -> None:
    """An unspecified context asks preparation to inherit the process environment."""
    context = PreparationContext()

    assert context.caller_environment is None
    assert context.diagnostics_path is None


def test_cargo_program_reads_the_supplied_environment() -> None:
    """The Cargo executable comes from the caller's mapping, not module state."""
    assert cargo_state_command.cargo_program({"CARGO": "/opt/probe/cargo"}) == Program(
        "/opt/probe/cargo"
    )
    assert cargo_state_command.cargo_program({}) == Program("cargo")


def test_run_cargo_bounds_the_process_and_admits_only_cargo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cargo runs in an owned group under a finite timeout and a closed scope."""
    result = _result(exit_code=0)
    observed: dict[str, object] = {}

    class _BoundCommand:
        """Capture the arguments the boundary hands to the command client."""

        def __init__(self, *arguments: str) -> None:
            """Store the arguments for the later assertion."""
            observed["arguments"] = arguments

        def run_sync(self, *, context: ExecutionContext) -> CommandResult:
            """Record the context and return the stubbed result."""
            observed["context"] = context
            return result

    def make(
        program: Program,
        *,
        catalogue: ProgramCatalogue,
    ) -> type[_BoundCommand]:
        """Record the admitted program and its catalogue."""
        observed["program"] = program
        observed["catalogue"] = catalogue
        return _BoundCommand

    @contextlib.contextmanager
    def record_scope(config: ScopeConfig) -> cabc.Iterator[None]:
        """Record the requested scope without entering the real one."""
        observed["allowlist"] = config.allowlist
        yield

    monkeypatch.setattr(cargo_state_command.sh, "make", make)
    monkeypatch.setattr(cargo_state_command, "scoped", record_scope)
    environment = {"CARGO": "/opt/probe/cargo", "CARGO_BUILD_JOBS": "3"}

    returned = cargo_state_command.run_cargo(("--version",), tmp_path, environment)

    program = typ.cast("Program", observed["program"])
    assert returned is result, "the boundary must return Cargo's result unchanged"
    assert program == Program("/opt/probe/cargo")
    catalogue = typ.cast("ProgramCatalogue", observed["catalogue"])
    assert catalogue.is_allowed(program), "the catalogue must admit the chosen program"
    assert observed["arguments"] == ("--version",)
    context = typ.cast("ExecutionContext", observed["context"])
    assert context.cwd == tmp_path
    assert context.env == environment, (
        "the caller's environment must reach Cargo intact"
    )
    assert context.timeout == cargo_state_command.CARGO_TIMEOUT_SECONDS
    assert context.timeout == 900, (
        "preparation is bounded independently of test budgets"
    )
    assert context.process_group is ProcessGroupPolicy.OWN_GROUP, (
        "a timed workload must be able to clean up Cargo's whole process group"
    )
    assert observed["allowlist"] == frozenset({program}), (
        "the scope must admit Cargo and nothing else"
    )


def test_cargo_invocation_is_a_frozen_record() -> None:
    """A changed operation yields a new record rather than mutating the old one."""
    invocation = _invocation(Path("/workspace"), environment={})

    replacement = dc.replace(invocation, operation="cargo version")

    assert replacement is not invocation, (
        "a changed operation must produce a distinct invocation record"
    )
    assert replacement.operation == "cargo version"
    assert invocation.operation == "cargo fetch --locked", (
        "the original invocation must not drift while a diagnostic is written"
    )
