"""Property-based tests for the tar and rsync command builders.

These tests pin down the argv contracts of ``tar_create``, ``tar_extract``,
and ``rsync_sync`` over generated paths and option combinations, via the
pure argv builders (``_tar_create_argv``, ``_tar_extract_argv``,
``_rsync_argv``) extracted for verifiability (issue #71).

The invariants checked here are:

- ``tar_create`` argv is exactly ``-c``, at most one compression flag,
  ``-f``, the archive, then the sources in order; the compression flag
  matches the ``Compression`` member and never appears twice.
- ``tar_create`` rejects empty source sequences (``ValueError``) and a
  bare string or ``Path`` passed instead of a sequence (``TypeError``).
- ``tar_extract`` argv is ``-x -f <archive>`` with an optional trailing
  ``-C <destination>``.
- ``rsync_sync`` emits exactly the flags enabled on ``RsyncOptions`` in
  the documented fixed order, followed by source then destination.
- Path conversion is deterministic: equal inputs give equal argv, and a
  ``Path`` yields the same argv as the equivalent string.
- The public wrappers attach the curated program and the argv produced
  by the pure builders.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.builders.rsync import (
    _FLAG_ORDER,
    RsyncOptions,
    _rsync_argv,
    rsync_sync,
)
from cuprum.builders.tar import (
    Compression,
    TarCreateOptions,
    _tar_create_argv,
    _tar_extract_argv,
    tar_create,
    tar_extract,
)
from cuprum.catalogue import RSYNC, TAR

_COMPRESSION_BY_MEMBER = {
    Compression.NONE: None,
    Compression.GZIP: "-z",
    Compression.BZIP2: "-j",
    Compression.XZ: "-J",
}
_ALL_COMPRESSION_FLAGS = {"-z", "-j", "-J"}

# Path segments avoid ".", NUL, and separators so generated paths are
# always SafePath-valid and normalisation is the identity.
_SEGMENTS = st.text(alphabet="abcxyz09-_", min_size=1, max_size=6)
_ABS_PATHS = st.lists(_SEGMENTS, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts),
)
_COMPRESSIONS = st.sampled_from(sorted(Compression, key=lambda c: c.value))
_RSYNC_OPTIONS = st.builds(
    RsyncOptions,
    archive=st.booleans(),
    delete=st.booleans(),
    dry_run=st.booleans(),
    verbose=st.booleans(),
    compress=st.booleans(),
)


@settings(max_examples=200)
@given(
    archive=_ABS_PATHS,
    sources=st.lists(_ABS_PATHS, min_size=1, max_size=5),
    compression=_COMPRESSIONS,
)
def test_tar_create_argv_shape_and_single_compression_flag(
    archive: str,
    sources: list[str],
    compression: Compression,
) -> None:
    """The argv is -c, one optional compression flag, -f, archive, sources."""
    options = TarCreateOptions(compression=compression)
    argv = _tar_create_argv(archive, sources, options)

    flag = _COMPRESSION_BY_MEMBER[compression]
    expected_head = ("-c",) if flag is None else ("-c", flag)
    assert argv == (*expected_head, "-f", archive, *sources), (
        "argv must be the documented head followed by archive and sources"
    )
    present = [entry for entry in argv if entry in _ALL_COMPRESSION_FLAGS]
    assert present == ([] if flag is None else [flag]), (
        "exactly one compression flag must appear, and only when requested"
    )


@settings(max_examples=100)
@given(archive=_ABS_PATHS, compression=_COMPRESSIONS)
def test_tar_create_rejects_invalid_source_collections(
    archive: str,
    compression: Compression,
) -> None:
    """Empty sources raise ValueError; a bare path raises TypeError."""
    options = TarCreateOptions(compression=compression)
    with pytest.raises(ValueError, match="at least one source"):
        _tar_create_argv(archive, [], options)
    # Deliberately defeat static typing: the contract under test is the
    # runtime rejection of a bare path where a sequence is required.
    bare_str = typ.cast("list[str]", archive)
    bare_path = typ.cast("list[str]", Path(archive))
    with pytest.raises(TypeError, match="sequence of source paths"):
        _tar_create_argv(archive, bare_str, options)
    with pytest.raises(TypeError, match="sequence of source paths"):
        _tar_create_argv(archive, bare_path, options)


@settings(max_examples=200)
@given(archive=_ABS_PATHS, destination=st.none() | _ABS_PATHS)
def test_tar_extract_argv_shape(archive: str, destination: str | None) -> None:
    """The argv is -x -f archive, with -C destination only when supplied."""
    argv = _tar_extract_argv(archive, destination, allow_relative=False)
    expected_tail = () if destination is None else ("-C", destination)
    assert argv == ("-x", "-f", archive, *expected_tail), (
        "argv must be -x -f <archive> with an optional -C <destination>"
    )


@settings(max_examples=200)
@given(source=_ABS_PATHS, destination=_ABS_PATHS, options=_RSYNC_OPTIONS)
def test_rsync_argv_flags_match_options_in_fixed_order(
    source: str,
    destination: str,
    options: RsyncOptions,
) -> None:
    """Exactly the enabled flags appear, in _FLAG_ORDER, then the paths."""
    argv = _rsync_argv(source, destination, options)
    expected_flags = tuple(flag for attr, flag in _FLAG_ORDER if getattr(options, attr))
    assert argv == (*expected_flags, source, destination), (
        "argv must be the enabled flags in fixed order, then source and destination"
    )


@settings(max_examples=200)
@given(
    archive=_ABS_PATHS,
    sources=st.lists(_ABS_PATHS, min_size=1, max_size=3),
    compression=_COMPRESSIONS,
)
def test_tar_path_conversion_is_deterministic_and_type_insensitive(
    archive: str,
    sources: list[str],
    compression: Compression,
) -> None:
    """Equal inputs give equal argv; Path and str inputs agree."""
    tar_options = TarCreateOptions(compression=compression)
    from_strings = _tar_create_argv(archive, sources, tar_options)
    assert from_strings == _tar_create_argv(archive, sources, tar_options), (
        "tar argv construction must be deterministic"
    )
    from_paths = _tar_create_argv(
        Path(archive),
        [Path(s) for s in sources],
        tar_options,
    )
    assert from_paths == from_strings, "Path inputs must match str inputs"


@settings(max_examples=200)
@given(source=_ABS_PATHS, destination=_ABS_PATHS, options=_RSYNC_OPTIONS)
def test_rsync_path_conversion_is_deterministic_and_type_insensitive(
    source: str,
    destination: str,
    options: RsyncOptions,
) -> None:
    """Equal inputs give equal argv; Path and str inputs agree."""
    rsync_from_strings = _rsync_argv(source, destination, options)
    assert rsync_from_strings == _rsync_argv(
        Path(source),
        Path(destination),
        options,
    ), "rsync argv must agree between Path and str inputs"


@settings(max_examples=50)
@given(
    archive=_ABS_PATHS,
    sources=st.lists(_ABS_PATHS, min_size=1, max_size=3),
    source=_ABS_PATHS,
    destination=_ABS_PATHS,
)
def test_wrappers_attach_curated_program_and_pure_argv(
    archive: str,
    sources: list[str],
    source: str,
    destination: str,
) -> None:
    """The sh.make wrappers carry the curated program and builder argv."""
    create_cmd = tar_create(archive, sources)
    assert create_cmd.program == TAR
    assert create_cmd.argv == _tar_create_argv(
        archive,
        sources,
        TarCreateOptions(),
    )

    extract_cmd = tar_extract(archive, destination=destination)
    assert extract_cmd.program == TAR
    assert extract_cmd.argv == _tar_extract_argv(
        archive,
        destination,
        allow_relative=False,
    )

    sync_cmd = rsync_sync(source, destination)
    assert sync_cmd.program == RSYNC
    assert sync_cmd.argv == _rsync_argv(source, destination, RsyncOptions())


@settings(max_examples=100)
@given(segments=st.lists(_SEGMENTS, min_size=1, max_size=3))
def test_relative_paths_require_explicit_opt_in(segments: list[str]) -> None:
    """Relative paths are rejected unless allow_relative is set."""
    relative = "/".join(segments)
    with pytest.raises(ValueError, match="absolute path"):
        _tar_extract_argv(relative, None, allow_relative=False)
    argv = _tar_extract_argv(relative, None, allow_relative=True)
    assert argv == ("-x", "-f", relative), (
        "allow_relative must accept the same path verbatim"
    )
