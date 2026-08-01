"""Behavioural tests for the core builder library."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from cuprum.builders import (
    Compression,
    RsyncOptions,
    TarCreateOptions,
    git_checkout,
    rsync_sync,
    tar_create,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@scenario(
    "../features/builder_library.feature",
    "Git checkout builder constructs argv",
)
def test_git_checkout_builder() -> None:
    """Behavioural coverage for git checkout builder."""


@scenario(
    "../features/builder_library.feature",
    "Invalid git ref is rejected",
)
def test_git_ref_rejection() -> None:
    """Behavioural coverage for git ref validation."""


@scenario(
    "../features/builder_library.feature",
    "Rsync builder constructs argv with safe paths",
)
def test_rsync_builder() -> None:
    """Behavioural coverage for rsync builder."""


@scenario(
    "../features/builder_library.feature",
    "Tar create builder constructs argv",
)
def test_tar_builder() -> None:
    """Behavioural coverage for tar builder."""


@dc.dataclass(frozen=True, slots=True)
class _CommandResult:
    argv_with_program: tuple[str, ...] | None = None
    error: Exception | None = None


@dc.dataclass(frozen=True, slots=True)
class _RsyncPaths:
    source: str
    destination: str


@dc.dataclass(frozen=True, slots=True)
class _TarInputs:
    archive: str
    sources: tuple[str, ...]


@given("the core git builders")
def given_core_git_builders() -> None:
    """No-op step for readability."""


@given("valid rsync paths", target_fixture="rsync_paths")
def given_valid_rsync_paths(tmp_path: Path) -> _RsyncPaths:
    """Provide absolute paths for rsync builder scenarios.

    Parameters
    ----------
    tmp_path : Path
        The pytest temporary directory the rsync paths are rooted in.

    Returns
    -------
    _RsyncPaths
        The source and destination paths for building rsync commands.
    """
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    return _RsyncPaths(source=source.as_posix(), destination=destination.as_posix())


@given("a tar archive and source paths", target_fixture="tar_inputs")
def given_tar_inputs(tmp_path: Path) -> _TarInputs:
    """Provide tar inputs for builder scenarios.

    Parameters
    ----------
    tmp_path : Path
        The pytest temporary directory the tar inputs are rooted in.

    Returns
    -------
    _TarInputs
        The archive path and source paths for building tar commands.
    """
    archive = tmp_path / "archive.tar.gz"
    sources = (tmp_path / "data", tmp_path / "more")
    return _TarInputs(
        archive=archive.as_posix(),
        sources=tuple(source.as_posix() for source in sources),
    )


@when(
    parsers.parse('I build a git checkout command for ref "{ref}"'),
    target_fixture="git_result",
)
def when_build_git_checkout(ref: str) -> _CommandResult:
    """Build a git checkout command and capture errors.

    Parameters
    ----------
    ref : str
        The git reference the checkout command is built for.

    Returns
    -------
    _CommandResult
        The built command's argv, or the captured error if the ref is
        rejected.
    """
    try:
        cmd = git_checkout(ref)
    except ValueError as exc:
        return _CommandResult(error=exc)
    return _CommandResult(argv_with_program=cmd.argv_with_program)


@when(
    "I build an rsync sync command with archive enabled",
    target_fixture="rsync_result",
)
def when_build_rsync_command(rsync_paths: _RsyncPaths) -> _CommandResult:
    """Build an rsync sync command.

    Parameters
    ----------
    rsync_paths : _RsyncPaths
        The source and destination paths the rsync command is built
        from.

    Returns
    -------
    _CommandResult
        The built rsync command's argv.
    """
    cmd = rsync_sync(
        rsync_paths.source,
        rsync_paths.destination,
        options=RsyncOptions(archive=True),
    )
    return _CommandResult(argv_with_program=cmd.argv_with_program)


@when(
    "I build a tar create command with gzip enabled",
    target_fixture="tar_result",
)
def when_build_tar_command(tar_inputs: _TarInputs) -> _CommandResult:
    """Build a tar create command with gzip compression.

    Parameters
    ----------
    tar_inputs : _TarInputs
        The archive and source paths the tar command is built from.

    Returns
    -------
    _CommandResult
        The built tar command's argv.
    """
    cmd = tar_create(
        tar_inputs.archive,
        tar_inputs.sources,
        options=TarCreateOptions(compression=Compression.GZIP),
    )
    return _CommandResult(argv_with_program=cmd.argv_with_program)


@then(parsers.parse('the git command argv is "{program}" "{verb}" "{ref}"'))
def then_git_argv_matches(
    git_result: _CommandResult,
    program: str,
    verb: str,
    ref: str,
) -> None:
    """Verify the git command argv matches expectation.

    Parameters
    ----------
    git_result : _CommandResult
        The built git command or captured error.
    program : str
        The expected program name.
    verb : str
        The expected git subcommand.
    ref : str
        The expected reference argument.
    """
    assert git_result.argv_with_program == (program, verb, ref)


@then("a git ref validation error is raised")
def then_git_ref_error_raised(git_result: _CommandResult) -> None:
    """Verify git ref validation raised an error.

    Parameters
    ----------
    git_result : _CommandResult
        The captured result expected to hold a validation error.
    """
    assert isinstance(git_result.error, ValueError), (
        "Expected git_ref validation to raise ValueError"
    )


@then(
    parsers.parse(
        'the rsync command argv includes "{flag}" and the paths',
    ),
)
def then_rsync_argv_matches(
    rsync_result: _CommandResult,
    rsync_paths: _RsyncPaths,
    flag: str,
) -> None:
    """Verify the rsync command argv includes expected flag and paths.

    Parameters
    ----------
    rsync_result : _CommandResult
        The built rsync command.
    rsync_paths : _RsyncPaths
        The paths expected in the argv.
    flag : str
        The flag expected in the argv.
    """
    assert rsync_result.argv_with_program == (
        "rsync",
        flag,
        rsync_paths.source,
        rsync_paths.destination,
    )


@then(
    parsers.parse('the tar command argv includes "{flag}" and the paths'),
)
def then_tar_argv_matches(
    tar_result: _CommandResult,
    tar_inputs: _TarInputs,
    flag: str,
) -> None:
    """Verify the tar command argv includes expected flag and paths.

    Parameters
    ----------
    tar_result : _CommandResult
        The built tar command.
    tar_inputs : _TarInputs
        The archive and sources expected in the argv.
    flag : str
        The flag expected in the argv.
    """
    assert tar_result.argv_with_program == (
        "tar",
        "-c",
        flag,
        "-f",
        tar_inputs.archive,
        *tar_inputs.sources,
    )
