"""Tests for the shared dictionary model, merging, and config rendering."""

from __future__ import annotations

import dataclasses as dc
import hashlib
import logging
import tomllib
import typing as typ
import urllib.error
import urllib.request
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types


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


@dc.dataclass(frozen=True, slots=True)
class _PinnedRefreshSetup:
    """Capture the observable boundaries of one pinned shared-base refresh."""

    generator: types.ModuleType
    requests: list[urllib.request.Request]
    timeouts: list[float]
    verified_caches: list[Path]


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


def test_default_source_pins_the_recorded_shared_baseline(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """The default source cannot silently resume following the shared main branch."""
    _, _, generator = rollout_modules

    expected_revision = "64bd9ce54942562cd89252b66fcedf5683324a78"
    expected_hash = "7eb3d405d49d466f918d189a671afc708fab26f982845ea82be3b1d377166b6a"
    expected_url = (
        "https://raw.githubusercontent.com/leynos/agent-helper-scripts/"
        f"{expected_revision}/data/typos-oxendict-base.toml"
    )

    assert expected_revision == generator.PINNED_BASE_REVISION, (
        "the generator must retain this run's recorded shared revision"
    )
    assert expected_hash == generator.PINNED_BASE_SHA256, (
        "the generator must retain this run's recorded shared content hash"
    )
    assert expected_url == generator.DEFAULT_BASE_URL, (
        "the default shared dictionary URL must select the recorded commit"
    )


@pytest.fixture(name="pinned_refresh_setup")
def pinned_refresh_setup_fixture(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
    dictionary_text: cabc.Callable[..., str],
) -> _PinnedRefreshSetup:
    """Set up a fresh pinned refresh and retain its observable boundaries."""
    _, _, generator = rollout_modules
    setup = _PinnedRefreshSetup(generator, [], [], [])

    class Response:
        """Return a small valid dictionary through the HTTPS test boundary."""

        status = 200
        headers: typ.ClassVar[dict[str, str]] = {"ETag": '"pinned-base"'}

        def read(self, limit: int | None = None) -> bytes:
            """Return the valid test dictionary, respecting the read limit."""
            return dictionary_text().encode()[:limit]

        def __enter__(self) -> Response:
            """Enter the response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the response context without suppressing failures."""

    def open_response(request: urllib.request.Request, *, timeout: float) -> Response:
        """Capture the immutable authority request and return valid content."""
        setup.requests.append(request)
        setup.timeouts.append(timeout)
        return Response()

    patch_https_opener(open_response)
    monkeypatch.setattr(
        generator,
        "_verify_pinned_base",
        setup.verified_caches.append,
    )
    return setup


def test_fresh_default_refresh_requests_the_pinned_revision(
    pinned_refresh_setup: _PinnedRefreshSetup,
    tmp_path: Path,
) -> None:
    """A fresh cache fetches the immutable default source before rendering."""
    result = pinned_refresh_setup.generator.main(repository=tmp_path)

    cache = tmp_path / ".typos-oxendict-base.toml"
    assert result.status == "refreshed", (
        "a fresh default cache must report that it was populated"
    )
    assert pinned_refresh_setup.timeouts == [30.0], (
        "the immutable shared dictionary request must use the 30-second timeout"
    )
    assert [request.full_url for request in pinned_refresh_setup.requests] == [
        pinned_refresh_setup.generator.DEFAULT_BASE_URL
    ], "the fresh cache request must use the immutable default URL"
    assert pinned_refresh_setup.verified_caches == [cache], (
        "the fresh cache must be verified before generated output is accepted"
    )
    assert cache.exists(), "a fresh default refresh must populate its cache"


def test_pinned_base_hash_mismatch_fails_closed(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """A valid but different shared document cannot pass for the pinned revision."""
    _, _, generator = rollout_modules
    cache = tmp_path / ".typos-oxendict-base.toml"
    cache.write_text(dictionary_text(), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 does not match"):
        generator._verify_pinned_base(cache)


def test_pinned_base_hash_accepts_exact_pinned_bytes(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """The recorded dictionary bytes satisfy the immutable digest contract."""
    _, _, generator = rollout_modules
    cache = tmp_path / ".typos-oxendict-base.toml"
    fixture = Path(__file__).parent / "data" / "typos-oxendict-base-64bd9ce.toml"
    pinned_bytes = fixture.read_bytes()

    assert hashlib.sha256(pinned_bytes).hexdigest() == generator.PINNED_BASE_SHA256, (
        "the immutable dictionary fixture must match the recorded SHA-256"
    )

    cache.write_bytes(pinned_bytes)
    generator._verify_pinned_base(cache)


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
