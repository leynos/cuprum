"""Tests for the Dependabot directory-glob helper."""

from __future__ import annotations

import pytest

from tests.helpers.dependabot_directories import (
    COMPOSITE_ACTIONS,
    directory_glob_matches,
)


@pytest.mark.parametrize(
    ("glob", "directory", "expected"),
    [
        ("/", "/", True),
        (COMPOSITE_ACTIONS, "/.github/actions/cache-keys", True),
        (COMPOSITE_ACTIONS, "/.github/actions/release/sign", False),
        ("/.github/actions/**", "/.github/actions/release/sign", True),
        ("/.github/actions/lint", "/.github/actions/lints", False),
    ],
)
def test_directory_globs_match_like_dependabot(
    glob: str, directory: str, *, expected: bool
) -> None:
    """``*`` stays within one path segment; ``**`` spans segments."""
    assert directory_glob_matches(glob, directory) is expected, (
        f"{glob!r} should {'cover' if expected else 'not cover'} {directory!r}"
    )
