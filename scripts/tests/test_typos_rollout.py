"""Tests for the shared dictionary model, merging, and config rendering."""

from __future__ import annotations

import dataclasses as dc
import logging
import tomllib
import typing as typ
import urllib.error

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types
    from pathlib import Path


def _invalid_schema(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary with an unsupported schema version."""
    return dictionary_text().replace("schema = 1", "schema = 2")


def _invalid_oxford_table(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary whose Oxford section is not a table."""
    return dictionary_text().replace('[oxford]\nstems = ["organ"]', 'oxford = "bad"')


def _invalid_stems(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary containing a non-string stem."""
    return dictionary_text().replace('stems = ["organ"]', "stems = [1]")


def _invalid_corrections(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary containing a non-string correction."""
    return dictionary_text().replace(
        "[words.corrections]", "[words.corrections]\nteh = 1"
    )


@dc.dataclass(frozen=True, slots=True)
class _InvalidDictionaryCase:
    """Describe one invalid shared-dictionary document and its rejection.

    Attributes
    ----------
    document_builder : cabc.Callable[[cabc.Callable[..., str]], str]
        Builder that derives the invalid document from the valid fixture.
    error_type : type[Exception]
        The exception ``load_dictionary`` must raise for the document.
    match : str
        Regular expression the raised message must match.
    """

    document_builder: cabc.Callable[[cabc.Callable[..., str]], str]
    error_type: type[Exception]
    match: str


def test_rollout_generates_oxford_corrections(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """The shared renderer accepts Oxford forms and corrects plain-British ones."""
    _, rollout, _ = rollout_modules

    mappings = rollout.generate_word_mappings(rollout.Dictionary(stems=("organ",)))

    assert mappings["organize"] == "organize", "an Oxford spelling must map to itself"
    assert mappings["organise"] == "organize", (
        "a plain-British spelling must map to its Oxford form"
    )


def test_local_refresh_keeps_a_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """An unchanged local authority leaves a locally edited cache in place.

    Freshness is decided by the metadata sidecar (recorded source path and
    mtime), not by the cache file's own mtime, so no timestamp manipulation
    is needed to exercise the contract.
    """
    _, rollout, _ = rollout_modules
    source = tmp_path / "shared.toml"
    cache = tmp_path / ".typos-base.toml"
    metadata = tmp_path / ".typos-base.json"
    source.write_text(dictionary_text(), encoding="utf-8")
    rollout.refresh_base(source, cache, metadata=metadata)
    cache.write_text(dictionary_text("newer"), encoding="utf-8")

    result = rollout.refresh_base(source, cache, metadata=metadata)

    assert result.status == "current", (
        "metadata recording an unchanged source must keep the cache current"
    )
    assert rollout.load_dictionary(cache).stems == ("newer",), (
        "a current cache must retain its locally edited contents"
    )


def test_https_failure_reuses_valid_tracked_config(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean network-restricted checkout retains its reviewed policy."""
    _, rollout, generator = rollout_modules
    tracked_config = tmp_path / "typos.toml"
    tracked_config.write_text('[default]\nlocale = "en-gb"\n', encoding="utf-8")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable HTTPS authority that always raises ``URLError``."""
        message = "offline"
        raise urllib.error.URLError(message)

    monkeypatch.setattr(rollout, "refresh_base", unavailable)

    with caplog.at_level(logging.WARNING):
        result = generator.main(
            repository=tmp_path, source="https://example.invalid/base"
        )

    assert result.status == "tracked-config", (
        "an unreachable HTTPS authority must fall back to the tracked config"
    )
    assert result.cache == tracked_config, (
        "the fallback must point at the tracked configuration file"
    )
    fallback_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "typos_rollout.tracked_config_fallback"
    )
    assert getattr(fallback_record, "error_type", None) == "URLError", (
        "the fallback warning must classify the bounded refresh error type"
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_schema,
                error_type=ValueError,
                match="schema",
            ),
            id="schema",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_oxford_table,
                error_type=TypeError,
                match="oxford",
            ),
            id="oxford",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_stems,
                error_type=TypeError,
                match="stems",
            ),
            id="stems",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_corrections,
                error_type=TypeError,
                match="corrections",
            ),
            id="corrections",
        ),
    ],
)
def test_dictionary_validation_rejects_invalid_documents(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    case: _InvalidDictionaryCase,
) -> None:
    """Schema, table, string-list and correction types remain validated."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "base.toml"
    source.write_text(case.document_builder(dictionary_text), encoding="utf-8")

    with pytest.raises(case.error_type, match=case.match):
        rollout.load_dictionary(source)


def test_merge_rejects_conflicting_corrections(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """A local overlay cannot silently weaken a shared correction."""
    _, rollout, _ = rollout_modules
    base = rollout.Dictionary(corrections=(("teh", "the"),))
    local = rollout.Dictionary(corrections=(("teh", "ten"),))

    with pytest.raises(ValueError, match="conflicting correction"):
        rollout.merge_dictionaries(base, local)


def test_render_and_write_are_deterministic_valid_toml(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Rendering is stable, parseable and atomically installed."""
    _, rollout, _ = rollout_modules
    dictionary = rollout.Dictionary(
        stems=("organ",),
        accepted=("proper-name",),
        ignore_patterns=("https?://", r"`[^`\n]+`", r"(?s)```.*?```"),
        excluded_files=("target",),
    )
    output = tmp_path / "nested" / "typos.toml"

    first = rollout.render_typos_config(dictionary)
    rollout.write_config(output, dictionary)

    assert first == rollout.render_typos_config(dictionary), (
        "rendering must be deterministic across repeated calls"
    )
    assert output.read_text(encoding="utf-8") == first, (
        "the written file must match the rendered document"
    )
    rendered_config = tomllib.loads(first)
    assert rendered_config["default"]["locale"] == "en-gb", (
        "the global locale must stay en-gb"
    )
    assert rendered_config["default"]["extend-ignore-re"] == ["https?://"], (
        "only non-Markdown patterns belong in the global scope"
    )
    assert rendered_config["type"]["markdown"]["extend-glob"] == ["*.md"], (
        "the Markdown type must be scoped to *.md"
    )
    assert rendered_config["type"]["markdown"]["extend-ignore-re"] == [
        r"(?s)```.*?```",
        r"`[^`\n]+`",
    ], "code-span and fenced-block patterns must be Markdown-only"
    assert not list(output.parent.glob(".typos.toml.*")), (
        "the atomic write must leave no temporary files behind"
    )
