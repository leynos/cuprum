"""Unit tests for the non-vacuous Loom execution driver."""

from __future__ import annotations

import dataclasses as dc
import importlib
import typing as typ
from pathlib import Path

import pytest

from cuprum.sh import CommandResult, SafeCmd

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


class _CommandFailure(typ.Protocol):
    """The diagnostic attributes attached to a failed external command."""

    command: list[str]
    diagnostic: str


class _OutputFailure(typ.Protocol):
    """The raw output retained when Cargo output cannot be parsed."""

    output: str


class _CountMismatch(typ.Protocol):
    """The parsed counts retained for a mismatched Cargo execution."""

    discovered: int
    executed: int


class _UnsupportedMode(typ.Protocol):
    """The mode retained when a caller bypasses the CLI choices."""

    mode: str


class _LoomRunErrorFactory(typ.Protocol):
    """Factory surface used to assert retained exception diagnostics."""

    @classmethod
    def command_failed(cls, command: list[str], diagnostic: str) -> _CommandFailure:
        """Build a command failure with its input diagnostics."""

    @classmethod
    def discovery_unreadable(cls, output: str) -> _OutputFailure:
        """Build a discovery parsing failure retaining raw output."""

    @classmethod
    def execution_unreadable(cls, output: str) -> _OutputFailure:
        """Build an execution parsing failure retaining raw output."""

    @classmethod
    def test_count_mismatch(cls, discovered: int, executed: int) -> _CountMismatch:
        """Build a mismatch retaining both parsed counts."""


class _LoomModeErrorFactory(typ.Protocol):
    """Factory surface used to assert retained unsupported modes."""

    @classmethod
    def unsupported(cls, mode: str) -> _UnsupportedMode:
        """Build an unsupported-mode error retaining its input."""


class LoomDriver(typ.Protocol):
    """Typed surface imported from the standalone Loom driver."""

    LoomBounds: type[_LoomBounds]
    LoomRunError: type[Exception]
    LoomModeError: type[Exception]

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


def _completed(command: SafeCmd, output: str) -> CommandResult:
    """Build a successful Cuprum result for ``command``."""
    return CommandResult(command.program, command.argv, 0, -1, output, "")


def _cargo_run_with_results(
    discovery_output: str, execution_output: str
) -> cabc.Callable[..., CommandResult]:
    """Build a fixed Cargo discovery, execution, and version-result runner."""

    def fake_run(command: SafeCmd, **_kwargs: object) -> CommandResult:
        """Return the configured Cargo or tool-version result."""
        if "--list" in command.argv:
            return _completed(command, discovery_output)
        if "test" in command.argv:
            return _completed(command, execution_output)
        return _completed(command, "version\n")

    return fake_run


def test_run_loom_uses_the_cfg_target_and_nonzero_discovery(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: LoomDriver,
) -> None:
    """The driver discovers and executes the dedicated cfg(loom) target."""
    commands: list[list[str]] = []

    def fake_run(command: SafeCmd, **kwargs: object) -> CommandResult:
        """Record calls and return deterministic Cargo and version output."""
        commands.append(list(command.argv_with_program))
        environment = kwargs["environment"]
        assert isinstance(environment, dict), "the driver must pass a complete env"
        if "test" in command.argv and "--list" in command.argv:
            assert environment["RUSTFLAGS"] == "--cfg loom -D warnings", (
                "the Loom cfg must reach Cargo"
            )
            return _completed(command, "one: test\ntwo: test\n2 tests, 0 benchmarks\n")
        if "test" in command.argv:
            return _completed(
                command,
                (
                    "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
                    "0 filtered out;\n"
                ),
            )
        return _completed(command, "version\n")

    monkeypatch.setattr(loom_driver, "_run", fake_run)

    result = loom_driver.run_loom(mode="smoke")

    loom_commands = [command for command in commands if "test" in command]
    assert len(loom_commands) == 2, "the driver must discover then execute"
    assert all(
        "--test" in command and "loom" in command for command in loom_commands
    ), "both Cargo invocations must select the dedicated Loom target"
    assert all("+1.85.0" in command for command in loom_commands), (
        "both Cargo invocations must select the pinned Rust toolchain"
    )
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
        loom_driver,
        "_run",
        _cargo_run_with_results(case.discovery_output, case.execution_output),
    )

    with pytest.raises(loom_driver.LoomRunError, match=case.error_match):
        loom_driver.run_loom(mode="full")


def test_run_loom_reports_cargo_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: LoomDriver,
) -> None:
    """Counterexamples and bound exhaustion remain visible in the failure."""

    def fake_run(command: SafeCmd, **_kwargs: object) -> CommandResult:
        """Return the native failure Cargo would provide."""
        return CommandResult(
            command.program, command.argv, 101, -1, "", "model exceeded branches"
        )

    monkeypatch.setattr(SafeCmd, "run_sync", fake_run)

    with pytest.raises(loom_driver.LoomRunError, match="model exceeded branches"):
        loom_driver.run_loom(mode="smoke")


def test_error_factories_retain_their_typed_inputs(loom_driver: LoomDriver) -> None:
    """Rendered driver errors also expose their source values to callers."""
    run_errors = typ.cast("type[_LoomRunErrorFactory]", loom_driver.LoomRunError)
    command = ["cargo", "+1.85.0", "test"]
    command_failure = run_errors.command_failed(command, "branch limit")
    discovery_failure = run_errors.discovery_unreadable("not Cargo output")
    execution_failure = run_errors.execution_unreadable("not test output")
    mismatch = run_errors.test_count_mismatch(3, 2)
    mode_errors = typ.cast("type[_LoomModeErrorFactory]", loom_driver.LoomModeError)
    unsupported = mode_errors.unsupported("unexpected")

    assert command_failure.command == command, "command failure must retain argv"
    assert command_failure.diagnostic == "branch limit", "must retain diagnostic"
    assert discovery_failure.output == "not Cargo output", (
        "must retain discovery output"
    )
    assert execution_failure.output == "not test output", "must retain execution output"
    assert mismatch.discovered == 3, "must retain discovered test count"
    assert mismatch.executed == 2, "must retain executed test count"
    assert unsupported.mode == "unexpected", "must retain unsupported mode"


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
