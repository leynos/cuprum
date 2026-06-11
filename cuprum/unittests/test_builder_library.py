"""Unit tests for the core builder library."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.builders import (
    Compression,
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

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_safe_path_requires_absolute_by_default(tmp_path: Path) -> None:
    """SafePath rejects relative paths unless explicitly allowed."""
    relative = Path("relative/file.txt")
    with pytest.raises(ValueError, match="SafePath"):
        safe_path(relative)

    absolute = tmp_path / "file.txt"
    assert safe_path(absolute) == absolute.as_posix(), (
        "safe_path should return POSIX for an absolute Path"
    )


def test_safe_path_allows_relative_when_opted_in() -> None:
    """SafePath supports relative paths when allow_relative is True."""
    relative = Path("relative/file.txt")
    assert safe_path(relative, allow_relative=True) == relative.as_posix(), (
        "safe_path should return POSIX string for relative paths when "
        "allow_relative=True"
    )


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
    assert git_ref(value) == value, f"git_ref failed for value: {value}"


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
    assert cmd.argv == ("status", "--short", "--branch"), "git_status argv mismatch"


def test_git_checkout_builder_outputs_args() -> None:
    """git_checkout builds checkout argv with branch creation."""
    cmd = git_checkout("feature/test", create_branch=True)
    assert cmd.argv == ("checkout", "-b", "feature/test"), "git_checkout argv mismatch"


def test_git_checkout_builder_force_branch_creation() -> None:
    """git_checkout uses -B when forcing branch creation."""
    cmd = git_checkout("feature/test", create_branch=True, force=True)
    assert cmd.argv == ("checkout", "-B", "feature/test"), "git_checkout argv mismatch"


def test_git_checkout_rejects_conflicting_flags() -> None:
    """git_checkout rejects incompatible options."""
    with pytest.raises(ValueError, match="create_branch"):
        git_checkout("feature/test", create_branch=True, detach=True)


def test_git_rev_parse_builder_outputs_args() -> None:
    """git_rev_parse builds the expected argv."""
    cmd = git_rev_parse("main")
    assert cmd.argv == ("rev-parse", "main"), "git_rev_parse argv mismatch"


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
    ), "rsync_sync argv mismatch"


def test_tar_create_builder_outputs_args(tmp_path: Path) -> None:
    """tar_create builds the expected argv ordering."""
    archive = tmp_path / "archive.tar.gz"
    sources = [tmp_path / "source-a", tmp_path / "source-b"]
    cmd = tar_create(
        archive,
        sources,
        options=TarCreateOptions(compression=Compression.GZIP),
    )
    assert cmd.argv == (
        "-c",
        "-z",
        "-f",
        archive.as_posix(),
        sources[0].as_posix(),
        sources[1].as_posix(),
    ), "tar_create argv mismatch"


@pytest.mark.parametrize(
    ("compression", "expected_flag"),
    [
        (Compression.NONE, None),
        (Compression.GZIP, "-z"),
        (Compression.BZIP2, "-j"),
        (Compression.XZ, "-J"),
    ],
)
def test_tar_create_maps_each_compression(
    tmp_path: Path,
    compression: Compression,
    expected_flag: str | None,
) -> None:
    """Each Compression member maps to its single tar flag, or none."""
    archive = tmp_path / "archive.tar"
    source = tmp_path / "source"
    cmd = tar_create(
        archive,
        [source],
        options=TarCreateOptions(compression=compression),
    )
    expected = ["-c"]
    if expected_flag is not None:
        expected.append(expected_flag)
    expected.extend(["-f", archive.as_posix(), source.as_posix()])
    assert cmd.argv == tuple(expected), "tar_create compression flag mismatch"


_ALL_COMPRESSION_FLAGS = frozenset({"-z", "-j", "-J"})


@given(compression=st.sampled_from(Compression))
def test_tar_create_emits_at_most_one_compression_flag(
    compression: Compression,
) -> None:
    """Property: no compression choice can produce two compression flags.

    Modelling compression as a single enum makes the "two algorithms at once"
    state unrepresentable, so the constructed argv may carry at most one of
    the mutually-exclusive compression flags.

    Parameters
    ----------
    compression : Compression
        Generated compression member drawn from every enum value.
    """
    archive = Path("/srv/archive.tar")
    cmd = tar_create(
        archive,
        [Path("/srv/source")],
        options=TarCreateOptions(compression=compression),
    )
    flags_present = [arg for arg in cmd.argv if arg in _ALL_COMPRESSION_FLAGS]
    assert len(flags_present) <= 1, "tar_create must emit at most one compression flag"
    if compression is Compression.NONE:
        assert not flags_present, "Compression.NONE must emit no compression flag"


def test_tar_create_rejects_missing_sources(tmp_path: Path) -> None:
    """tar_create requires at least one source."""
    archive = tmp_path / "archive.tar"
    with pytest.raises(ValueError, match="tar_create"):
        tar_create(archive, [])


def test_tar_create_rejects_single_path_source(tmp_path: Path) -> None:
    """tar_create rejects a single path provided as sources."""
    archive = tmp_path / "archive.tar"
    source = tmp_path / "source"
    for value in (source, source.as_posix()):
        sequence = typ.cast("cabc.Sequence[str | Path]", value)
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
    ), "tar_extract argv mismatch"
