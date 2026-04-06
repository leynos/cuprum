"""Shared helpers for documentation contract testing."""

from __future__ import annotations

from pathlib import Path

# -- Section heading constants ------------------------------------------------

# These constants ensure that heading renames only need to be updated in one place
BUILD_PREREQUISITES_HEADING = "Build prerequisites for native extensions"
TROUBLESHOOTING_HEADING = "Troubleshooting"


# -- Documentation file access ------------------------------------------------


def repo_root() -> Path:
    """Return the repository root for documentation lookups.

    This centralizes the path calculation logic used across all doc tests.
    """
    # tests/helpers/docs.py -> tests/helpers -> tests -> repo root
    return Path(__file__).resolve().parents[2]


def read_doc(relative_path: str) -> str:
    """Read a documentation file as UTF-8 text.

    Args:
        relative_path: Path relative to the repository root.

    Returns
    -------
        The file contents as a string.
    """
    return (repo_root() / relative_path).read_text(encoding="utf-8")


def read_users_guide() -> str:
    """Read the users' guide documentation file.

    Returns
    -------
        The users' guide as a string.
    """
    return read_doc("docs/users-guide.md")


# -- Case-insensitive assertion helpers ---------------------------------------


def contains_case_insensitive(text: str, substring: str) -> bool:
    """Check if text contains substring, ignoring case.

    Args:
        text: The text to search in.
        substring: The substring to search for.

    Returns
    -------
        True if substring is found (case-insensitive), False otherwise.
    """
    return substring.lower() in text.lower()
