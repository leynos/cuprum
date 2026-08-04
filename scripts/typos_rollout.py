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

import typos_rollout_cache
import typos_rollout_dictionary
import typos_rollout_refresh

if typ.TYPE_CHECKING:
    import pathlib

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
        The merged dictionary rendered as a ``typos.toml`` configuration.

    Returns
    -------
    str
        The rendered configuration document.

    Raises
    ------
    tomllib.TOMLDecodeError
        If the rendered document fails the ``tomllib.loads`` parse check.
    """  # noqa: DOC502 - TOMLDecodeError propagates from tomllib.loads
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
        Destination path the rendered configuration is written to.
    dictionary : Dictionary
        The merged dictionary rendered into the configuration.

    Raises
    ------
    tomllib.TOMLDecodeError
        If the rendered document fails the ``tomllib.loads`` parse check
        performed by ``render_typos_config``.
    OSError
        If the atomic write to *path* fails.
    """  # noqa: DOC502 - render and write errors propagate from the callees
    _atomic_write(path, render_typos_config(dictionary).encode())
