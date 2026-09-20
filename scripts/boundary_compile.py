"""Describe validated Cargo compilation contracts for boundary verification.

The contracts distinguish the verifier's full production coverage from its
focused external-test compilation without admitting arbitrary Cargo arguments.
"""

from __future__ import annotations

import dataclasses
import re
import typing as typ

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh

if typ.TYPE_CHECKING:
    from pathlib import Path

TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _require_target_tuple(value: object) -> tuple[object, ...]:
    """Return the supplied target tuple or reject another collection type."""
    if not isinstance(value, tuple):
        msg = "compile targets must be a tuple"
        raise TypeError(msg)
    return value


def _require_non_empty_targets(names: tuple[object, ...]) -> None:
    """Reject an empty target selection."""
    if not names:
        msg = "compile targets must not be empty"
        raise ValueError(msg)


def _require_string_targets(names: tuple[object, ...]) -> tuple[str, ...]:
    """Return target strings after rejecting other values."""
    if not all(isinstance(name, str) for name in names):
        msg = "compile targets must contain strings"
        raise ValueError(msg)
    return typ.cast("tuple[str, ...]", names)


def _require_valid_target_names(names: tuple[str, ...]) -> None:
    """Reject names that cannot be passed as fixed Cargo target arguments."""
    if any(TARGET_NAME.fullmatch(name) is None for name in names):
        msg = "compile targets must be valid Cargo target names"
        raise ValueError(msg)


@dataclasses.dataclass(frozen=True, slots=True)
class CompileTargets:
    """Select validated named integration-test targets for a focused Cargo check.

    Parameters
    ----------
    names : tuple[str, ...]
        Non-empty Cargo integration-test target names. Each name begins with an
        alphanumeric character and then contains only alphanumeric characters,
        hyphens, or underscores.

    Raises
    ------
    TypeError
        If ``names`` is not a tuple.
    ValueError
        If ``names`` is empty, contains non-strings, or contains invalid names.
    """

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject malformed target selections at every construction boundary."""
        names = _require_target_tuple(self.names)
        _require_non_empty_targets(names)
        _require_valid_target_names(_require_string_targets(names))

    @classmethod
    def integration_tests(cls, *names: str) -> typ.Self:
        """Create a validated immutable integration-test target selection.

        Parameters
        ----------
        names : str
            One or more valid Cargo integration-test target names.

        Returns
        -------
        Self
            A selection whose constructor enforces the target-name invariant.

        The returned constructor-enforced selection raises ``ValueError`` when
        no name is supplied or a supplied name is invalid.
        """
        return cls(names)


def compile_arguments(targets: CompileTargets | None = None) -> tuple[str, ...]:
    """Return the Cargo check contract for full or named-test coverage.

    Parameters
    ----------
    targets : CompileTargets | None, optional
        A validated named-test selection. ``None`` retains the verifier's full
        ``--all-targets`` coverage.

    Returns
    -------
    tuple[str, ...]
        The fixed Cargo subcommand and arguments for the requested coverage.
    """
    if targets is None:
        return (
            "check",
            "--package",
            "cuprum-streams",
            "--all-targets",
            "--all-features",
        )
    test_arguments = tuple(
        argument for name in targets.names for argument in ("--test", name)
    )
    return ("check", "--package", "cuprum-streams", "--all-features", *test_arguments)


def _compile(
    workspace: Path, target_directory: Path, targets: CompileTargets | None = None
) -> tuple[int, str]:
    """Compile a copied safe library through the boundary command contract.

    Parameters
    ----------
    workspace : Path
        Isolated Rust workspace containing the copied boundary sources.
    target_directory : Path
        Dedicated Cargo target directory for the verification run.
    targets : CompileTargets | None, optional
        Named integration tests to compile. ``None`` retains the production
        ``--all-targets`` command; a selection compiles only its named tests.

    Returns
    -------
    tuple[int, str]
        Cargo's exit code and combined standard output and standard error.
    """
    cargo_program = Program("cargo")
    project = ProjectSettings(
        name="boundary-contract",
        programs=(cargo_program,),
        documentation_locations=("docs/rust-boundary-verification.md",),
        noise_rules=(),
    )
    cargo = sh.make(cargo_program, catalogue=ProgramCatalogue(projects=(project,)))
    context = sh.ExecutionContext(
        cwd=workspace,
        env={"CARGO_TARGET_DIR": str(target_directory)},
        timeout=600,
    )
    with scoped(ScopeConfig(allowlist=frozenset({cargo_program}))):
        result = cargo(*compile_arguments(targets)).run_sync(context=context)
    return result.exit_code, (result.stdout or "") + (result.stderr or "")
