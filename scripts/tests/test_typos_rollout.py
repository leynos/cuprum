"""Tests for the shared dictionary model, merging, and config rendering."""
from __future__ import annotations

import ast
import dataclasses as dc
import logging
import re
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


# The package baseline from ``pyproject.toml``. A helper script may raise its
# own floor with a PEP 723 script block; anything without one is held here.
_PACKAGE_BASELINE = (3, 12)
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
_MINIMUM_VERSION = re.compile(r">=\s*(\d+)\.(\d+)")


def _declared_baseline(source: str) -> tuple[int, int]:
    """Return a script's own Python floor, or the package baseline."""
    for match in _PEP723_BLOCK.finditer(source):
        if match.group("type") != "script":
            continue
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in match.group("content").splitlines(keepends=True)
        )
        requires = tomllib.loads(content).get("requires-python")
        if isinstance(requires, str) and (found := _MINIMUM_VERSION.search(requires)):
            return (int(found.group(1)), int(found.group(2)))
    return _PACKAGE_BASELINE


def test_rollout_scripts_parse_at_their_declared_baseline(
    script_directory: Path,
) -> None:
    """Every rollout script parses at the Python floor it is held to.

    Scripts without a PEP 723 block must stay on the package baseline; one that
    declares its own ``requires-python`` is parsed at that version instead, so
    raising a script's floor is a deliberate, visible act rather than a silent
    consequence of the grammar the test happens to use.
    """
    rollout_scripts = tuple(script_directory.glob("*.py"))
    assert rollout_scripts, "the rollout script directory must contain Python files"
    for script in rollout_scripts:
        source = script.read_text(encoding="utf-8")
        ast.parse(
            source,
            filename=str(script),
            feature_version=_declared_baseline(source),
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
    assert fallback_record.error_type == "URLError", (
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

def test_local_policy_preserves_inline_code_exemption(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    script_directory: Path,
) -> None:
    """The generated config retains the repository's inline-code policy."""
    _, _, generator = rollout_modules
    (tmp_path / ".typos-oxendict-base.toml").write_text(
        dictionary_text(), encoding="utf-8"
    )
    (tmp_path / "typos.local.toml").write_text(
        (script_directory.parent / "typos.local.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    config = tomllib.loads(generator.render_config(tmp_path))

    assert "`[^`\\n]+`" in config["default"]["extend-ignore-re"], (
        "repository inline-code exemption must survive generated configuration"
    )


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
