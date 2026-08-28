"""Property tests for deterministic, scoped ``typos.toml`` rendering."""

from __future__ import annotations

import dataclasses as dc
import tomllib
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    import types

_MARKDOWN_IGNORE_PATTERNS = (r"`[^`\n]+`", r"(?s)```.*?```")
_STEM_ALPHABET = "abcde"
_ACCEPTED_ALPHABET = "mno"
_CORRECTION_ALPHABET = "qwx"


def _word_boundary_pattern(token: str) -> str:
    """Wrap a generated token as an ordinary global regex."""
    return rf"\b{token}\b"


def _correction_items(
    corrections: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Preserve the generated correction insertion order as pairs."""
    return tuple(corrections.items())


@dc.dataclass(frozen=True, slots=True)
class _RenderCase:
    """Define one unordered logical spelling dictionary."""

    stems: tuple[str, ...]
    accepted: tuple[str, ...]
    corrections: tuple[tuple[str, str], ...]
    global_patterns: tuple[str, ...]
    markdown_patterns: tuple[str, ...]
    excluded_files: tuple[str, ...]


_stems = st.lists(
    st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
    min_size=2,
    max_size=5,
    unique=True,
).map(tuple)
_accepted = st.lists(
    st.text(alphabet=_ACCEPTED_ALPHABET, min_size=1, max_size=5),
    max_size=5,
    unique=True,
).map(tuple)
_corrections = st.dictionaries(
    st.text(alphabet=_CORRECTION_ALPHABET, min_size=1, max_size=5),
    st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
    max_size=5,
).map(_correction_items)
_global_patterns = st.lists(
    st.text(alphabet="abcdef", min_size=1, max_size=5).map(_word_boundary_pattern),
    max_size=4,
    unique=True,
).map(tuple)
_markdown_patterns = st.lists(
    st.sampled_from(_MARKDOWN_IGNORE_PATTERNS),
    max_size=len(_MARKDOWN_IGNORE_PATTERNS),
    unique=True,
).map(tuple)
_excluded_files = st.lists(
    st.text(alphabet="abcdef0123456789/_-.", min_size=1, max_size=10),
    max_size=4,
    unique=True,
).map(tuple)
_render_cases = st.builds(
    _RenderCase,
    stems=_stems,
    accepted=_accepted,
    corrections=_corrections,
    global_patterns=_global_patterns,
    markdown_patterns=_markdown_patterns,
    excluded_files=_excluded_files,
)
_shared_settings = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _ordered(values: tuple[str, ...], *, should_reverse: bool) -> tuple[str, ...]:
    """Return either generated input order or its distinct reverse."""
    if should_reverse:
        return tuple(reversed(values))
    return values


def _ordered_corrections(
    corrections: tuple[tuple[str, str], ...],
    *,
    should_reverse: bool,
) -> tuple[tuple[str, str], ...]:
    """Return correction pairs in one of two equivalent input orders."""
    if should_reverse:
        return tuple(reversed(corrections))
    return corrections


def _dictionary_with_order(
    rollout: types.ModuleType,
    case: _RenderCase,
    *,
    should_reverse: bool,
) -> object:
    """Build one valid dictionary ordering from the same logical contents."""
    return rollout.Dictionary(
        stems=_ordered(case.stems, should_reverse=should_reverse),
        accepted=_ordered(case.accepted, should_reverse=should_reverse),
        corrections=_ordered_corrections(
            case.corrections,
            should_reverse=should_reverse,
        ),
        ignore_patterns=_ordered(
            case.global_patterns + case.markdown_patterns,
            should_reverse=should_reverse,
        ),
        excluded_files=_ordered(case.excluded_files, should_reverse=should_reverse),
    )


@given(case=_render_cases)
@_shared_settings
def test_render_is_canonical_and_scopes_markdown_patterns(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    case: _RenderCase,
) -> None:
    """Rendering ignores input order and scopes Markdown-only exceptions."""
    _, rollout, _ = rollout_modules
    first_dictionary = _dictionary_with_order(rollout, case, should_reverse=False)

    first = rollout.render_typos_config(first_dictionary)
    second = rollout.render_typos_config(
        _dictionary_with_order(rollout, case, should_reverse=True)
    )

    assert first == second, "equivalent dictionaries must render byte-for-byte alike"
    assert first.endswith("\n"), (
        "every rendered configuration must have one trailing newline"
    )
    assert not first.endswith("\n\n"), (
        "every rendered configuration must have exactly one trailing newline"
    )
    assert second.endswith("\n"), (
        "every rendered configuration must have one trailing newline"
    )
    assert not second.endswith("\n\n"), (
        "every rendered configuration must have exactly one trailing newline"
    )

    config = tomllib.loads(first)

    assert config == tomllib.loads(second), (
        "equivalent rendered documents must parse to the same TOML structure"
    )
    assert config["files"]["extend-exclude"] == sorted(case.excluded_files), (
        "excluded files must retain all generated values in canonical order"
    )

    default = config["default"]
    markdown = config["type"]["markdown"]

    assert markdown["extend-glob"] == ["*.md"], (
        "Markdown-only patterns must apply only to Markdown files"
    )
    assert markdown["extend-ignore-re"] == sorted(case.markdown_patterns), (
        "the Markdown scope must contain exactly the generated Markdown patterns"
    )
    assert default["extend-ignore-re"] == sorted(case.global_patterns), (
        "the default scope must contain exactly the generated global patterns"
    )
    assert not set(default["extend-ignore-re"]) & set(_MARKDOWN_IGNORE_PATTERNS), (
        "Markdown-only patterns must never appear in the default scope"
    )

    mappings = rollout.generate_word_mappings(first_dictionary)
    rendered_mappings = default["extend-words"]

    assert rendered_mappings == mappings, (
        "the rendered word table must preserve the complete generated mapping"
    )
    for word, correction in case.corrections:
        assert rendered_mappings[word] == correction, (
            f"explicit correction {word!r} must remain {correction!r}"
        )
    for stem in case.stems:
        for plain_british, oxford in rollout.SUFFIX_PAIRS:
            assert rendered_mappings[f"{stem}{plain_british}"] == f"{stem}{oxford}", (
                f"the plain-British expansion of {stem!r} must use Oxford spelling"
            )
            assert rendered_mappings[f"{stem}{oxford}"] == f"{stem}{oxford}", (
                f"the Oxford expansion of {stem!r} must remain unchanged"
            )
    for word in case.accepted:
        assert rendered_mappings[word] == word, (
            f"accepted word {word!r} must remain an identity mapping"
        )
