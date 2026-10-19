"""Reusable builder-library steps registered by the behaviour module.

The functions are plain implementations. The behaviour module binds each one to
its step text, because pytest-bdd resolves steps against the module that
declares the scenario, not against this support module.

The steps build argv only: they never execute the tools, so they run on every
platform. The end-of-options scenarios assert that a source or path beginning
with ``-`` is positioned as an operand, not that a given tar or rsync accepts
the separator. The real-tool projection of that claim lives in
``cuprum/unittests/test_builder_operand_separation.py``.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

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

    from cuprum.sh import SafeCmd

_CHECKPOINT_SOURCE = "--checkpoint-action=exec=touch pwned"


@dc.dataclass(frozen=True, slots=True)
class _RelativeTarInputs:
    """Relative archive and option-shaped sources for the separator scenario."""

    archive: str
    sources: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class _RelativeRsyncPaths:
    """Option-shaped relative paths for the rsync separator scenario."""

    source: str
    destination: str


@dc.dataclass(frozen=True, slots=True)
class _CommandResult:
    """Outcome of one builder invocation, argv or error."""

    argv_with_program: tuple[str, ...] | None = None
    error: Exception | None = None


@dc.dataclass(frozen=True, slots=True)
class _RsyncPaths:
    """Source and destination paths for the rsync builder scenario."""

    source: str
    destination: str


@dc.dataclass(frozen=True, slots=True)
class _TarInputs:
    """Archive and source paths for the tar builder scenario."""

    archive: str
    sources: tuple[str, ...]


def given_core_git_builders() -> None:
    """No-op step for readability."""


def given_valid_rsync_paths(tmp_path: Path) -> _RsyncPaths:
    """Provide absolute rsync source and destination paths."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    return _RsyncPaths(source=source.as_posix(), destination=destination.as_posix())


def given_tar_inputs(tmp_path: Path) -> _TarInputs:
    """Provide a tar archive path and source paths."""
    archive = tmp_path / "archive.tar.gz"
    sources = (tmp_path / "data", tmp_path / "more")
    return _TarInputs(
        archive=archive.as_posix(),
        sources=tuple(source.as_posix() for source in sources),
    )


def given_relative_tar_inputs() -> _RelativeTarInputs:
    """Provide checkpoint-shaped relative sources for the tar separator scenario."""
    return _RelativeTarInputs(
        archive="archive.tar",
        sources=("--checkpoint=1", _CHECKPOINT_SOURCE),
    )


def given_relative_rsync_paths() -> _RelativeRsyncPaths:
    """Provide option-shaped relative paths for the rsync separator scenario."""
    return _RelativeRsyncPaths(source="--source-file", destination="--dest-file")


def _capture(command: SafeCmd) -> _CommandResult:
    """Record one built command's argv as a step result."""
    return _CommandResult(argv_with_program=command.argv_with_program)


def when_build_git_checkout(ref: str) -> _CommandResult:
    """Build a git checkout command and capture ref-validation errors."""
    try:
        return _capture(git_checkout(ref))
    except ValueError as exc:
        return _CommandResult(error=exc)


def when_build_rsync_command(rsync_paths: _RsyncPaths) -> _CommandResult:
    """Build an rsync sync command with archive enabled."""
    command = rsync_sync(
        rsync_paths.source,
        rsync_paths.destination,
        options=RsyncOptions(archive=True),
    )
    return _capture(command)


def when_build_tar_command(tar_inputs: _TarInputs) -> _CommandResult:
    """Build a tar create command with gzip compression."""
    command = tar_create(
        tar_inputs.archive,
        tar_inputs.sources,
        options=TarCreateOptions(compression=Compression.GZIP),
    )
    return _capture(command)


def when_build_relative_tar_command(
    relative_tar_inputs: _RelativeTarInputs,
) -> _CommandResult:
    """Build a tar create command from checkpoint-shaped relative sources."""
    command = tar_create(
        relative_tar_inputs.archive,
        relative_tar_inputs.sources,
        options=TarCreateOptions(allow_relative=True),
    )
    return _capture(command)


def when_build_relative_rsync_command(
    relative_rsync_paths: _RelativeRsyncPaths,
) -> _CommandResult:
    """Build an rsync command from option-shaped relative paths."""
    command = rsync_sync(
        relative_rsync_paths.source,
        relative_rsync_paths.destination,
        options=RsyncOptions(archive=True, allow_relative=True),
    )
    return _capture(command)


def then_git_argv_matches(
    git_result: _CommandResult,
    program: str,
    verb: str,
    ref: str,
) -> None:
    """Verify the git command argv matches expectation."""
    assert git_result.argv_with_program == (program, verb, ref), (  # ruff: ignore[assert] - step assertions replace return values
        "Expected git_result.argv_with_program == (program, verb, ref)"
    )


def then_git_ref_error_raised(git_result: _CommandResult) -> None:
    """Verify git ref validation raised an error."""
    assert isinstance(git_result.error, ValueError), (  # ruff: ignore[assert] - step assertions replace return values
        "Expected git_ref validation to raise ValueError"
    )


def _assert_argv(result: _CommandResult, expected: tuple[str, ...]) -> None:
    """Assert a step result carries exactly the expected argv."""
    assert result.argv_with_program == expected, (  # ruff: ignore[assert] - step assertions replace return values
        f"expected the builder argv {expected}, got {result.argv_with_program}"
    )


def _expected_tar_create_argv(
    flag: str,
    archive: str,
    sources: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the argv ``tar_create`` must emit for a compression flag."""
    compression = (flag,) if flag else ()
    return ("tar", "-c", *compression, "-f", archive, "--", *sources)


def then_rsync_argv_matches(
    rsync_result: _CommandResult,
    rsync_paths: _RsyncPaths,
    flag: str,
) -> None:
    """Verify the rsync argv includes the expected flag and paths."""
    expected = ("rsync", flag, "--", rsync_paths.source, rsync_paths.destination)
    _assert_argv(rsync_result, expected)


def then_tar_argv_matches(
    tar_result: _CommandResult,
    tar_inputs: _TarInputs,
    flag: str,
) -> None:
    """Verify the tar argv includes the expected flag and paths."""
    _assert_argv(
        tar_result,
        _expected_tar_create_argv(flag, tar_inputs.archive, tar_inputs.sources),
    )


def then_tar_sources_follow_separator(
    relative_tar_result: _CommandResult,
    relative_tar_inputs: _RelativeTarInputs,
) -> None:
    """Verify checkpoint-shaped tar sources are operands after ``--``."""
    _assert_argv(
        relative_tar_result,
        _expected_tar_create_argv(
            "",
            relative_tar_inputs.archive,
            relative_tar_inputs.sources,
        ),
    )


def then_rsync_operands_follow_separator(
    relative_rsync_result: _CommandResult,
    relative_rsync_paths: _RelativeRsyncPaths,
) -> None:
    """Verify option-shaped rsync operands follow ``--``."""
    _assert_argv(
        relative_rsync_result,
        (
            "rsync",
            "--archive",
            "--",
            relative_rsync_paths.source,
            relative_rsync_paths.destination,
        ),
    )
