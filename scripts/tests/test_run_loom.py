"""Unit tests for the non-vacuous Loom execution driver."""

from __future__ import annotations

import dataclasses as dc
import importlib
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests replace the fixed driver command.
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class _LoomBounds:
    """Structural test double for the driver's immutable exploration bounds."""

    max_preemptions: int
    max_branches: int
    max_threads: int


@dc.dataclass(frozen=True, slots=True)
class _LoomCliOptions:
    """Typed options accepted by the driver's bound-selection helper."""

    max_preemptions: int | None
    max_branches: int | None
    max_threads: int | None


@dc.dataclass(frozen=True, slots=True)
class _InvalidExecutionCount:
    """Cargo outputs and expected error for an invalid Loom execution."""

    discovery_output: str
    execution_output: str
    error_match: str


class _LoomRunResult(typ.Protocol):
    """The result fields asserted by the driver contract tests."""

    discovered_tests: int
    executed_tests: int


class LoomDriver(typ.Protocol):
    """Typed surface imported from the standalone Loom driver."""

    LoomBounds: type[_LoomBounds]
    LoomRunError: type[Exception]

    def run_loom(
        self, *, mode: str, bounds: _LoomBounds | None = None
    ) -> _LoomRunResult:
        """Run the dedicated Loom target."""

    def _selected_bounds(self, arguments: _LoomCliOptions) -> _LoomBounds | None:
        """Validate a complete command-line override."""


@pytest.fixture(name="loom_driver")
def loom_driver_fixture(monkeypatch: pytest.MonkeyPatch) -> LoomDriver:
    """Import the standalone driver through its runtime top-level path."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return typ.cast("LoomDriver", importlib.import_module("run_loom"))


def _completed(command: list[str], output: str) -> subprocess.CompletedProcess[str]:
    """Build a successful text subprocess result for ``command``."""
    return subprocess.CompletedProcess(command, 0, output, "")


def _cargo_run_with_results(
    discovery_output: str, execution_output: str
) -> cabc.Callable[..., subprocess.CompletedProcess[str]]:
    """Build a fixed Cargo discovery, execution, and version-result runner."""

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Return the configured Cargo or tool-version result."""
        if "--list" in command:
            return _completed(command, discovery_output)
        if command[:2] == ["cargo", "test"]:
            return _completed(command, execution_output)
        return _completed(command, "version\n")

    return fake_run


def test_run_loom_uses_the_cfg_target_and_nonzero_discovery(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: LoomDriver,
) -> None:
    """The driver discovers and executes the dedicated cfg(loom) target."""
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Record calls and return deterministic Cargo and version output."""
        commands.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict), "the driver must pass a complete env"
        if command[:2] == ["cargo", "test"] and "--list" in command:
            assert environment["RUSTFLAGS"] == "--cfg loom -D warnings", (
                "the Loom cfg must reach Cargo"
            )
            return _completed(command, "one: test\ntwo: test\n2 tests, 0 benchmarks\n")
        if command[:2] == ["cargo", "test"]:
            return _completed(
                command,
                (
                    "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
                    "0 filtered out;\n"
                ),
            )
        return _completed(command, "version\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = loom_driver.run_loom(mode="smoke")

    loom_commands = [
        command for command in commands if command[:2] == ["cargo", "test"]
    ]
    assert len(loom_commands) == 2, "the driver must discover then execute"
    assert all(
        "--test" in command and "loom" in command for command in loom_commands
    ), "both Cargo invocations must select the dedicated Loom target"
    assert result.discovered_tests == 2, "discovery count must be retained"
    assert result.executed_tests == 2, "execution count must be retained"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _InvalidExecutionCount(
                "one: test\n1 test, 0 benchmarks\n",
                (
                    "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; "
                    "1 filtered out;\n"
                ),
                "executed zero tests",
            ),
            id="green-zero-execution",
        ),
        pytest.param(
            _InvalidExecutionCount(
                "one: test\ntwo: test\n2 tests, 0 benchmarks\n",
                (
                    "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; "
                    "1 filtered out;\n"
                ),
                "discovery found 2",
            ),
            id="discovery-execution-mismatch",
        ),
    ],
)
def test_run_loom_rejects_invalid_execution_count(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: LoomDriver,
    case: _InvalidExecutionCount,
) -> None:
    """A vacuous or mismatched Cargo execution is a driver failure."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _cargo_run_with_results(case.discovery_output, case.execution_output),
    )

    with pytest.raises(loom_driver.LoomRunError, match=case.error_match):
        loom_driver.run_loom(mode="full")


def test_run_loom_reports_cargo_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: LoomDriver,
) -> None:
    """Counterexamples and bound exhaustion remain visible in the failure."""

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Raise the native failure Cargo would provide."""
        raise subprocess.CalledProcessError(
            101, command, stderr="model exceeded branches"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(loom_driver.LoomRunError, match="model exceeded branches"):
        loom_driver.run_loom(mode="smoke")


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(_LoomCliOptions(None, 2, 3), id="missing-preemptions"),
        pytest.param(_LoomCliOptions(1, None, 3), id="missing-branches"),
        pytest.param(_LoomCliOptions(1, 2, None), id="missing-threads"),
    ],
)
def test_selected_bounds_rejects_each_partial_override(
    loom_driver: LoomDriver,
    arguments: _LoomCliOptions,
) -> None:
    """An incomplete command-line override cannot select a model budget."""
    with pytest.raises(loom_driver.LoomRunError, match="Specify all positive"):
        loom_driver._selected_bounds(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(_LoomCliOptions(0, 2, 3), id="zero-preemptions"),
        pytest.param(_LoomCliOptions(1, 0, 3), id="zero-branches"),
        pytest.param(_LoomCliOptions(1, 2, 0), id="zero-threads"),
        pytest.param(_LoomCliOptions(-1, 2, 3), id="negative-preemptions"),
        pytest.param(_LoomCliOptions(1, -1, 3), id="negative-branches"),
        pytest.param(_LoomCliOptions(1, 2, -1), id="negative-threads"),
    ],
)
def test_selected_bounds_rejects_each_non_positive_complete_override(
    loom_driver: LoomDriver,
    arguments: _LoomCliOptions,
) -> None:
    """A complete override delegates non-positive values to shared validation."""
    with pytest.raises(loom_driver.LoomRunError, match="Specify all positive"):
        loom_driver._selected_bounds(arguments)


def test_selected_bounds_returns_a_complete_positive_override(
    loom_driver: LoomDriver,
) -> None:
    """A complete positive override reaches the shared bound validator."""
    bounds = loom_driver._selected_bounds(_LoomCliOptions(1, 2, 3))

    assert bounds is not None, "a complete positive override must yield bounds"
    assert (
        bounds.max_preemptions,
        bounds.max_branches,
        bounds.max_threads,
    ) == (1, 2, 3), "the shared validator must preserve every override value"


def test_run_loom_rejects_an_unknown_mode(loom_driver: LoomDriver) -> None:
    """Programmatic callers cannot bypass the command-line mode choices."""
    with pytest.raises(ValueError, match="unsupported Loom mode"):
        loom_driver.run_loom(
            mode="unknown",
            bounds=loom_driver.LoomBounds(1, 1, 1),
        )


def test_run_loom_rejects_a_non_positive_programmatic_bound(
    loom_driver: LoomDriver,
) -> None:
    """Direct callers cannot silently remove the configured exploration budget."""
    with pytest.raises(loom_driver.LoomRunError, match="Specify all positive"):
        loom_driver.run_loom(
            mode="smoke",
            bounds=loom_driver.LoomBounds(0, 1, 1),
        )
