"""Unit tests for the non-vacuous Loom execution driver."""

from __future__ import annotations

import importlib
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests replace the fixed driver command.
import types
from pathlib import Path

import pytest


@pytest.fixture(name="loom_driver")
def loom_driver_fixture(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import the standalone driver through its runtime top-level path."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module("run_loom")


def _completed(command: list[str], output: str) -> subprocess.CompletedProcess[str]:
    """Build a successful text subprocess result for ``command``."""
    return subprocess.CompletedProcess(command, 0, output, "")


def test_run_loom_uses_the_cfg_target_and_nonzero_discovery(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: types.ModuleType,
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


def test_run_loom_rejects_a_green_zero_test_execution(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: types.ModuleType,
) -> None:
    """A target that compiles but executes no models is a driver failure."""

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Return a non-empty discovery then an invalid green execution."""
        if "--list" in command:
            return _completed(command, "one: test\n1 test, 0 benchmarks\n")
        if command[:2] == ["cargo", "test"]:
            return _completed(
                command,
                (
                    "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; "
                    "1 filtered out;\n"
                ),
            )
        return _completed(command, "version\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(loom_driver.LoomRunError, match="executed zero tests"):
        loom_driver.run_loom(mode="full")


def test_run_loom_reports_cargo_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    loom_driver: types.ModuleType,
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


def test_selected_bounds_rejects_partial_or_zero_overrides(
    loom_driver: types.ModuleType,
) -> None:
    """Overrides cannot accidentally turn a bounded model into an invalid run."""
    arguments = types.SimpleNamespace(
        max_preemptions=1,
        max_branches=None,
        max_threads=3,
    )

    with pytest.raises(loom_driver.LoomRunError, match="Specify all positive"):
        loom_driver._selected_bounds(arguments)
