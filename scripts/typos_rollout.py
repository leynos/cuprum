"""Refresh and render shared en-GB-oxendict ``typos`` configuration."""

from __future__ import annotations

import json
import tomllib
import typing as typ

import typos_rollout_cache
import typos_rollout_dictionary
import typos_rollout_refresh

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

_atomic_write = typos_rollout_cache.atomic_write

SCHEMA_VERSION = typos_rollout_dictionary.SCHEMA_VERSION
SUFFIX_PAIRS = typos_rollout_dictionary.SUFFIX_PAIRS
Dictionary = typos_rollout_dictionary.Dictionary
load_dictionary = typos_rollout_dictionary.load_dictionary
merge_dictionaries = typos_rollout_dictionary.merge_dictionaries
generate_word_mappings = typos_rollout_dictionary.generate_word_mappings

RefreshResult = typos_rollout_refresh.RefreshResult
refresh_base = typos_rollout_refresh.refresh_base


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
    """
    lines = [
        "# Generated from the shared en-GB-oxendict dictionary.",
        "# Regenerate with scripts/generate_typos_config.py; do not edit by hand.",
        "",
        "[files]",
        *_render_array("extend-exclude", dictionary.excluded_files),
        "",
        "[default]",
        'locale = "en-gb"',
        *_render_array("extend-ignore-re", dictionary.ignore_patterns),
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

    Returns
    -------
    None
        The validated configuration is atomically published at *path*.
    """
    _atomic_write(path, render_typos_config(dictionary).encode())
