"""Build and run the verifier commands a boundary fault check needs.

The fault harness pairs each deliberate fault with a control that must pass on
production source. Running a verifier is the part where a copied workspace, an
offline Cargo home, and a per-check build directory have to agree, so the
invocation contract lives here rather than in the fault catalogue.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from cuprum import (
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    ScopeConfig,
    scoped,
    sh,
)
from scripts.boundary_cargo import offline_cargo_environment
from scripts.cargo_state_metadata import _sha256

# The repository root the harness entry point defaults to. Nothing below reads
# it: every helper takes the root it should use, so a test can point the whole
# chain at a temporary copy without patching a module global.
ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True, slots=True)
class ToolPins:
    """Pinned verifier versions, read from the repository's tool manifests."""

    kani: str
    verus: str


@dataclasses.dataclass(frozen=True, slots=True)
class VerifierContext:
    """The repository root and tool pins one verification chain is bound to.

    Passing these together keeps the root and the pins agreeing by construction.
    A run against a temporary copy needs both to change, and a caller that had to
    remember two arguments could hand the copy's workspace to the checkout's
    pins. It is also what makes the dependency visible: :func:`_invocation`
    receives the pins instead of reading a module global itself.
    """

    root: Path
    pins: ToolPins


class _LockfileRewriteError(RuntimeError):
    """Report a verifier that rewrote its lockfile, retaining its output."""

    def __init__(self, message: str, output: str) -> None:
        """Carry the verifier's diagnostic stream with the rewrite failure."""
        super().__init__(message)
        self.output = output


@dataclasses.dataclass(frozen=True, slots=True)
class _Invocation:
    """One verifier command and the lockfile contract that must survive it."""

    command_arguments: tuple[str, ...]
    environment: dict[str, str]
    lockfile: Path | None = None
    lockfile_digest: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _Check:
    """One verifier invocation, its expected diagnostic, and its build tree.

    ``target_dir`` belongs to the check rather than to the call that runs it:
    Kani decides whether to rebuild from that directory alone, so the directory
    is part of what makes this invocation's result mean what it says. A control
    sharing its mutation's directory can be served the mutation's prebuilt
    harness and report the fault it was meant to precede.
    """

    name: str
    program: str
    args: tuple[str, ...]
    expected_failure: str | None = None
    target_dir: Path | None = None


def read_tool_pins(root: Path = ROOT) -> ToolPins:
    """Read the pinned Kani and Verus versions from a repository root.

    The manifests are read when a verification path runs, not at import time,
    so importing the fault catalogue does not require either pin file to exist.
    ``root`` defaults to the checkout these helpers were imported from; pass it
    explicitly to read a copy.

    A missing, unreadable, or empty pin is a hard failure rather than a default:
    a blank version would otherwise build a verifier path that does not exist
    and report the resulting launch failure as a failed proof.

    Parameters
    ----------
    root : Path
        Repository root holding ``tools/kani/VERSION`` and
        ``tools/verus/VERSION``.

    Returns
    -------
    ToolPins
        The versions pinned by those manifests.

    Raises
    ------
    OSError
        If a pin file cannot be read.
    ValueError
        If a pin file exists but records no version.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError/ValueError propagate from _read_pin.
    return ToolPins(
        kani=_read_pin(root, "kani"),
        verus=_read_pin(root, "verus"),
    )


def _read_pin(root: Path, tool: str) -> str:
    """Return one tool's pinned version, refusing a file that records none."""
    pin = (root / f"tools/{tool}/VERSION").read_text(encoding="utf-8").strip()
    if not pin:
        msg = f"tools/{tool}/VERSION records no version"
        raise ValueError(msg)
    return pin


def _invocation(
    check: _Check,
    workspace: Path,
    context: VerifierContext,
) -> _Invocation:
    """Build the verifier environment and its arguments, freezing native Cargo.

    Cargo consumes a global ``--frozen`` before dispatching an external
    subcommand, and cargo-kani 0.67.0 rejects that flag itself, so a Kani
    invocation cannot receive it. The offline environment is the supported
    mechanism there; the copied lockfile is compared afterwards instead.

    ``context`` is a parameter rather than a lookup so the caller decides which
    repository and manifests this run is bound to; the returned environment and
    any lockfile contract then follow from values the caller can inspect.

    ``check.target_dir`` overrides the shared build directory. Kani decides
    whether to rebuild from that directory alone, so a directory reused across a
    mutation and its control can serve the mutation's prebuilt harness to the
    control, which then reports the fault it was meant to precede.

    Returns
    -------
    _Invocation
        The arguments, environment, and lockfile contract for this check.
    """
    kani = Path.home() / f".kani/kani-{context.pins.kani}"
    target_directory = (
        check.target_dir
        or workspace.parent / "target/boundary-faults-debug-all-features"
    )
    environment = os.environ.copy()
    environment.update({
        "CARGO_TARGET_DIR": str(target_directory),
        "LD_LIBRARY_PATH": f"{kani}/toolchain/lib:{kani}/lib",
        "VERUS_Z3_PATH": str(context.root / ".cache/boundary-z3/z3"),
        "RUSTUP_TOOLCHAIN": "1.98.0" if check.program != "cargo" else "1.87.0",
    })
    if check.program != "cargo":
        return _Invocation(check.args, environment)
    environment = offline_cargo_environment(
        workspace,
        target_directory,
        caller_environment=environment,
    )
    if check.args[0] != "kani":
        return _Invocation(("--frozen", *check.args), environment)
    lockfile = workspace / "Cargo.lock"
    return _Invocation(check.args, environment, lockfile, _sha256(lockfile))


def _run(check: _Check, workspace: Path, context: VerifierContext) -> tuple[int, str]:
    """Run one allowlisted verification tool with bounded execution time.

    ``context`` is passed through to :func:`_invocation` rather than read here,
    so the same call chain serves the repository and a copy of it.

    ``check.target_dir`` gives a check its own build directory. Kani decides
    whether to rebuild from that directory alone, so a directory reused across a
    mutation and its control can serve the mutation's prebuilt harness to the
    control. The control then reports the fault it was meant to precede.

    Returns
    -------
    tuple[int, str]
        The verifier's exit code and its combined standard output and error.

    Raises
    ------
    _LockfileRewriteError
        If a guarded Kani run rewrote the copied lockfile, carrying its output.
    """
    executable = Program(check.program)
    project = ProjectSettings(
        name="boundary-faults",
        programs=(executable,),
        documentation_locations=("docs/rust-boundary-verification.md",),
        noise_rules=(),
    )
    command = sh.make(executable, catalogue=ProgramCatalogue(projects=(project,)))
    invocation = _invocation(check, workspace, context)
    execution = sh.ExecutionContext(
        cwd=workspace,
        env=invocation.environment,
        timeout=1200,
        process_group=ProcessGroupPolicy.OWN_GROUP,
    )
    with scoped(ScopeConfig(allowlist=frozenset({executable}))):
        result = command(*invocation.command_arguments).run_sync(context=execution)
    output = (result.stdout or "") + (result.stderr or "")
    if (
        invocation.lockfile is not None
        and _sha256(invocation.lockfile) != invocation.lockfile_digest
    ):
        msg = f"cargo kani changed the copied lockfile: {invocation.lockfile}"
        raise _LockfileRewriteError(msg, output)
    return result.exit_code, output
