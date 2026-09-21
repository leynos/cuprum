#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cyclopts>=4.0", "cuprum==0.1.0"]
# ///
"""Run Cuprum's bounded Loom models and emit an auditable summary."""

from __future__ import annotations

import dataclasses as dc
import enum
import os
import re
import time
import typing as typ
from pathlib import Path

import cyclopts
from cyclopts import Parameter

from cuprum import (
    ExecutionContext,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    scoped,
    sh,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

    from cuprum.sh import CommandResult, SafeCmd

try:
    from cuprum.context import ScopeConfig
except ImportError as error:  # Cuprum 0.1.0 exposes the older keyword scope API.
    if error.name != "cuprum.context":
        raise
    ScopeConfig = None

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LOOM_MANIFEST = Path("rust/cuprum-rust/Cargo.toml")
LOOM_TARGET = "loom"
RUST_TOOLCHAIN = "1.85.0"
CARGO_PROGRAM = Program("cargo")
GIT_PROGRAM = Program("git")
RUSTC_PROGRAM = Program("rustc")
CATALOGUE = ProgramCatalogue(
    projects=(
        ProjectSettings(
            name="loom-driver",
            programs=(CARGO_PROGRAM, GIT_PROGRAM, RUSTC_PROGRAM),
            documentation_locations=("docs/developers-guide.md",),
            noise_rules=(),
        ),
    )
)
cargo = sh.make(CARGO_PROGRAM, catalogue=CATALOGUE)
git = sh.make(GIT_PROGRAM, catalogue=CATALOGUE)
rustc = sh.make(RUSTC_PROGRAM, catalogue=CATALOGUE)
_DISCOVERY_RE = re.compile(r"^(?P<count>\d+) tests?, \d+ benchmarks$", re.MULTILINE)
_EXECUTION_RE = re.compile(
    r"test result: (?:ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed;"
)


@dc.dataclass(frozen=True, slots=True)
class LoomBounds:
    """Bounded exploration inputs passed to Loom through its environment."""

    max_preemptions: int
    max_branches: int
    max_threads: int


class LoomMode(enum.StrEnum):
    """Named execution modes with fixed bounded exploration budgets."""

    SMOKE = "smoke"
    FULL = "full"


@dc.dataclass(frozen=True, slots=True)
class LoomRunResult:
    """Observable result of one discovery-and-execution Loom run."""

    mode: LoomMode
    bounds: LoomBounds
    commit: str
    cargo_version: str
    rustc_version: str
    discovered_tests: int
    executed_tests: int
    elapsed_seconds: float

    def markdown(self) -> str:
        """Render the durable summary used locally and by GitHub Actions."""
        return "\n".join((
            "### Loom model summary",
            "",
            f"- commit: `{self.commit}`",
            f"- mode: `{self.mode}`",
            f"- models discovered: {self.discovered_tests}",
            f"- models executed: {self.executed_tests}",
            (
                "- bounds: "
                f"preemptions={self.bounds.max_preemptions}, "
                f"branches={self.bounds.max_branches}, "
                f"threads={self.bounds.max_threads}"
            ),
            f"- cargo: `{self.cargo_version}`",
            f"- rustc: `{self.rustc_version}`",
            f"- elapsed: {self.elapsed_seconds:.3f}s",
            "",
        ))


@dc.dataclass(frozen=True, slots=True)
class LoomCliOptions:
    """Typed command-line options at the Cyclopts boundary."""

    mode: LoomMode
    summary: Path | None = None
    max_preemptions: int | None = None
    max_branches: int | None = None
    max_threads: int | None = None


class LoomError(Exception):
    """Base error for all bounded Loom driver failures."""


class LoomRunError(LoomError, RuntimeError):
    """Report a failed, incomplete, or vacuous Loom model execution."""

    command: list[str]
    diagnostic: str
    output: str
    discovered: int
    executed: int

    @classmethod
    def command_failed(cls, command: list[str], diagnostic: str) -> LoomRunError:
        """Build the error for Cargo's model failure or exploration exhaustion."""
        message = f"Loom command failed ({' '.join(command)}):\n{diagnostic}"
        error = cls(message)
        error.command = command
        error.diagnostic = diagnostic
        return error

    @classmethod
    def discovery_unreadable(cls, output: str) -> LoomRunError:
        """Build the error for an unparsable test-discovery result."""
        message = f"Could not determine Loom test discovery from:\n{output}"
        error = cls(message)
        error.output = output
        return error

    @classmethod
    def execution_unreadable(cls, output: str) -> LoomRunError:
        """Build the error for an unparsable test-execution result."""
        message = f"Could not determine Loom test execution from:\n{output}"
        error = cls(message)
        error.output = output
        return error

    @classmethod
    def test_count_mismatch(cls, discovered: int, executed: int) -> LoomRunError:
        """Build the error when discovery and execution select different tests."""
        message = (
            f"Loom discovery found {discovered} tests but execution ran {executed}"
        )
        error = cls(message)
        error.discovered = discovered
        error.executed = executed
        return error

    @classmethod
    def invalid_bound_override(cls) -> LoomRunError:
        """Build the error for an incomplete or non-positive bound override."""
        message = (
            "Specify all positive --max-preemptions, --max-branches, and "
            "--max-threads values together"
        )
        return cls(message)

    @classmethod
    def zero_discovered_tests(cls) -> LoomRunError:
        """Build the error for an empty Loom target discovery."""
        message = "Loom target discovered zero tests; refusing a vacuous run"
        return cls(message)

    @classmethod
    def zero_executed_tests(cls) -> LoomRunError:
        """Build the error for a green run that executed no Loom models."""
        message = "Loom target executed zero tests; refusing a vacuous run"
        return cls(message)


class LoomModeError(LoomError, ValueError):
    """Report an unsupported Loom execution mode."""

    mode: str

    @classmethod
    def unsupported(cls, mode: str) -> LoomModeError:
        """Build the error for a caller bypassing command-line mode choices."""
        message = f"unsupported Loom mode: {mode}"
        error = cls(message)
        error.mode = mode
        return error


def _bounds_for_mode(mode: LoomMode) -> LoomBounds:
    """Return the repository's explicit exploration budget for ``mode``."""
    if mode is LoomMode.SMOKE:
        return LoomBounds(max_preemptions=2, max_branches=300, max_threads=4)
    return LoomBounds(max_preemptions=3, max_branches=2_000, max_threads=4)


def _selected_mode(mode: str | LoomMode) -> LoomMode:
    """Return the typed mode while preserving programmatic diagnostics."""
    try:
        return LoomMode(mode)
    except ValueError as error:
        raise LoomModeError.unsupported(str(mode)) from error


def _environment(bounds: LoomBounds) -> dict[str, str]:
    """Build the complete deterministic process environment for Loom."""
    return {
        **os.environ,
        "RUSTFLAGS": "--cfg loom -D warnings",
        "LOOM_MAX_PREEMPTIONS": str(bounds.max_preemptions),
        "LOOM_MAX_BRANCHES": str(bounds.max_branches),
        "LOOM_MAX_THREADS": str(bounds.max_threads),
    }


def _validate_bounds(bounds: LoomBounds) -> LoomBounds:
    """Reject non-positive programmatic exploration bounds."""
    values = (bounds.max_preemptions, bounds.max_branches, bounds.max_threads)
    if any(value < 1 for value in values):
        raise LoomRunError.invalid_bound_override()
    return bounds


def _cargo_command(*tail: str) -> SafeCmd:
    """Build the fixed Cargo command line for the dedicated Loom target."""
    return cargo(
        f"+{RUST_TOOLCHAIN}",
        "test",
        "--manifest-path",
        str(LOOM_MANIFEST),
        "--test",
        LOOM_TARGET,
        "--release",
        *tail,
    )


def _catalogue_scope() -> contextlib.AbstractContextManager[object]:
    """Return a catalogue scope across Cuprum's supported scope APIs."""
    if ScopeConfig is None:
        legacy_scoped = typ.cast(
            "cabc.Callable[..., contextlib.AbstractContextManager[object]]", scoped
        )
        return legacy_scoped(allowlist=CATALOGUE.allowlist)
    return scoped(ScopeConfig(allowlist=CATALOGUE.allowlist))


def _run(command: SafeCmd, *, environment: dict[str, str]) -> CommandResult:
    """Run one fixed tool command while retaining its diagnostic output."""
    context = ExecutionContext(cwd=REPOSITORY_ROOT, env=environment)
    with _catalogue_scope():
        result = command.run_sync(context=context)
    if result.ok:
        return result
    diagnostic = "\n".join(
        output for output in (result.stdout, result.stderr) if output
    )
    if not diagnostic:
        diagnostic = "no process output"
    raise LoomRunError.command_failed(list(command.argv_with_program), diagnostic)


def _tool_output(command: SafeCmd) -> str:
    """Return one version or commit string without accepting command failure."""
    result = _run(command, environment=dict(os.environ))
    return (result.stdout or "").strip()


def _count_discovered(output: str) -> int:
    """Return Cargo's listed Loom-test count, rejecting an empty target."""
    match = _DISCOVERY_RE.search(output)
    if match is None:
        raise LoomRunError.discovery_unreadable(output)
    count = int(match.group("count"))
    if count == 0:
        raise LoomRunError.zero_discovered_tests()
    return count


def _count_executed(output: str) -> int:
    """Return tests Cargo actually executed, rejecting an empty green result."""
    match = _EXECUTION_RE.search(output)
    if match is None:
        raise LoomRunError.execution_unreadable(output)
    count = int(match.group("passed")) + int(match.group("failed"))
    if count == 0:
        raise LoomRunError.zero_executed_tests()
    return count


def run_loom(
    *, mode: str | LoomMode, bounds: LoomBounds | None = None
) -> LoomRunResult:
    """Discover then execute the non-empty Loom target under explicit bounds."""
    selected_mode = _selected_mode(mode)
    selected_bounds = _validate_bounds(bounds or _bounds_for_mode(selected_mode))
    environment = _environment(selected_bounds)
    started_at = time.monotonic()
    discovery = _run(_cargo_command("--", "--list"), environment=environment)
    discovered = _count_discovered(discovery.stdout or "")
    execution = _run(_cargo_command(), environment=environment)
    executed = _count_executed(execution.stdout or "")
    if executed != discovered:
        raise LoomRunError.test_count_mismatch(discovered, executed)
    return LoomRunResult(
        mode=selected_mode,
        bounds=selected_bounds,
        commit=_tool_output(git("rev-parse", "HEAD")),
        cargo_version=_tool_output(cargo(f"+{RUST_TOOLCHAIN}", "--version")),
        rustc_version=_tool_output(rustc(f"+{RUST_TOOLCHAIN}", "--version")),
        discovered_tests=discovered,
        executed_tests=executed,
        elapsed_seconds=time.monotonic() - started_at,
    )


def _parse_arguments() -> LoomCliOptions:
    """Parse the mode-and-summary interface through Cyclopts."""
    app = cyclopts.App(config=cyclopts.config.Env("INPUT_", command=False))

    @app.default
    def parse(
        options: typ.Annotated[LoomCliOptions, Parameter(name="*")],
    ) -> LoomCliOptions:
        """Convert CLI values into driver options."""
        return options

    arguments = app(result_action="return_value")
    if arguments is None:
        raise SystemExit(0)
    return typ.cast("LoomCliOptions", arguments)


def _selected_bounds(arguments: LoomCliOptions) -> LoomBounds | None:
    """Validate optional complete bound overrides from the command line."""
    values = (
        arguments.max_preemptions,
        arguments.max_branches,
        arguments.max_threads,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise LoomRunError.invalid_bound_override()
    return _validate_bounds(
        LoomBounds(
            max_preemptions=typ.cast("int", arguments.max_preemptions),
            max_branches=typ.cast("int", arguments.max_branches),
            max_threads=typ.cast("int", arguments.max_threads),
        )
    )


def main() -> int:
    """Run the requested mode and persist its summary even when CI captures it."""
    arguments = _parse_arguments()
    try:
        result = run_loom(mode=arguments.mode, bounds=_selected_bounds(arguments))
        summary = result.markdown()
    except LoomRunError as error:
        summary = f"### Loom model failure\n\n{error}\n"
        if arguments.summary is not None:
            arguments.summary.write_text(summary, encoding="utf-8")
        print(summary, end="")
        raise
    if arguments.summary is not None:
        arguments.summary.write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
