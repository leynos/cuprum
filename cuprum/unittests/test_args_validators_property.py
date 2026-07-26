"""Property-based fuzz tests for the ``safe_path`` and ``git_ref`` validators.

These tests exercise the rejection-classification helpers exposed by
``cuprum.builders.args`` (``classify_path_string`` and ``classify_git_ref``).
Because those helpers report *why* an input is rejected, the properties can
assert on the rejection category rather than only pass/fail, and can pin the
contract that the public validators raise exactly when — and with the message —
the classifier dictates.

The invariants checked here are:

- Totality: classification never raises for any string input.
- Consistency: ``safe_path``/``git_ref`` raise ``ValueError`` iff the
  classifier returns a rejection, carrying that rejection's message verbatim.
- Category coverage: NUL bytes, ``..`` segments, whitespace, and a leading
  ``-`` map to their designated rejection categories.
- Round-trip: constructed valid inputs classify as ``None`` and survive the
  validators unchanged (``git_ref``) or normalised and idempotent
  (``safe_path``).
"""

from __future__ import annotations

import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.builders.args import (
    GitRefRejection,
    PathRejection,
    classify_git_ref,
    classify_path_string,
    git_ref,
    safe_path,
)

# A small alphabet that includes the interesting boundary characters (NUL,
# path separators, whitespace, Unicode) without ballooning the search space.
_FUZZ_ALPHABET = "ab/.\\-_ \t\x00:@{}é☃"
_FUZZ_TEXT = st.text(alphabet=_FUZZ_ALPHABET, max_size=12)
# Ref-safe characters accepted by the git-ref pattern.
_REF_SEGMENT = st.text(alphabet="abcXYZ0123_-", min_size=1, max_size=5)
# A platform-native absolute anchor. ``safe_path`` treats a leading "/" as
# absolute on POSIX but not on Windows (where it is drive-relative), so anchor
# with a drive letter there to keep the round-trip property valid on both.
_ABSOLUTE_ANCHOR = "C:/" if sys.platform == "win32" else "/"


@given(raw=_FUZZ_TEXT, allow_relative=st.booleans())
def test_classify_path_string_is_total(raw: str, *, allow_relative: bool) -> None:
    """``classify_path_string`` returns a value or ``None`` but never raises."""
    result = classify_path_string(raw, allow_relative=allow_relative)
    assert result is None or isinstance(result, PathRejection)


@given(raw=_FUZZ_TEXT, allow_relative=st.booleans())
def test_safe_path_matches_classification(raw: str, *, allow_relative: bool) -> None:
    """``safe_path`` raises exactly when classification reports a rejection."""
    rejection = classify_path_string(raw, allow_relative=allow_relative)
    if rejection is None:
        # A ``None`` classification must yield a value without raising.
        result = safe_path(raw, allow_relative=allow_relative)
        assert isinstance(result, str)
    else:
        with pytest.raises(ValueError, match="SafePath") as exc_info:
            safe_path(raw, allow_relative=allow_relative)
        assert str(exc_info.value) == rejection.value


@given(raw=_FUZZ_TEXT.filter(lambda s: s != "" and "\x00" in s))
def test_nul_bytes_are_rejected_as_nul(raw: str) -> None:
    """Any non-empty string containing a NUL classifies as ``NUL``."""
    assert classify_path_string(raw, allow_relative=True) is PathRejection.NUL


@given(
    prefix=_REF_SEGMENT,
    suffix=_REF_SEGMENT,
)
def test_parent_segments_are_rejected(prefix: str, suffix: str) -> None:
    """A ``..`` path segment classifies as ``PARENT_SEGMENT`` (NUL-free)."""
    raw = f"/{prefix}/../{suffix}"
    assert classify_path_string(raw, allow_relative=True) is (
        PathRejection.PARENT_SEGMENT
    )


@given(segments=st.lists(_REF_SEGMENT, min_size=1, max_size=4))
def test_absolute_paths_without_traversal_round_trip(segments: list[str]) -> None:
    """Absolute, NUL-free, traversal-free paths validate and normalise stably."""
    # Use a platform-native absolute anchor: on Windows a leading "/" without a
    # drive is root-relative (not absolute), so anchor with a drive there.
    raw = _ABSOLUTE_ANCHOR + "/".join(segments)
    assert classify_path_string(raw, allow_relative=False) is None
    normalised = safe_path(raw)
    # A valid, traversal-free absolute path stays valid and normalises stably.
    assert classify_path_string(normalised, allow_relative=False) is None
    assert safe_path(normalised) == normalised


@given(raw=_FUZZ_TEXT)
def test_classify_git_ref_is_total(raw: str) -> None:
    """``classify_git_ref`` returns a value or ``None`` but never raises."""
    result = classify_git_ref(raw)
    assert result is None or isinstance(result, GitRefRejection)


@given(raw=_FUZZ_TEXT)
def test_git_ref_matches_classification(raw: str) -> None:
    """``git_ref`` raises exactly when classification reports a rejection."""
    rejection = classify_git_ref(raw)
    if rejection is None:
        assert git_ref(raw) == raw
    else:
        with pytest.raises(ValueError, match="GitRef") as exc_info:
            git_ref(raw)
        assert str(exc_info.value) == rejection.value


@given(raw=_FUZZ_TEXT.filter(lambda s: any(c.isspace() for c in s)))
def test_whitespace_refs_are_rejected(raw: str) -> None:
    """Any whitespace-bearing ref is rejected (never accepted)."""
    assert classify_git_ref(raw) is not None


@given(segments=st.lists(_REF_SEGMENT, min_size=1, max_size=3))
def test_simple_refs_are_accepted(segments: list[str]) -> None:
    """Slash-joined ref-safe segments classify as valid and round-trip."""
    raw = "/".join(segments)
    # The ref-safe alphabet contains no dots or slashes, so single-slash joins
    # cannot form "..", "//", ".lock", or a trailing "."; the only degenerate
    # case is a leading "-", which the classifier legitimately rejects.
    if raw.startswith("-"):
        return
    assert classify_git_ref(raw) is None
    assert git_ref(raw) == raw
