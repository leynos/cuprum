"""Pin timeout arguments at the boundary harness subprocess seams."""

from __future__ import annotations

import json
import typing as typ
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import boundary_compile, check_boundary_faults
from scripts.tests import boundary_harness_support

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum import ExecutionContext

EXPECTED_CARGO_METADATA_ARGUMENTS = [
    "/usr/bin/cargo",
    "metadata",
    "--no-deps",
    "--format-version=1",
]


def test_compile_passes_its_named_timeout_to_execution_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copied Cargo check receives the explicit cold-build budget."""
    command_result = SimpleNamespace(exit_code=0, stdout="checked", stderr="")
    contexts: list[ExecutionContext] = []
    cargo_arguments: list[tuple[str, ...]] = []

    class RecordingCommand:
        """Capture the execution context passed to the command seam."""

        def run_sync(self, *, context: ExecutionContext) -> SimpleNamespace:
            """Record the context and return the controlled result."""
            contexts.append(context)
            return command_result

    command = RecordingCommand()

    class RecordingCargo:
        """Capture Cargo arguments before returning the recording command."""

        def __call__(self, *arguments: str) -> RecordingCommand:
            """Return the command used to record its execution context."""
            cargo_arguments.append(arguments)
            return command

    cargo = RecordingCargo()
    monkeypatch.setattr(
        boundary_compile.sh,
        "make",
        lambda _program, *, catalogue: cargo,
    )
    monkeypatch.setattr(boundary_compile, "scoped", lambda _config: nullcontext())
    monkeypatch.setenv("CARGO_BUILD_JOBS", "9")
    target_directory = tmp_path / "target"

    exit_code, output = boundary_compile._compile(tmp_path, target_directory)

    assert contexts[0].timeout == 3600, (
        "the Cargo execution context must enforce a 3600-second compile budget"
    )
    compile_context = contexts[0]
    assert compile_context.env is not None, (
        "the compile must receive an explicit Cargo environment"
    )
    assert compile_context.env["CARGO_TARGET_DIR"] == str(target_directory), (
        "the compile must keep its isolated target directory"
    )
    assert compile_context.env["CARGO_NET_OFFLINE"] == "true", (
        "the timed compile must use dependencies prepared before execution"
    )
    assert compile_context.env["CARGO_BUILD_JOBS"] == "9", (
        "the boundary compile must preserve caller-selected Cargo parallelism"
    )
    assert (
        compile_context.process_group is boundary_compile.ProcessGroupPolicy.OWN_GROUP
    ), "a timed compile must own its group for descendant cleanup"
    assert cargo_arguments == [boundary_compile.compile_arguments()], (
        "the timeout contract must preserve the production Cargo arguments"
    )
    assert "--frozen" in cargo_arguments[0], (
        "the timed compile must preserve its copied lockfile and stay offline"
    )
    assert (exit_code, output) == (0, "checked"), (
        "the compile helper must return the command result"
    )


def test_cargo_metadata_passes_its_named_timeout_and_propagates_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata uses its package-cache wait budget and propagates expiry."""
    source = tmp_path / "src/main.rs"
    metadata = {
        "packages": [
            {
                "name": "cuprum-streams",
                "targets": [{"src_path": str(source)}],
            }
        ]
    }
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(arguments: list[str], **options: object) -> SimpleNamespace:
        """Capture the metadata invocation and return a synthetic package."""
        calls.append((arguments, options))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(metadata),
            stderr="",
        )

    monkeypatch.setattr(
        boundary_harness_support.shutil, "which", lambda _name: "/usr/bin/cargo"
    )
    monkeypatch.setattr(boundary_harness_support.subprocess, "run", run)

    roots = boundary_harness_support.cargo_target_roots(tmp_path)

    assert roots == (source,), "Cargo metadata must return its source root"
    assert len(calls) == 1, "Cargo metadata must run exactly once"
    arguments, options = calls[0]
    assert arguments == EXPECTED_CARGO_METADATA_ARGUMENTS, (
        "Cargo metadata must retain its fixed non-resolving command"
    )
    assert len(options) == 5, "metadata must receive exactly five subprocess options"
    assert options.get("capture_output") is True, "metadata output must be captured"
    assert options.get("check") is False, "metadata errors must use the helper's error"
    assert options.get("cwd") == tmp_path, "metadata must inspect the requested crate"
    assert options.get("text") is True, "metadata output must be decoded as text"
    assert options.get("timeout") == 120, (
        "metadata must use a 120-second subprocess budget"
    )

    timeout_error = boundary_harness_support.subprocess.TimeoutExpired(
        "cargo metadata",
        timeout=boundary_harness_support.CARGO_METADATA_TIMEOUT_SECONDS,
    )

    def raise_timeout(_arguments: list[str], **_options: object) -> None:
        """Raise the saved timeout as Cargo metadata would."""
        raise timeout_error

    monkeypatch.setattr(boundary_harness_support.subprocess, "run", raise_timeout)
    with pytest.raises(boundary_harness_support.subprocess.TimeoutExpired) as raised:
        boundary_harness_support.cargo_target_roots(tmp_path)

    assert raised.value is timeout_error, (
        "metadata timeouts must propagate instead of becoming an empty target list"
    )


def test_fault_cargo_route_is_frozen_and_owns_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault Cargo child inherits jobs but terminates as one owned group."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cargo_home = tmp_path / "cargo-home"
    cargo_home.mkdir()
    monkeypatch.setattr(check_boundary_faults, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_boundary_faults,
        "read_tool_pins",
        lambda: SimpleNamespace(kani="0.1", verus="0.2"),
    )
    monkeypatch.setenv("CARGO_BUILD_JOBS", "6")
    contexts: list[ExecutionContext] = []
    calls: list[tuple[str, ...]] = []

    class RecordingCommand:
        """Capture the process policy used for Cargo fault checks."""

        def run_sync(self, *, context: ExecutionContext) -> SimpleNamespace:
            """Record the context and return a passing Cargo result."""
            contexts.append(context)
            return SimpleNamespace(exit_code=0, stdout="checked", stderr="")

    command = RecordingCommand()

    class RecordingCargo:
        """Capture Cargo's fixed fault-check argument tuple."""

        def __call__(self, *arguments: str) -> RecordingCommand:
            """Record the Cargo arguments and return the command double."""
            calls.append(arguments)
            return command

    monkeypatch.setattr(
        check_boundary_faults.sh,
        "make",
        lambda _program, *, catalogue: RecordingCargo(),
    )
    monkeypatch.setattr(
        check_boundary_faults,
        "scoped",
        lambda _config: nullcontext(),
    )

    code, output = check_boundary_faults._run(
        "cargo", ("test", "--package", "cuprum-native-io"), workspace
    )

    assert code == 0, "the controlled Cargo result must preserve its exit code"
    assert output == "checked", "the controlled Cargo result must preserve output"
    assert calls == [("--frozen", "test", "--package", "cuprum-native-io")], (
        "Cargo fault checks must run offline without changing the lockfile"
    )
    context = contexts[0]
    assert (
        context.process_group is check_boundary_faults.ProcessGroupPolicy.OWN_GROUP
    ), "fault checks must own descendants for bounded termination"
    assert context.timeout == 1200, "the fault workload must keep its finite timeout"
    assert context.env is not None, (
        "the Cargo fault route must receive an explicit environment"
    )
    assert context.env["CARGO_HOME"] == str(cargo_home), (
        "Cargo fault checks must use the run-local writable home"
    )
    assert context.env["CARGO_NET_OFFLINE"] == "true", (
        "fault Cargo commands must use dependencies prepared before execution"
    )
    assert context.env["CARGO_BUILD_JOBS"] == "6", (
        "the fault route must preserve caller-selected Cargo parallelism"
    )
