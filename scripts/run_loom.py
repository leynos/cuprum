#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Run Cuprum's bounded Loom models and emit an auditable summary."""

from __future__ import annotations

import argparse
import dataclasses as dc
import os
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this driver runs a fixed Cargo command.
import time
import typing as typ
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LOOM_MANIFEST = Path("rust/cuprum-rust/Cargo.toml")
LOOM_TARGET = "loom"
VALID_MODES = frozenset(("smoke", "full"))
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


@dc.dataclass(frozen=True, slots=True)
class LoomRunResult:
    """Observable result of one discovery-and-execution Loom run."""

    mode: str
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
    """Typed command-line options at the argparse boundary."""

    mode: str
    summary: Path | None
    max_preemptions: int | None
    max_branches: int | None
    max_threads: int | None


class LoomError(Exception):
    """Base error for all bounded Loom driver failures."""


class LoomRunError(LoomError, RuntimeError):
    """Report a failed, incomplete, or vacuous Loom model execution."""

    @classmethod
    def command_failed(cls, command: list[str], diagnostic: str) -> LoomRunError:
        """Build the error for Cargo's model failure or exploration exhaustion."""
        message = f"Loom command failed ({' '.join(command)}):\n{diagnostic}"
        return cls(message)

    @classmethod
    def discovery_unreadable(cls, output: str) -> LoomRunError:
        """Build the error for an unparsable test-discovery result."""
        message = f"Could not determine Loom test discovery from:\n{output}"
        return cls(message)

    @classmethod
    def execution_unreadable(cls, output: str) -> LoomRunError:
        """Build the error for an unparsable test-execution result."""
        message = f"Could not determine Loom test execution from:\n{output}"
        return cls(message)

    @classmethod
    def test_count_mismatch(cls, discovered: int, executed: int) -> LoomRunError:
        """Build the error when discovery and execution select different tests."""
        message = (
            f"Loom discovery found {discovered} tests but execution ran {executed}"
        )
        return cls(message)

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

    @classmethod
    def unsupported(cls, mode: str) -> LoomModeError:
        """Build the error for a caller bypassing command-line mode choices."""
        message = f"unsupported Loom mode: {mode}"
        return cls(message)


def _bounds_for_mode(mode: str) -> LoomBounds:
    """Return the repository's explicit exploration budget for ``mode``."""
    _validate_mode(mode)
    if mode == "smoke":
        return LoomBounds(max_preemptions=2, max_branches=300, max_threads=4)
    return LoomBounds(max_preemptions=3, max_branches=2_000, max_threads=4)


def _validate_mode(mode: str) -> None:
    """Reject programmatic execution modes outside the fixed lane set."""
    if mode not in VALID_MODES:
        raise LoomModeError.unsupported(mode)


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


def _cargo_command(*tail: str) -> list[str]:
    """Build the fixed Cargo command line for the dedicated Loom target."""
    return [
        "cargo",
        "test",
        "--manifest-path",
        str(LOOM_MANIFEST),
        "--test",
        LOOM_TARGET,
        "--release",
        *tail,
    ]


def _run(
    command: list[str], *, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run one fixed tool command while retaining its diagnostic output."""
    try:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - command is assembled exclusively by this driver.
            command,
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        diagnostic = (
            "\n".join(output for output in (error.stdout, error.stderr) if output)
            or "no process output"
        )
        raise LoomRunError.command_failed(command, diagnostic) from error


def _tool_output(command: list[str]) -> str:
    """Return one version or commit string without accepting command failure."""
    result = _run(command, environment=dict(os.environ))
    return result.stdout.strip()


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


def run_loom(*, mode: str, bounds: LoomBounds | None = None) -> LoomRunResult:
    """Discover then execute the non-empty Loom target under explicit bounds."""
    _validate_mode(mode)
    selected_bounds = _validate_bounds(bounds or _bounds_for_mode(mode))
    environment = _environment(selected_bounds)
    started_at = time.monotonic()
    discovery = _run(_cargo_command("--", "--list"), environment=environment)
    discovered = _count_discovered(discovery.stdout)
    execution = _run(_cargo_command(), environment=environment)
    executed = _count_executed(execution.stdout)
    if executed != discovered:
        raise LoomRunError.test_count_mismatch(discovered, executed)
    return LoomRunResult(
        mode=mode,
        bounds=selected_bounds,
        commit=_tool_output(["git", "rev-parse", "HEAD"]),
        cargo_version=_tool_output(["cargo", "--version"]),
        rustc_version=_tool_output(["rustc", "--version"]),
        discovered_tests=discovered,
        executed_tests=executed,
        elapsed_seconds=time.monotonic() - started_at,
    )


def _parse_arguments() -> LoomCliOptions:
    """Parse the intentionally small mode-and-summary command interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "full"), required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--max-preemptions", type=int)
    parser.add_argument("--max-branches", type=int)
    parser.add_argument("--max-threads", type=int)
    arguments = parser.parse_args()
    return LoomCliOptions(
        mode=typ.cast("str", arguments.mode),
        summary=typ.cast("Path | None", arguments.summary),
        max_preemptions=typ.cast("int | None", arguments.max_preemptions),
        max_branches=typ.cast("int | None", arguments.max_branches),
        max_threads=typ.cast("int | None", arguments.max_threads),
    )


def _selected_bounds(arguments: LoomCliOptions) -> LoomBounds | None:
    """Validate optional complete bound overrides from the command line."""
    values = (
        arguments.max_preemptions,
        arguments.max_branches,
        arguments.max_threads,
    )
    if all(value is None for value in values):
        return None
    if arguments.max_preemptions is None:
        raise LoomRunError.invalid_bound_override()
    if arguments.max_branches is None:
        raise LoomRunError.invalid_bound_override()
    if arguments.max_threads is None:
        raise LoomRunError.invalid_bound_override()
    if (
        min(
            arguments.max_preemptions,
            arguments.max_branches,
            arguments.max_threads,
        )
        < 1
    ):
        raise LoomRunError.invalid_bound_override()
    return _validate_bounds(
        LoomBounds(
            max_preemptions=arguments.max_preemptions,
            max_branches=arguments.max_branches,
            max_threads=arguments.max_threads,
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
