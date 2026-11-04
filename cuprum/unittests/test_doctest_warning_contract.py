"""Prove the Linux doctest route fails when a doctest emits a warning."""

from __future__ import annotations

import os
import sys
import time
import typing as typ
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from cuprum import (
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    scoped,
    sh,
)
from cuprum.context import EnvMode

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult
    from tests.helpers.prepared_cargo_run import PreparedCargoRun


class _PreparedRun(typ.Protocol):
    """Describe the prepared Cargo state required by one doctest invocation."""

    def command_environment(self, *, target_configuration: str) -> dict[str, str]:
        """Return the isolated Cargo environment for a build configuration."""

    def record_phase(self, phase: str, seconds: float) -> None:
        """Record a monotonic phase duration."""


DEV_FAST_TOOLCHAIN = os.environ.get("DEV_FAST_TOOLCHAIN", "nightly-2026-08-23")
# Each scratch Cargo call must win a compiler-admission slot before it runs, and
# the coordinator exposes no workload lease that could exclude that host-wide
# queue. The per-call bound is therefore an end-to-end watchdog, not an
# execution budget. On 2026-11-03 a 270-second bound expired while the captured
# stream reported 343 requests ahead, so this matches the boundary compile
# tier's finite 3600-second watchdog rather than a measured workload.
DOCTEST_CALL_TIMEOUT_SECONDS = 3600
DOCTEST_TEST_TIMEOUT_SECONDS = 2 * DOCTEST_CALL_TIMEOUT_SECONDS + 60
DOCTEST_RUSTDOC_FLAGS = (
    "--cfg docsrs -D warnings -Zunstable-options --display-doctest-warnings "
    "--doctest-build-arg=-D --doctest-build-arg=warnings"
)
CARGO = Program("cargo")
CARGO_CATALOGUE = ProgramCatalogue.from_programs(
    CARGO,
    documentation_locations=("docs/developers-guide.md",),
)
CARGO_COMMAND = sh.make(CARGO, catalogue=CARGO_CATALOGUE)
WARNING_DOCTEST = """//! Warning-bearing doctest fixture.

/// ```
/// #[deprecated(note = "doctest warning contract")]
/// fn legacy() {}
/// legacy();
/// ```
pub struct WarningFixture;
"""


def _run_doctests(
    crate: Path,
    rustdoc_flags: str | None,
    prepared_run: _PreparedRun,
) -> CommandResult:
    """Run the pinned nightly with the standard backend and stated Rustdoc flags."""
    environment = prepared_run.command_environment(
        target_configuration="doctest-warning-contract"
    )
    environment["RUSTFLAGS"] = "-D warnings"
    environment["RUSTUP_TOOLCHAIN"] = DEV_FAST_TOOLCHAIN
    if rustdoc_flags is not None:
        environment["RUSTDOCFLAGS"] = rustdoc_flags
    else:
        environment.pop("RUSTDOCFLAGS", None)
    context = sh.ExecutionContext(
        cwd=crate,
        env=environment,
        env_mode=EnvMode.REPLACE,
        timeout=DOCTEST_CALL_TIMEOUT_SECONDS,
        process_group=ProcessGroupPolicy.OWN_GROUP,
    )
    started = time.monotonic()
    with scoped(ScopeConfig(allowlist=frozenset({CARGO}))):
        result = CARGO_COMMAND(
            "--color",
            "never",
            "test",
            "--offline",
            "--doc",
            "--manifest-path",
            str(crate / "Cargo.toml"),
        ).run_sync(context=context)
    prepared_run.record_phase("doctest_workload", time.monotonic() - started)
    return result


def _diagnostics(completed: CommandResult) -> str:
    """Return every Cargo diagnostic stream for setup and failure assertions."""
    return (completed.stdout or "") + (completed.stderr or "")


def test_doctest_calls_are_offline_bounded_and_group_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scratch Cargo commands retain caller jobs and have owned finite bounds."""
    crate = tmp_path / "crate"
    crate.mkdir()
    calls: list[tuple[tuple[str, ...], sh.ExecutionContext]] = []
    recorded_phases: list[tuple[str, float]] = []

    class RecordingCommand:
        """Capture the context passed to one fixed Cargo command."""

        def run_sync(self, *, context: sh.ExecutionContext) -> SimpleNamespace:
            """Record the context and return the controlled command result."""
            calls.append((command_arguments[0], context))
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class RecordingPreparedRun:
        """Return an isolated home and target for the controlled command."""

        def command_environment(self, *, target_configuration: str) -> dict[str, str]:
            """Create the expected run-local environment for the doctest."""
            assert target_configuration == "doctest-warning-contract", (
                "the warning test must use its named build configuration"
            )
            return {
                **os.environ,
                "CARGO_HOME": str(tmp_path / "cargo-home"),
                "CARGO_TARGET_DIR": str(tmp_path / "target"),
                "CARGO_NET_OFFLINE": "true",
            }

        def record_phase(self, phase: str, seconds: float) -> None:
            """Record the phase so the timing contract remains observable."""
            recorded_phases.append((phase, seconds))

    class RecordingCargo:
        """Return a command object after recording arguments."""

        def __call__(self, *arguments: str) -> RecordingCommand:
            """Save the invocation arguments and return the command double."""
            command_arguments[0] = arguments
            return RecordingCommand()

    command_arguments: list[tuple[str, ...]] = [()]
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "CARGO_COMMAND", RecordingCargo())
    monkeypatch.setattr(module, "scoped", lambda _scope: nullcontext())
    monkeypatch.setenv("CARGO_BUILD_JOBS", "8")

    result = _run_doctests(
        crate, rustdoc_flags=None, prepared_run=RecordingPreparedRun()
    )

    assert result.exit_code == 0, "the command result must pass through"
    _require_doctest_call_contract(calls, recorded_phases, command_arguments, tmp_path)


def test_doctest_mark_encloses_both_call_watchdogs() -> None:
    """The pytest mark must outlast both bounded Cargo calls and their setup."""
    assert DOCTEST_TEST_TIMEOUT_SECONDS > 2 * DOCTEST_CALL_TIMEOUT_SECONDS, (
        "the mark must not expire while a call can still reach its own bound"
    )


# The end-to-end mark encloses two compiler-admission waits because the
# coordinator has no parent workload lease; it is not an execution-only budget.
@pytest.mark.timeout(DOCTEST_TEST_TIMEOUT_SECONDS, func_only=True)
@pytest.mark.skipif(
    sys.platform != "linux", reason="the production doctest warning route is Linux-only"
)
def test_pinned_doctest_route_rejects_a_warning(
    tmp_path: Path,
    prepared_cargo_run: PreparedCargoRun,
) -> None:
    """Only the corrected nightly Rustdoc route rejects a doctest warning.

    Each Cargo call is bounded and owns its process group. Both routes use
    offline resolution and share the test's isolated target directory.
    """
    crate = tmp_path / "warning-doctest"
    source = crate / "src/lib.rs"
    source.parent.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "warning-doctest"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    source.write_text(WARNING_DOCTEST, encoding="utf-8")
    former_route = _run_doctests(
        crate, rustdoc_flags=None, prepared_run=prepared_cargo_run
    )
    former_diagnostics = _diagnostics(former_route)
    assert former_route.exit_code == 0, (
        "the former RUSTFLAGS-only route must pass the warning-bearing doctest:\n"
        f"{former_diagnostics}"
    )
    assert "test result: ok" in former_diagnostics, (
        "the former route must execute the doctest rather than only build setup"
    )

    corrected_route = _run_doctests(
        crate,
        DOCTEST_RUSTDOC_FLAGS,
        prepared_run=prepared_cargo_run,
    )
    corrected_diagnostics = _diagnostics(corrected_route)
    assert corrected_route.exit_code != 0, (
        "the corrected Rustdoc route must reject a doctest warning:\n"
        f"{corrected_diagnostics}"
    )
    assert "\x1b" not in corrected_diagnostics, (
        "the synthetic Cargo route must disable colour for deterministic diagnostics"
    )
    assert "error: use of deprecated function" in corrected_diagnostics, (
        "the corrected route must fail on the doctest warning rather than setup"
    )
    assert "-D deprecated" in corrected_diagnostics, (
        "rustdoc must deny the displayed doctest warning"
    )


def _require_doctest_call_contract(
    calls: list[tuple[tuple[str, ...], sh.ExecutionContext]],
    recorded_phases: list[tuple[str, float]],
    command_arguments: list[tuple[str, ...]],
    tmp_path: Path,
) -> None:
    """Assert the recorded invocation's arguments, bounds, and environment."""
    arguments, context = calls[0]
    assert arguments == command_arguments[0], (
        "the captured invocation must match its command"
    )
    assert "--offline" in arguments, "scratch workspace routes must stay offline"
    assert "--frozen" not in arguments, (
        "scratch workspace routes must not require a lockfile"
    )
    assert context.timeout == DOCTEST_CALL_TIMEOUT_SECONDS, (
        "each scratch Cargo invocation must have its own finite timeout"
    )
    assert context.process_group is ProcessGroupPolicy.OWN_GROUP, (
        "each invocation must terminate Cargo descendants on expiry"
    )
    assert context.env is not None, "Cargo must receive an explicit environment overlay"
    assert context.env_mode is EnvMode.REPLACE, (
        "ambient variables such as RUSTDOCFLAGS must not leak into the child"
    )
    assert context.env["CARGO_NET_OFFLINE"] == "true", (
        "offline mode must reach Cargo through the environment too"
    )
    assert context.env["CARGO_BUILD_JOBS"] == "8", (
        "the doctest helper must preserve caller-selected Cargo parallelism"
    )
    assert context.env["CARGO_TARGET_DIR"] == str(tmp_path / "target"), (
        "both routes for one scratch crate must share its target directory"
    )
    assert len(recorded_phases) == 1, "the helper must record exactly one phase"
    assert recorded_phases[0][0] == "doctest_workload", (
        "the helper must record the timed Cargo workload"
    )
