"""Unit tests for the core builder library."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest

from cuprum.builders import (
    RsyncOptions,
    TarCreateOptions,
    git_checkout,
    git_ref,
    git_rev_parse,
    git_status,
    rsync_sync,
    safe_path,
    tar_create,
    tar_extract,
)


def test_safe_path_requires_absolute_by_default(tmp_path: Path) -> None:
    """SafePath rejects relative paths unless explicitly allowed."""
    relative = Path("relative/file.txt")
    with pytest.raises(ValueError, match="SafePath"):
        safe_path(relative)

    absolute = tmp_path / "file.txt"
    assert safe_path(absolute) == absolute.as_posix()


def test_safe_path_allows_relative_when_opted_in() -> None:
    """SafePath supports relative paths when allow_relative is True."""
    relative = Path("relative/file.txt")
    assert safe_path(relative, allow_relative=True) == relative.as_posix()


def test_safe_path_rejects_empty_and_null() -> None:
    """SafePath rejects empty strings and NUL characters."""
    with pytest.raises(ValueError, match="SafePath"):
        safe_path("")
    with pytest.raises(ValueError, match="SafePath"):
        safe_path("bad\x00path")


def test_safe_path_rejects_parent_segments(tmp_path: Path) -> None:
    """SafePath rejects traversal segments to avoid ambiguity."""
    parent_path = tmp_path / ".." / "var"
    with pytest.raises(ValueError, match="SafePath"):
        safe_path(parent_path)


@pytest.mark.parametrize(
    "value",
    [
        "main",
        "feature/one",
        "v1.2.3",
        "refs/heads/main",
        "deadbeef",
    ],
)
def test_git_ref_accepts_valid_values(value: str) -> None:
    """GitRef allows common branch, tag, and object names."""
    assert git_ref(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " bad",
        "bad ref",
        "-branch",
        "bad@{",
        "bad..ref",
        "bad//ref",
        "bad.lock",
        "bad/",
        "/bad",
        "bad.",
        "HEAD~1",
    ],
)
def test_git_ref_rejects_invalid_values(value: str) -> None:
    """GitRef rejects whitespace and unsafe ref formats."""
    with pytest.raises(ValueError, match="GitRef"):
        git_ref(value)


def test_git_status_builder_outputs_flags() -> None:
    """git_status builds the expected argv."""
    cmd = git_status(short=True, branch=True)
    assert cmd.argv == ("status", "--short", "--branch")


def test_git_checkout_builder_outputs_args() -> None:
    """git_checkout builds checkout argv with branch creation."""
    cmd = git_checkout("feature/test", create_branch=True)
    assert cmd.argv == ("checkout", "-b", "feature/test")


def test_git_checkout_builder_force_branch_creation() -> None:
    """git_checkout uses -B when forcing branch creation."""
    cmd = git_checkout("feature/test", create_branch=True, force=True)
    assert cmd.argv == ("checkout", "-B", "feature/test")


def test_git_checkout_rejects_conflicting_flags() -> None:
    """git_checkout rejects incompatible options."""
    with pytest.raises(ValueError, match="create_branch"):
        git_checkout("feature/test", create_branch=True, detach=True)


def test_git_rev_parse_builder_outputs_args() -> None:
    """git_rev_parse builds the expected argv."""
    cmd = git_rev_parse("main")
    assert cmd.argv == ("rev-parse", "main")


def test_rsync_builder_outputs_args(tmp_path: Path) -> None:
    """rsync_sync builds the expected argv ordering."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    cmd = rsync_sync(
        source,
        destination,
        options=RsyncOptions(
            archive=True,
            delete=True,
            dry_run=True,
            verbose=True,
            compress=True,
        ),
    )
    assert cmd.argv == (
        "--archive",
        "--delete",
        "--dry-run",
        "--verbose",
        "--compress",
        source.as_posix(),
        destination.as_posix(),
    )


def test_tar_create_builder_outputs_args(tmp_path: Path) -> None:
    """tar_create builds the expected argv ordering."""
    archive = tmp_path / "archive.tar.gz"
    sources = [tmp_path / "source-a", tmp_path / "source-b"]
    cmd = tar_create(archive, sources, options=TarCreateOptions(gzip=True))
    assert cmd.argv == (
        "-c",
        "-z",
        "-f",
        archive.as_posix(),
        sources[0].as_posix(),
        sources[1].as_posix(),
    )


def test_tar_create_rejects_multiple_compression(tmp_path: Path) -> None:
    """tar_create rejects multiple compression flags."""
    archive = tmp_path / "archive.tar"
    source = tmp_path / "source"
    with pytest.raises(ValueError, match="tar_create"):
        tar_create(
            archive,
            [source],
            options=TarCreateOptions(gzip=True, bzip2=True),
        )


def test_tar_create_rejects_missing_sources(tmp_path: Path) -> None:
    """tar_create requires at least one source."""
    archive = tmp_path / "archive.tar"
    with pytest.raises(ValueError, match="tar_create"):
        tar_create(archive, [])


def test_tar_create_rejects_single_path_source(tmp_path: Path) -> None:
    """tar_create rejects a single path provided as sources."""
    archive = tmp_path / "archive.tar"
    source = tmp_path / "source"
    sequence = typ.cast("typ.Sequence[str | Path]", source)
    with pytest.raises(TypeError, match="tar_create requires a sequence of"):
        tar_create(archive, sequence)


def test_tar_extract_builder_outputs_args(tmp_path: Path) -> None:
    """tar_extract builds argv with destination when provided."""
    archive = tmp_path / "archive.tar"
    destination = tmp_path / "dest"
    cmd = tar_extract(archive, destination=destination)
    assert cmd.argv == (
        "-x",
        "-f",
        archive.as_posix(),
        "-C",
        destination.as_posix(),
    )
