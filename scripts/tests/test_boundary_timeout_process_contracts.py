"""Pin timeout arguments at the boundary harness subprocess seams."""

from __future__ import annotations

import json
import typing as typ
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import boundary_compile, boundary_fault_invocations
from scripts.boundary_cargo import offline_cargo_environment
from scripts.tests import boundary_harness_support

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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
    (tmp_path / "workspace").mkdir()
    (tmp_path / "cargo-home").mkdir()
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
    exit_code, output = boundary_compile._compile(
        tmp_path / "workspace",
        tmp_path / "target",
    )

    assert contexts[0].timeout == 3600, (
        "the Cargo execution context must enforce a 3600-second compile budget"
    )
    compile_context = contexts[0]
    assert compile_context.env is not None, (
        "the compile must receive an explicit Cargo environment"
    )
    assert compile_context.env["CARGO_TARGET_DIR"] == str(tmp_path / "target"), (
        "the compile must keep its isolated target directory"
    )
    assert compile_context.env["CARGO_NET_OFFLINE"] == "true", (
        "the timed compile must use dependencies prepared before execution"
    )
    assert compile_context.env["CARGO_HOME"] == str(tmp_path / "cargo-home"), (
        "the timed compile must use its run-local writable Cargo home"
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


def test_offline_cargo_environment_rejects_a_missing_run_home(
    tmp_path: Path,
) -> None:
    """A missing prepared home must not fall back to the ambient Cargo cache."""
    workspace = tmp_path / "run" / "workspace"
    workspace.mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="prepared run-local Cargo home"):
        offline_cargo_environment(workspace, tmp_path / "run" / "target")


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


def _run_with_recorded_cargo(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    args: tuple[str, ...],
    *,
    after_run: cabc.Callable[[Path], None] | None = None,
) -> tuple[list[tuple[str, ...]], list[ExecutionContext], tuple[int, str]]:
    """Drive one Cargo fault route through an argument-recording seam."""
    cargo_home = workspace.parent / "cargo-home"
    cargo_home.mkdir(exist_ok=True)
    monkeypatch.setattr(boundary_fault_invocations, "ROOT", workspace.parent)
    monkeypatch.setattr(
        boundary_fault_invocations,
        "read_tool_pins",
        lambda: SimpleNamespace(kani="0.1", verus="0.2"),
    )
    monkeypatch.setenv("CARGO_BUILD_JOBS", "6")
    contexts: list[ExecutionContext] = []
    calls: list[tuple[str, ...]] = []

    class RecordingCommand:
        """Capture the process policy used for Cargo fault checks."""

        def run_sync(self, *, context: ExecutionContext) -> SimpleNamespace:
            """Record the context, apply any post-run change, and pass."""
            contexts.append(context)
            if after_run is not None:
                after_run(workspace)
            return SimpleNamespace(exit_code=0, stdout="checked", stderr="")

    command = RecordingCommand()

    class RecordingCargo:
        """Capture Cargo's fixed fault-check argument tuple."""

        def __call__(self, *arguments: str) -> RecordingCommand:
            """Record the Cargo arguments and return the command double."""
            calls.append(arguments)
            return command

    monkeypatch.setattr(
        boundary_fault_invocations.sh,
        "make",
        lambda _program, *, catalogue: RecordingCargo(),
    )
    monkeypatch.setattr(
        boundary_fault_invocations,
        "scoped",
        lambda _config: nullcontext(),
    )
    result = boundary_fault_invocations._run("cargo", args, workspace)
    return calls, contexts, result


def test_fault_cargo_route_is_frozen_and_owns_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault Cargo child inherits jobs but terminates as one owned group."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cargo_home = tmp_path / "cargo-home"

    calls, contexts, (code, output) = _run_with_recorded_cargo(
        monkeypatch,
        workspace,
        ("test", "--package", "cuprum-native-io"),
    )

    assert code == 0, "the controlled Cargo result must preserve its exit code"
    assert output == "checked", "the controlled Cargo result must preserve output"
    assert calls == [("--frozen", "test", "--package", "cuprum-native-io")], (
        "Cargo fault checks must run offline without changing the lockfile"
    )
    context = contexts[0]
    assert (
        context.process_group is boundary_fault_invocations.ProcessGroupPolicy.OWN_GROUP
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


def test_fault_kani_route_guards_the_copied_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cargo-kani rejects `--frozen`, so a lockfile digest enforces the contract."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lockfile = workspace / "Cargo.lock"
    lockfile.write_text("version = 4\n", encoding="utf-8")
    kani_args = ("kani", "--package", "cuprum-native-io", "--harness", "harness")

    calls, contexts, (code, output) = _run_with_recorded_cargo(
        monkeypatch,
        workspace,
        kani_args,
    )

    assert (code, output) == (0, "checked"), "the guarded run must pass through"
    assert calls == [kani_args], (
        "cargo-kani must not receive the frozen flag it rejects"
    )
    assert contexts[0].env is not None, "Kani checks must receive their environment"
    assert contexts[0].env["CARGO_NET_OFFLINE"] == "true", (
        "the supported offline mechanism must reach cargo-kani's builds"
    )

    def rewrite_lockfile(path: Path) -> None:
        """Simulate an internal Cargo build rewriting the copied lockfile."""
        (path / "Cargo.lock").write_text("version = 4\n[[package]]\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed the copied lockfile"):
        _run_with_recorded_cargo(
            monkeypatch,
            workspace,
            kani_args,
            after_run=rewrite_lockfile,
        )
