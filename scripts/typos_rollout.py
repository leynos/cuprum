"""Render shared en-GB-oxendict ``typos`` configuration.

This module owns the rendering step and re-exports the public rollout API so
callers keep a single entry point. The supporting concerns live in dedicated
modules to keep each file focused: ``typos_rollout_dictionary`` models and
parses the shared dictionary, ``typos_rollout_refresh`` refreshes the untracked
cache from a local or HTTPS authority, and ``typos_rollout_cache`` provides the
cache types and atomic writes.
"""

from __future__ import annotations

import json
import tomllib
import typing as typ

from typos_rollout_cache import atomic_write as _atomic_write
from typos_rollout_dictionary import (
    SCHEMA_VERSION,
    SUFFIX_PAIRS,
    Dictionary,
    generate_word_mappings,
    load_dictionary,
    merge_dictionaries,
)
from typos_rollout_refresh import RefreshResult, refresh_base

if typ.TYPE_CHECKING:
    import pathlib

# The façade's public surface: the aliases scripts and tests consume.
# Private helpers such as ``_atomic_write`` stay unexported.
__all__ = [
    "SCHEMA_VERSION",
    "SUFFIX_PAIRS",
    "Dictionary",
    "RefreshResult",
    "generate_word_mappings",
    "load_dictionary",
    "merge_dictionaries",
    "refresh_base",
    "render_typos_config",
    "write_config",
]

_MARKDOWN_IGNORE_PATTERNS = frozenset((r"`[^`\n]+`", r"(?s)```.*?```"))


def _toml_string(value: str) -> str:
    """Render a string using TOML-compatible JSON quoting."""
    return json.dumps(value, ensure_ascii=False)


def _render_array(name: str, values: tuple[str, ...]) -> list[str]:
    """Render a deterministic TOML string array."""
    lines = [f"{name} = ["]
    lines.extend(f"    {_toml_string(value)}," for value in values)
    lines.append("]")
    return lines


def render_typos_config(dictionary: Dictionary) -> str:
    """Render a deterministic, parse-checked ``typos.toml`` document.

    Parameters
    ----------
    dictionary : Dictionary
        Shared spelling policy to render.

    Returns
    -------
    str
        Newline-terminated TOML validated with :func:`tomllib.loads` before
        it is returned.

    Raises
    ------
    tomllib.TOMLDecodeError
        If the rendered document fails the ``tomllib.loads`` parse check.
    """  # ruff: ignore[docstring-extraneous-exception] - TOMLDecodeError propagates from tomllib.loads
    global_patterns = tuple(
        sorted(
            pattern
            for pattern in dictionary.ignore_patterns
            if pattern not in _MARKDOWN_IGNORE_PATTERNS
        )
    )
    markdown_patterns = tuple(
        sorted(
            pattern
            for pattern in dictionary.ignore_patterns
            if pattern in _MARKDOWN_IGNORE_PATTERNS
        )
    )
    lines = [
        "# Generated from the shared en-GB-oxendict dictionary.",
        "# Regenerate with scripts/generate_typos_config.py; do not edit by hand.",
        "",
        "[files]",
        *_render_array("extend-exclude", tuple(sorted(dictionary.excluded_files))),
        "",
        "[default]",
        'locale = "en-gb"',
        *_render_array("extend-ignore-re", global_patterns),
        "",
        "[type.markdown]",
        *_render_array("extend-glob", ("*.md",)),
        *_render_array("extend-ignore-re", markdown_patterns),
        "",
        "[default.extend-words]",
    ]
    lines.extend(
        f"{_toml_string(word)} = {_toml_string(correction)}"
        for word, correction in generate_word_mappings(dictionary).items()
    )
    rendered = "\n".join(lines) + "\n"
    tomllib.loads(rendered)
    return rendered


def write_config(path: pathlib.Path, dictionary: Dictionary) -> None:
    """Atomically write validated generated configuration to *path*.

    Parameters
    ----------
    path : pathlib.Path
        Destination configuration path.
    dictionary : Dictionary
        Shared spelling policy to render and validate before writing.

    Raises
    ------
    tomllib.TOMLDecodeError
        If the rendered document fails the ``tomllib.loads`` parse check
        performed by ``render_typos_config``.
    OSError
        If the atomic write to *path* fails.
    """  # ruff: ignore[docstring-extraneous-exception] - render and write errors propagate from the callees
    _atomic_write(path, render_typos_config(dictionary).encode())
