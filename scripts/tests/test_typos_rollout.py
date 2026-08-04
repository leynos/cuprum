"""Tests for the shared dictionary model, merging, and config rendering."""

from __future__ import annotations

import ast
import logging
import os
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


def test_rollout_scripts_support_python_313(script_directory: Path) -> None:
    """Every rollout script parses with the declared minimum Python version."""
    rollout_scripts = tuple(script_directory.glob("*.py"))
    assert rollout_scripts, "the rollout script directory must contain Python files"
    for script in rollout_scripts:
        ast.parse(
            script.read_text(encoding="utf-8"),
            filename=str(script),
            feature_version=(3, 13),
        )


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
    """An older local authority cannot replace a newer untracked cache."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "shared.toml"
    cache = tmp_path / ".typos-base.toml"
    metadata = tmp_path / ".typos-base.json"
    source.write_text(dictionary_text(), encoding="utf-8")
    source.touch()
    rollout.refresh_base(source, cache, metadata=metadata)
    cache.write_text(dictionary_text("newer"), encoding="utf-8")
    cache.touch()
    source_mtime = source.stat().st_mtime_ns
    cache_mtime = max(cache.stat().st_mtime_ns, source_mtime + 1)
    os.utime(cache, ns=(cache_mtime, cache_mtime))

    result = rollout.refresh_base(source, cache, metadata=metadata)

    assert result.status == "current", (
        "a newer cache must not be replaced by an older local authority"
    )
    assert rollout.load_dictionary(cache).stems == ("newer",)


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
        """Model an unavailable HTTPS authority."""
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
    assert fallback_record.error_type == "URLError", (
        "the fallback warning must classify the bounded refresh error type"
    )


@pytest.mark.parametrize(
    ("document_builder", "error_type", "match"),
    [
        pytest.param(_invalid_schema, ValueError, "schema", id="schema"),
        pytest.param(_invalid_oxford_table, TypeError, "oxford", id="oxford"),
        pytest.param(_invalid_stems, TypeError, "stems", id="stems"),
        pytest.param(
            _invalid_corrections,
            TypeError,
            "corrections",
            id="corrections",
        ),
    ],
)
def test_dictionary_validation_rejects_invalid_documents(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    document_builder: cabc.Callable[[cabc.Callable[..., str]], str],
    error_type: type[Exception],
    match: str,
) -> None:
    """Schema, table, string-list and correction types remain validated."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "base.toml"
    source.write_text(document_builder(dictionary_text), encoding="utf-8")

    with pytest.raises(error_type, match=match):
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
        ignore_patterns=("https?://",),
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
    assert tomllib.loads(first)["default"]["locale"] == "en-gb", (
        "the rendered document must be valid TOML with the en-gb locale"
    )
    assert list(output.parent.glob(".typos.toml.*")) == [], (
        "the atomic write must leave no temporary files behind"
    )
