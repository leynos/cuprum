"""Run the pinned packaging frontends behind explicit process and file seams.

The factory in :mod:`tests.helpers.distribution_artefacts` owns caching and
timing. This module owns everything that touches the outside world: the
watchdog configuration, the archive scan, and the child process. Keeping the
two apart lets a test drive the cache as a pure query, and the frontend as a
command whose runner, filesystem, and program are supplied rather than
discovered.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

from cuprum import (
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    scoped,
    sh,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult

type Backend = typ.Literal["uv", "maturin"]
type Format = typ.Literal["wheel", "sdist"]

ARCHIVE_SUFFIXES: dict[Format, str] = {
    "wheel": "*.whl",
    "sdist": "*.tar.gz",
}
_MATURIN_SUBCOMMANDS: dict[Format, str] = {
    "wheel": "build",
    "sdist": "sdist",
}
BUILD_TIMEOUT_ENVIRONMENT_VARIABLE = "CUPRUM_PACKAGING_BUILD_TIMEOUT"
# Maturin compiles the native extension from a cold target. A fresh wheel build
# measured 163 s on a loaded developer host on 2026-10-30, but that is workload
# time, not a safe budget: one gate run waited behind 67 and then 123
# compiler-admission requests and overran 600 s. No workload lease exists to
# exclude that queue, so the native frontend keeps the 3600 s end-to-end
# watchdog inherited from its pre-factory contract. The uv frontend only
# packages Python and stays at 180 s. `CUPRUM_PACKAGING_BUILD_TIMEOUT` can raise
# either bound but never lowers a backend below its default.
_DEFAULT_BUILD_TIMEOUT_SECONDS: dict[Backend, int] = {
    "uv": 180,
    "maturin": 3600,
}
_BUILD_TIMEOUT_MARGIN_SECONDS = 60


def configured_build_timeout_seconds(value: str | None, *, backend: Backend) -> int:
    """Return the backend default or a larger positive override.

    The override exists to add queue headroom, so it can only raise a bound.
    Applying a shared value such as ``600`` to both backends would otherwise
    lower the native watchdog that has already expired under compiler-admission
    queueing.

    Returns
    -------
    int
        The effective end-to-end watchdog in seconds.

    Raises
    ------
    ValueError
        If the override is not a positive integer.
    """
    if value is None:
        return _DEFAULT_BUILD_TIMEOUT_SECONDS[backend]
    try:
        timeout_seconds = int(value)
    except ValueError as error:
        msg = f"{BUILD_TIMEOUT_ENVIRONMENT_VARIABLE} must be a positive integer"
        raise ValueError(msg) from error
    if timeout_seconds <= 0:
        msg = f"{BUILD_TIMEOUT_ENVIRONMENT_VARIABLE} must be a positive integer"
        raise ValueError(msg)
    return max(_DEFAULT_BUILD_TIMEOUT_SECONDS[backend], timeout_seconds)


def configured_build_timeouts(
    environment: cabc.Mapping[str, str],
) -> dict[Backend, int]:
    """Return every frontend watchdog from an explicit environment mapping.

    Reading the mapping here rather than at module import keeps the ambient
    process environment out of the factory's behaviour: a caller supplies the
    values it wants, and a test can supply none.

    Returns
    -------
    dict[Backend, int]
        The effective end-to-end watchdog for each packaging backend.
    """
    return {
        backend: configured_build_timeout_seconds(
            environment.get(BUILD_TIMEOUT_ENVIRONMENT_VARIABLE), backend=backend
        )
        for backend in ("uv", "maturin")
    }


def distribution_test_timeout_seconds(environment: cabc.Mapping[str, str]) -> int:
    """Return a consumer bound that keeps its margin over every frontend limit."""
    return (
        max(configured_build_timeouts(environment).values())
        + _BUILD_TIMEOUT_MARGIN_SECONDS
    )


@dc.dataclass(frozen=True, slots=True)
class BuildFailure:
    """Preserve a failed build's original diagnostics for later consumers."""

    diagnostic: str


type BuildOutcome = Path | BuildFailure


@dc.dataclass(frozen=True, slots=True)
class BuildRequest:
    """Describe one frontend invocation and its isolated output locations."""

    backend: Backend
    archive_format: Format
    repository_root: Path
    destination: Path
    environment: cabc.Mapping[str, str]


class ArtefactFileSystem(typ.Protocol):
    """Describe the filesystem commands the packaging layer needs."""

    def prepare(self, directory: Path) -> None:
        """Create a directory that will receive build output."""

    def archives(self, directory: Path, pattern: str) -> list[Path]:
        """Return matching archives in a stable order."""

    def append_line(self, path: Path, line: str) -> None:
        """Append one line to a text file, creating it when absent."""


class PathFileSystem:
    """Apply the packaging filesystem commands to the real filesystem.

    The adapter is stateless: every command names its own path, so the methods
    are static and the object exists only to make the seam injectable.
    """

    @staticmethod
    def prepare(directory: Path) -> None:
        """Create the directory and any missing parents."""
        directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def archives(directory: Path, pattern: str) -> list[Path]:
        """Return the matching archives sorted by path."""
        return sorted(directory.glob(pattern))

    @staticmethod
    def append_line(path: Path, line: str) -> None:
        """Append one newline-terminated line."""
        with path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")


class CommandRunner(typ.Protocol):
    """Run one packaging command under the scope that permits it."""

    def __call__(
        self, arguments: tuple[str, ...], context: sh.ExecutionContext
    ) -> CommandResult:
        """Run the command and return its captured result."""


class ScopedCommandRunner:
    """Run the pinned frontend inside its catalogue scope.

    The scope admits only the injected program, so a packaging route cannot
    reach any other executable.
    """

    def __init__(self, program: Program) -> None:
        """Create the scope for one injected frontend program."""
        self._program = program
        self._catalogue = ProgramCatalogue.from_programs(
            program,
            name="distribution-artefact-tests",
            documentation_locations=("docs/developers-guide.md",),
        )
        self._command = sh.make(program, catalogue=self._catalogue)

    def __call__(
        self, arguments: tuple[str, ...], context: sh.ExecutionContext
    ) -> CommandResult:
        """Run the command with its arguments under the single-program scope."""
        with scoped(ScopeConfig(allowlist=frozenset({self._program}))):
            return self._command(*arguments).run_sync(context=context)


class PackagingFrontend(typ.Protocol):
    """Run one packaging backend and report its archive or diagnostics."""

    def __call__(self, request: BuildRequest) -> BuildOutcome:
        """Build the requested archive."""


class SubprocessFrontend:
    """Run the pinned frontends with an owned process group and finite bound."""

    def __init__(
        self,
        *,
        runner: CommandRunner,
        build_timeouts: cabc.Mapping[Backend, int],
        filesystem: ArtefactFileSystem,
    ) -> None:
        """Store the injected runner, watchdogs, and archive scanner."""
        self._runner = runner
        self._build_timeouts = dict(build_timeouts)
        self._filesystem = filesystem

    def __call__(self, request: BuildRequest) -> BuildOutcome:
        """Run one frontend, then require exactly one archive of its kind."""
        result = self._runner(
            distribution_arguments(
                request.backend, request.archive_format, request.destination
            ),
            sh.ExecutionContext(
                cwd=request.repository_root,
                env=dict(request.environment),
                timeout=self._build_timeouts[request.backend],
                process_group=ProcessGroupPolicy.OWN_GROUP,
            ),
        )
        if result.exit_code != 0:
            return BuildFailure(
                f"{request.backend} {request.archive_format} build failed "
                f"with {result.exit_code}; stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )

        suffix = ARCHIVE_SUFFIXES[request.archive_format]
        archives = self._filesystem.archives(request.destination, suffix)
        if len(archives) != 1:
            return BuildFailure(
                f"{request.backend} {request.archive_format} build must produce "
                f"exactly one {suffix} archive, "
                f"found {[archive.name for archive in archives]}"
            )
        return archives[0]


def distribution_arguments(
    backend: Backend,
    archive_format: Format,
    destination: Path,
) -> tuple[str, ...]:
    """Return the pinned frontend's command for one archive format."""
    if backend == "uv":
        return ("build", f"--{archive_format}", "--out-dir", str(destination))
    return (
        "run",
        "maturin",
        _MATURIN_SUBCOMMANDS[archive_format],
        "--manifest-path",
        "rust/cuprum-rust/Cargo.toml",
        "--out",
        str(destination),
    )


def failure_from_exception(
    backend: Backend,
    archive_format: Format,
    error: Exception,
) -> BuildFailure:
    """Retain timeout streams as well as the exception's diagnostic."""
    diagnostic = (
        f"{backend} {archive_format} build raised {type(error).__name__}: {error}"
    )
    stdout = getattr(error, "stdout", None)
    stderr = getattr(error, "stderr", None)
    if stdout:
        diagnostic += f"\nstdout: {_decode_diagnostic(stdout)}"
    if stderr:
        diagnostic += f"\nstderr: {_decode_diagnostic(stderr)}"
    return BuildFailure(diagnostic)


def _decode_diagnostic(value: str | bytes) -> str:
    """Render one captured stream without assuming its subprocess encoding."""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
