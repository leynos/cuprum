"""Unit tests for the core builder library."""

from __future__ import annotations

import sys
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
from cuprum.builders.args import PathRejection
from cuprum.builders.rsync import _local_rsync_operand

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="host-native absolute semantics differ on Windows"
)
_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows drive paths are absolute only on Windows"
)


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


@_POSIX_ONLY
@pytest.mark.parametrize("value", ["C:/file", "C:\\file", "z:/data"])
def test_safe_path_rejects_windows_drive_paths_on_posix(value: str) -> None:
    """A Windows drive prefix is relative on POSIX and must be rejected.

    Rsync reads a leading ``drive:`` as a remote specification, so accepting it
    as an absolute local path would hand the sink a remote operand.
    """
    with pytest.raises(ValueError, match="SafePath") as exc_info:
        safe_path(value)
    assert str(exc_info.value) == PathRejection.NOT_ABSOLUTE.value, (
        "a drive-prefixed value must be rejected as non-absolute on POSIX"
    )


@_WINDOWS_ONLY
@pytest.mark.parametrize("value", ["C:/file", "C:\\file"])
def test_safe_path_accepts_native_windows_drive_paths(value: str) -> None:
    """Host-native absolute detection still accepts drive paths on Windows."""
    assert safe_path(value) == Path(value).as_posix(), (
        "a native Windows drive path must normalize as before"
    )


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
        "--",
        source.as_posix(),
        destination.as_posix(),
    ), "rsync_sync argv mismatch"


# Each spelling is a remote operand for rsync even though safe_path accepts
# it once relative paths are enabled. The drive-prefixed rows are tagged
# separately because default-absolute validation may reject them first.
_REMOTE_OPERANDS = (
    "host:path",
    "host:/path",
    "host::module/path",
    "host::module",
    "user@host:path",
    "rsync://host/module/dir",
    "RSYNC://host/module/dir",
    "[::1]:path",
    "./host:path",
)
_DRIVE_OPERANDS = ("C:/path", "C:\\path")


@pytest.mark.parametrize("role", ["source", "destination"])
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS)
def test_rsync_rejects_remote_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """With relative paths enabled, remote syntax must be named as such."""
    local = "/srv/local"
    source, destination = (operand, local) if role == "source" else (local, operand)
    options = RsyncOptions(allow_relative=True)
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(source, destination, options=options)


@pytest.mark.parametrize("role", ["source", "destination"])
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS + _DRIVE_OPERANDS)
def test_rsync_rejects_remote_operands_by_default(
    operand: str,
    role: str,
) -> None:
    """Default options reject every remote spelling, whatever layer reports it."""
    local = "/srv/local"
    source, destination = (operand, local) if role == "source" else (local, operand)
    with pytest.raises(
        ValueError, match=rf"SafePath|rsync {role} must be a local path"
    ):
        rsync_sync(source, destination)


@pytest.mark.parametrize("role", ["source", "destination"])
@pytest.mark.parametrize("operand", _DRIVE_OPERANDS)
def test_rsync_rejects_drive_prefixed_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """A drive prefix stays a remote operand even with relative paths allowed."""
    local = "/srv/local"
    source, destination = (operand, local) if role == "source" else (local, operand)
    options = RsyncOptions(allow_relative=True)
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(source, destination, options=options)


@pytest.mark.parametrize(
    ("operand", "allow_relative"),
    [
        ("/srv/data", False),
        ("/srv/data", True),
        ("dir/file", True),
        ("dir/host:path", True),
        ("/abs/host:path", False),
    ],
)
def test_rsync_accepts_local_operands_unchanged(
    operand: str,
    *,
    allow_relative: bool,
) -> None:
    """Accepted local operands reach the argv as safe_path normalizes them."""
    expected = str(safe_path(operand, allow_relative=allow_relative))
    cmd = rsync_sync(
        operand,
        operand,
        options=RsyncOptions(allow_relative=allow_relative),
    )
    assert cmd.argv == (expected, expected), (
        "accepted local operands must be emitted in normalized form"
    )


@pytest.mark.parametrize("operand", ["host:path", "host::module", "host::module/path"])
def test_rsync_local_operand_rejects_remote_syntax_directly(operand: str) -> None:
    """The private operand check rejects remote syntax independent of _rsync_argv."""
    with pytest.raises(ValueError, match="rsync source must be a local path"):
        _local_rsync_operand(operand, role="source", allow_relative=True)


@_POSIX_ONLY
def test_rsync_rejects_drive_prefixed_destination_by_default() -> None:
    """A Windows drive destination is not a local absolute path on POSIX."""
    with pytest.raises(ValueError, match="SafePath"):
        rsync_sync("/src", "C:/path")


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
        "--",
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
    expected.extend(["-f", archive.as_posix(), "--", source.as_posix()])
    assert cmd.argv == tuple(expected), "tar_create compression flag mismatch"


_OPTION_SHAPED_SOURCES = (
    "-name",
    "--verbose",
    "--checkpoint=1",
    "--checkpoint-action=exec=touch pwned",
)


@pytest.mark.parametrize("source_name", _OPTION_SHAPED_SOURCES)
def test_tar_create_separates_option_shaped_relative_source(
    tmp_path: Path,
    source_name: str,
) -> None:
    """A leading-dash source stays a file operand after ``--``."""
    archive = "archive.tar"
    cmd = tar_create(
        archive,
        [source_name],
        options=TarCreateOptions(allow_relative=True),
    )
    assert cmd.argv == ("-c", "-f", archive, "--", source_name), (
        "option-shaped relative sources must follow the end-of-options separator"
    )


def test_tar_create_separator_stays_fixed_when_archive_is_dashdash() -> None:
    """An archive literally named ``--`` must not be mistaken for the separator."""
    sources = ["--checkpoint=1", "plain"]
    cmd = tar_create("--", sources, options=TarCreateOptions(allow_relative=True))

    assert cmd.argv == ("-c", "-f", "--", "--", *sources), (
        "the archive value must keep its slot before the separator"
    )
    assert cmd.argv.index("--", 3) == 3, (
        "searching after the archive value must find the separator"
    )


def test_tar_create_separator_precedes_every_source_verbatim() -> None:
    """Mixed ordinary and option-shaped sources keep their order after ``--``."""
    archive = "/srv/archive.tar"
    sources = ["-leading", "/srv/plain", "--checkpoint=1", "relative"]
    cmd = tar_create(archive, sources, options=TarCreateOptions(allow_relative=True))
    separator = cmd.argv.index("--")
    assert cmd.argv == ("-c", "-f", archive, "--", *sources), (
        "the sources must follow the separator verbatim and in order"
    )
    assert separator == 3, "the separator must directly follow the archive value"


def test_rsync_separates_option_shaped_operands() -> None:
    """Rsync source and destination names beginning with ``--`` follow ``--``."""
    cmd = rsync_sync(
        "--source-file",
        "--dest-file",
        options=RsyncOptions(archive=True, allow_relative=True),
    )
    assert cmd.argv == (
        "--archive",
        "--",
        "--source-file",
        "--dest-file",
    ), "rsync operands must follow the end-of-options separator"


def test_tar_extract_keeps_option_values_after_their_flags(tmp_path: Path) -> None:
    """Leading-dash archive and destination stay ``-f`` and ``-C`` values."""
    archive = "-archive.tar"
    destination = "-out"
    cmd = tar_extract(archive, destination=destination, allow_relative=True)
    assert cmd.argv == ("-x", "-f", archive, "-C", destination), (
        "extract paths are option values, so no separator may precede them"
    )


_ALL_COMPRESSION_FLAGS = frozenset({"-z", "-j", "-J"})


@given(compression=st.sampled_from(Compression))
def test_tar_create_emits_at_most_one_compression_flag(
    compression: Compression,
) -> None:
    """No compression choice can produce two compression flags."""
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
