"""Model, parse, and merge the shared en-GB-oxendict dictionary.

This module owns the dictionary data shape and its TOML parsing rules. It
depends only on the standard library so both the cache refresh
(``typos_rollout_refresh``) and the renderer (``typos_rollout``) can build on
it without importing one another.
"""

from __future__ import annotations

import dataclasses as dc
import tomllib
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

SCHEMA_VERSION = 1
SUFFIX_PAIRS = (
    ("ise", "ize"),
    ("ises", "izes"),
    ("ised", "ized"),
    ("ising", "izing"),
    ("iser", "izer"),
    ("isers", "izers"),
    ("isable", "izable"),
    ("isation", "ization"),
    ("isations", "izations"),
)


@dc.dataclass(frozen=True, slots=True)
class Dictionary:
    """Curated words and exclusions used to generate a ``typos`` config.

    This dataclass is frozen; instances are immutable once constructed.

    Attributes
    ----------
    stems : tuple[str, ...]
        Oxford stems expanded into ``-ise``/``-ize`` mappings.
    accepted : tuple[str, ...]
        Words accepted as-is.
    corrections : tuple[tuple[str, str], ...]
        Explicit ``(word, correction)`` pairs.
    ignore_patterns : tuple[str, ...]
        Regexes excluded from checking.
    excluded_files : tuple[str, ...]
        Path patterns excluded from checking.
    """

    stems: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    corrections: tuple[tuple[str, str], ...] = ()
    ignore_patterns: tuple[str, ...] = ()
    excluded_files: tuple[str, ...] = ()


def _string_list(table: cabc.Mapping[str, object], key: str) -> tuple[str, ...]:
    """Read and validate a list of strings from a TOML table."""
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = f"{key!r} must be a list of strings"
        raise TypeError(message)
    strings = typ.cast("list[str]", value)
    return tuple(sorted(set(strings)))


def _table(document: cabc.Mapping[str, object], key: str) -> cabc.Mapping[str, object]:
    """Read and validate a TOML table."""
    value = document.get(key, {})
    if not isinstance(value, dict):
        message = f"{key!r} must be a table"
        raise TypeError(message)
    return typ.cast("cabc.Mapping[str, object]", value)


def parse_dictionary_text(text: str) -> Dictionary:
    """Parse and validate shared dictionary text.

    Parameters
    ----------
    text : str
        The shared dictionary TOML document.

    Returns
    -------
    Dictionary
        The parsed and validated dictionary.

    Raises
    ------
    ValueError
        If the document's ``schema`` is not the supported
        ``SCHEMA_VERSION``.
    TypeError
        If a table, string list, or the corrections mapping has the
        wrong type.
    tomllib.TOMLDecodeError
        If *text* is not valid TOML.
    """  # ruff: ignore[docstring-extraneous-exception] - TOMLDecodeError and helper TypeErrors propagate
    document = tomllib.loads(text)
    schema = document.get("schema")
    if schema != SCHEMA_VERSION:
        message = f"unsupported dictionary schema {schema!r}"
        raise ValueError(message)
    oxford = _table(document, "oxford")
    words = _table(document, "words")
    patterns = _table(document, "patterns")
    files = _table(document, "files")
    corrections_table = _table(words, "corrections")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in corrections_table.items()
    ):
        message = "word corrections must map strings to strings"
        raise TypeError(message)
    corrections = typ.cast("cabc.Mapping[str, str]", corrections_table)
    return Dictionary(
        stems=_string_list(oxford, "stems"),
        accepted=_string_list(words, "accepted"),
        corrections=tuple(sorted(corrections.items())),
        ignore_patterns=_string_list(patterns, "ignore"),
        excluded_files=_string_list(files, "exclude"),
    )


def load_dictionary(path: pathlib.Path) -> Dictionary:
    """Load a validated shared dictionary from *path*.

    Parameters
    ----------
    path : pathlib.Path
        The dictionary file, read as UTF-8.

    Returns
    -------
    Dictionary
        The parsed dictionary read from *path*.

    Raises
    ------
    OSError
        If the file cannot be read.
    UnicodeDecodeError
        If the file is not valid UTF-8.
    ValueError, TypeError, tomllib.TOMLDecodeError
        If the document fails validation.
    """  # ruff: ignore[docstring-extraneous-exception] - read and parse errors propagate from the callees
    return parse_dictionary_text(path.read_text(encoding="utf-8"))


def merge_dictionaries(base: Dictionary, local: Dictionary) -> Dictionary:
    """Merge a shared dictionary with a non-conflicting local overlay.

    Parameters
    ----------
    base : Dictionary
        The shared, estate-wide dictionary.
    local : Dictionary
        The repository-local overlay.

    Returns
    -------
    Dictionary
        The merged dictionary.

    Raises
    ------
    ValueError
        If the overlay defines a correction that conflicts with the base.
    """
    corrections = dict(base.corrections)
    for word, correction in local.corrections:
        existing = corrections.get(word)
        if existing is not None and existing != correction:
            message = (
                f"conflicting correction for {word!r}: {existing!r} != {correction!r}"
            )
            raise ValueError(message)
        corrections[word] = correction
    return Dictionary(
        stems=tuple(sorted(set(base.stems) | set(local.stems))),
        accepted=tuple(sorted(set(base.accepted) | set(local.accepted))),
        corrections=tuple(sorted(corrections.items())),
        ignore_patterns=tuple(
            sorted(set(base.ignore_patterns) | set(local.ignore_patterns))
        ),
        excluded_files=tuple(
            sorted(set(base.excluded_files) | set(local.excluded_files))
        ),
    )


def generate_word_mappings(dictionary: Dictionary) -> dict[str, str]:
    """Expand Oxford stems and explicit words into deterministic mappings.

    Parameters
    ----------
    dictionary : Dictionary
        The merged dictionary whose stems and words are expanded.

    Returns
    -------
    dict[str, str]
        A sorted mapping of source word to its correction.

    Raises
    ------
    ValueError
        If an expanded stem or explicit correction conflicts with an
        existing mapping.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError is raised by the nested ``add`` helper
    mappings = {word: word for word in dictionary.accepted}

    def add(word: str, correction: str) -> None:
        existing = mappings.get(word)
        if existing is not None and existing != correction:
            message = (
                f"conflicting generated correction for {word!r}: "
                f"{existing!r} != {correction!r}"
            )
            raise ValueError(message)
        mappings[word] = correction

    for word, correction in dictionary.corrections:
        add(word, correction)
    for stem in dictionary.stems:
        for plain_british, oxford in SUFFIX_PAIRS:
            add(f"{stem}{plain_british}", f"{stem}{oxford}")
            add(f"{stem}{oxford}", f"{stem}{oxford}")
    return dict(sorted(mappings.items()))
