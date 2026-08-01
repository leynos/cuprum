"""Tests for the repository spelling-policy scripts."""

from __future__ import annotations

import ast
import email.message
import importlib
import json
import logging
import os
import tomllib
import typing as typ
import urllib.error
import urllib.request
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]


def test_rollout_scripts_support_python_313() -> None:
    """Every rollout script parses with the declared minimum Python version."""
    for script in SCRIPT_DIRECTORY.glob("*.py"):
        ast.parse(
            script.read_text(encoding="utf-8"),
            filename=str(script),
            feature_version=(3, 13),
        )


@pytest.fixture(name="rollout_modules")
def rollout_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Import the scripts through the same top-level module path used at runtime."""
    monkeypatch.syspath_prepend(str(SCRIPT_DIRECTORY))
    names = ("typos_rollout_cache", "typos_rollout", "generate_typos_config")
    importlib.invalidate_caches()
    cache, rollout, generator = (importlib.import_module(name) for name in names)
    return cache, rollout, generator


@pytest.fixture(name="refresh_module")
def refresh_module_fixture(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> types.ModuleType:
    """Import the cache-refresh module through the runtime module path.

    Returns
    -------
    types.ModuleType
        The ``typos_rollout_refresh`` module.
    """
    _ = rollout_modules  # Ensures the script directory is on ``sys.path``.
    return importlib.import_module("typos_rollout_refresh")


def _patch_https_opener(
    monkeypatch: pytest.MonkeyPatch,
    open_response: cabc.Callable[..., object],
) -> None:
    """Route HTTPS fetches through *open_response*, asserting the redirect policy.

    The refresh module builds its own opener so the HTTPS-only redirect handler
    applies, so tests patch ``build_opener`` rather than ``urlopen``.
    """
    refresh = importlib.import_module("typos_rollout_refresh")

    class _StubOpener:
        """Minimal stand-in for ``urllib.request.OpenerDirector``."""

        def open(self, *args: object, **kwargs: object) -> object:
            """Delegate to the supplied response factory."""
            return open_response(*args, **kwargs)

    def build_opener(*handlers: object) -> _StubOpener:
        """Return the stub opener once the redirect policy is confirmed."""
        assert any(
            isinstance(handler, refresh._HttpsOnlyRedirectHandler)
            for handler in handlers
        ), "HTTPS refresh must install the HTTPS-only redirect handler"
        return _StubOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)


def _dictionary_text(stem: str = "organ") -> str:
    """Return a minimal valid shared-dictionary document."""
    return (
        'schema = 1\n\n[oxford]\nstems = ["'
        + stem
        + '"]\n\n[words]\naccepted = []\n\n[words.corrections]\n\n'
        + "[patterns]\nignore = []\n\n[files]\nexclude = []\n"
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
) -> None:
    """An older local authority cannot replace a newer untracked cache."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "shared.toml"
    cache = tmp_path / ".typos-base.toml"
    metadata = tmp_path / ".typos-base.json"
    source.write_text(_dictionary_text(), encoding="utf-8")
    source.touch()
    rollout.refresh_base(source, cache, metadata=metadata)
    cache.write_text(_dictionary_text("newer"), encoding="utf-8")
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

    result = generator.main(repository=tmp_path, source="https://example.invalid/base")

    assert result.status == "tracked-config", (
        "an unreachable HTTPS authority must fall back to the tracked config"
    )
    assert result.cache == tracked_config, (
        "the fallback must point at the tracked configuration file"
    )


def test_dictionary_validation_rejects_invalid_documents(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Schema, table, string-list and correction types remain validated."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "base.toml"
    invalid_documents = (
        _dictionary_text().replace("schema = 1", "schema = 2"),
        _dictionary_text().replace('[oxford]\nstems = ["organ"]', 'oxford = "bad"'),
        _dictionary_text().replace('stems = ["organ"]', "stems = [1]"),
        _dictionary_text().replace(
            "[words.corrections]", "[words.corrections]\nteh = 1"
        ),
    )

    for document in invalid_documents:
        source.write_text(document, encoding="utf-8")
        with pytest.raises((TypeError, ValueError)):
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


def test_offline_refresh_requires_and_reuses_valid_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Offline mode fails closed before reusing a validated cache."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "base.toml"
    metadata = tmp_path / "base.json"

    with pytest.raises(FileNotFoundError, match="no cached shared dictionary"):
        rollout.refresh_base(
            "https://example.invalid/base", cache, metadata=metadata, offline=True
        )

    cache.write_text(_dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base(
        "https://example.invalid/base", cache, metadata=metadata, offline=True
    )

    assert result.status == "offline-cache", (
        "offline mode must reuse the existing valid cache"
    )


def test_local_refresh_switches_authority_and_records_metadata(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """A different explicit authority replaces a cache regardless of mtime."""
    _, rollout, _ = rollout_modules
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    first.write_text(_dictionary_text("first"), encoding="utf-8")
    second.write_text(_dictionary_text("second"), encoding="utf-8")
    os.utime(first, ns=(3_000_000_000, 3_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))
    rollout.refresh_base(first, cache, metadata=metadata)

    result = rollout.refresh_base(second, cache, metadata=metadata)

    assert result.status == "refreshed", (
        "a newer local authority must refresh the cache"
    )
    assert rollout.load_dictionary(cache).stems == ("second",)
    assert json.loads(metadata.read_text(encoding="utf-8"))["source"] == str(
        second.resolve()
    )


def test_http_refresh_uses_validators_and_preserves_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote refresh persists validators and sends them on the next request."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    requests: list[urllib.request.Request] = []

    class Response:
        """Provide the HTTP response surface consumed by the helper."""

        status = 200
        headers: typ.ClassVar[dict[str, str]] = {
            "ETag": '"estate-v1"',
            "Last-Modified": "Fri, 10 Jul 2026 08:00:00 GMT",
        }

        def read(self) -> bytes:
            """Return a valid shared dictionary."""
            return _dictionary_text().encode()

        def __enter__(self) -> Response:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context."""

    def open_response(request: urllib.request.Request, *, timeout: float) -> Response:
        """Capture the request passed to the network boundary."""
        assert timeout == 30.0, "the HTTPS fetch must use the configured 30s timeout"
        requests.append(request)
        return Response()

    _patch_https_opener(monkeypatch, open_response)

    first = rollout.refresh_base(
        "https://example.test/base.toml", cache, metadata=metadata
    )
    second = rollout.refresh_base(
        "https://example.test/base.toml", cache, metadata=metadata
    )

    assert first.status == "refreshed", "the first refresh must populate the cache"
    assert second.status == "current", (
        "a validated second refresh must report the cache as current"
    )
    assert requests[1].get_header("If-none-match") == '"estate-v1"', (
        "the conditional request must carry the saved ETag validator"
    )


def test_remote_failure_reuses_only_a_valid_stale_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Network failure keeps validated data and propagates without it."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"

    def fail(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable remote authority."""
        message = "offline"
        raise urllib.error.URLError(message)

    _patch_https_opener(monkeypatch, fail)

    with pytest.raises(urllib.error.URLError):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    cache.write_text(_dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    assert result.status == "stale-cache", (
        "an unreachable authority must fall back to the stale cache"
    )


def test_remote_refresh_rejects_insecure_and_invalid_content(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The remote boundary requires HTTPS and validates bytes before install."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"

    with pytest.raises(ValueError, match="must use HTTPS"):
        rollout.refresh_base("http://example.test/base", cache, metadata=metadata)

    class InvalidResponse:
        """Return malformed TOML from an otherwise successful response."""

        status = 200
        headers: typ.ClassVar[dict[str, str]] = {}

        def read(self) -> bytes:
            """Return malformed bytes."""
            return b"not = [valid"

        def __enter__(self) -> InvalidResponse:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context."""

    _patch_https_opener(monkeypatch, lambda *_args, **_kwargs: InvalidResponse())

    with pytest.raises(tomllib.TOMLDecodeError):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)
    assert not cache.exists(), "an invalid download must not leave a cache behind"


def test_metadata_reader_handles_invalid_and_non_object_json(
    refresh_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """Malformed or non-object freshness metadata is safely ignored."""
    metadata = tmp_path / "cache.json"

    metadata.write_text("not-json", encoding="utf-8")
    assert refresh_module._read_metadata(metadata) == {}, (
        "malformed metadata must degrade to an empty mapping"
    )
    metadata.write_text("[]", encoding="utf-8")
    assert refresh_module._read_metadata(metadata) == {}, (
        "non-object metadata must degrade to an empty mapping"
    )


def test_http_error_translation_handles_not_modified_and_stale_cache(
    refresh_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """HTTP status handling distinguishes current, stale and absent data."""
    cache = tmp_path / "cache.toml"
    cache.write_text(_dictionary_text(), encoding="utf-8")
    headers = email.message.Message()
    not_modified = urllib.error.HTTPError(
        "https://example.test/base", 304, "not modified", headers, None
    )
    unavailable = urllib.error.HTTPError(
        "https://example.test/base", 503, "unavailable", headers, None
    )

    assert refresh_module._http_error_result(cache, not_modified).status == "current"
    assert refresh_module._http_error_result(cache, unavailable).status == "stale-cache"
    cache.unlink()
    with pytest.raises(urllib.error.HTTPError):
        refresh_module._http_error_result(cache, unavailable)


def test_remote_freshness_uses_dates_and_falls_back_on_invalid_values(
    refresh_module: types.ModuleType,
) -> None:
    """Last-Modified comparison remains conservative for malformed dates."""
    assert refresh_module._remote_is_not_newer(
        {"last_modified": "Fri, 10 Jul 2026 08:00:00 GMT"},
        {"Last-Modified": "Fri, 10 Jul 2026 07:00:00 GMT"},
    )
    assert refresh_module._remote_is_not_newer(
        {"last_modified": "invalid"}, {"Last-Modified": "invalid"}
    )
    assert not refresh_module._remote_is_not_newer({}, {"Last-Modified": "invalid"})


def test_https_redirect_to_http_is_rejected(
    refresh_module: types.ModuleType,
) -> None:
    """A redirect that downgrades HTTPS to HTTP is refused before reissue."""
    handler = refresh_module._HttpsOnlyRedirectHandler()
    request = urllib.request.Request("https://example.test/base.toml")
    headers = email.message.Message()

    allowed = handler.redirect_request(
        request, None, 302, "Found", headers, "https://cdn.example.test/base.toml"
    )

    assert allowed is not None, "an HTTPS redirect target must be followed"
    assert allowed.full_url == "https://cdn.example.test/base.toml", (
        "an HTTPS redirect target must be followed"
    )

    with pytest.raises(urllib.error.URLError, match="must stay on HTTPS"):
        handler.redirect_request(
            request, None, 302, "Found", headers, "http://cdn.example.test/base.toml"
        )


def test_https_redirect_downgrade_is_logged_and_counted(
    refresh_module: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused downgrade emits a structured warning and bumps the counter."""
    handler = refresh_module._HttpsOnlyRedirectHandler()
    request = urllib.request.Request("https://example.test/base.toml")
    headers = email.message.Message()
    before = refresh_module.REFRESH_DEGRADATIONS["https_redirect_downgrade"]

    with (
        caplog.at_level(logging.WARNING, logger=refresh_module.__name__),
        pytest.raises(urllib.error.URLError),
    ):
        handler.redirect_request(
            request, None, 302, "Found", headers, "http://cdn.example.test/base.toml"
        )

    assert (
        refresh_module.REFRESH_DEGRADATIONS["https_redirect_downgrade"] == before + 1
    ), "a refused downgrade must increment the bounded degradation counter"
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "typos_rollout.https_redirect_downgrade"
    ]
    assert records, "the downgrade must emit a structured warning"
    assert records[0].redirect_scheme == "http", (
        "the rejected scheme must be recorded for diagnosis"
    )
    assert "cdn.example.test" not in caplog.text, (
        "the redirect URL must never be logged"
    )
