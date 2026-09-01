"""Tests for cache refresh, validator metadata, and the HTTPS redirect policy."""

from __future__ import annotations

import email.message
import json
import logging
import os
import threading
import tomllib
import typing as typ
import urllib.error
import urllib.request

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types
    from pathlib import Path


def test_offline_refresh_requires_and_reuses_valid_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """Offline mode fails closed before reusing a validated cache."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "base.toml"
    metadata = tmp_path / "base.json"

    with pytest.raises(FileNotFoundError, match="no cached shared dictionary"):
        rollout.refresh_base(
            "https://example.invalid/base", cache, metadata=metadata, offline=True
        )

    cache.write_text(dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base(
        "https://example.invalid/base", cache, metadata=metadata, offline=True
    )

    assert result.status == "offline-cache", (
        "offline mode must reuse the existing valid cache"
    )


def test_local_refresh_switches_authority_and_records_metadata(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """A different explicit authority replaces a cache regardless of mtime."""
    _, rollout, _ = rollout_modules
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    first.write_text(dictionary_text("first"), encoding="utf-8")
    second.write_text(dictionary_text("second"), encoding="utf-8")
    os.utime(first, ns=(3_000_000_000, 3_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))
    rollout.refresh_base(first, cache, metadata=metadata)

    result = rollout.refresh_base(second, cache, metadata=metadata)

    assert result.status == "refreshed", (
        "an older local authority must still refresh the cache "
        "because the recorded source path changed"
    )
    assert rollout.load_dictionary(cache).stems == ("second",), (
        "switching authority must replace the cached dictionary contents"
    )
    assert json.loads(metadata.read_text(encoding="utf-8"))["source"] == str(
        second.resolve()
    ), "local refresh metadata must identify the active local authority"


def test_http_refresh_uses_validators_and_preserves_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
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

        def read(self, limit: int | None = None) -> bytes:
            """Return a valid shared dictionary, honouring any byte limit."""
            return dictionary_text().encode()[:limit]

        def __enter__(self) -> Response:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context.

            Returns
            -------
            None
                This method returns ``None`` and does not suppress
                exceptions raised within the ``with`` body.
            """

    def open_response(request: urllib.request.Request, *, timeout: float) -> Response:
        """Capture the request passed to the network boundary.

        Returns
        -------
        Response
            A fake response for the captured request.
        """
        assert timeout == pytest.approx(30.0), (
            "the HTTPS fetch must use the configured 30s timeout"
        )
        requests.append(request)
        return Response()

    patch_https_opener(open_response)

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
    assert "source" not in json.loads(metadata.read_text(encoding="utf-8")), (
        "remote metadata must not persist a potentially sensitive source URL"
    )


def test_remote_failure_reuses_only_a_valid_stale_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
) -> None:
    """Network failure keeps validated data and propagates without it."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"

    def fail(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable remote authority that always raises ``URLError``."""
        message = "offline"
        raise urllib.error.URLError(message)

    patch_https_opener(fail)

    with pytest.raises(urllib.error.URLError):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    cache.write_text(dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    assert result.status == "stale-cache", (
        "an unreachable authority must fall back to the stale cache"
    )


def test_remote_refresh_rejects_insecure_and_invalid_content(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
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

        def read(self, limit: int | None = None) -> bytes:
            """Return malformed bytes, honouring any byte limit."""
            return b"not = [valid"[:limit]

        def __enter__(self) -> InvalidResponse:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context."""

    patch_https_opener(lambda *_args, **_kwargs: InvalidResponse())

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
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """HTTP status handling distinguishes current, stale and absent data."""
    cache = tmp_path / "cache.toml"
    cache.write_text(dictionary_text(), encoding="utf-8")
    headers = email.message.Message()
    not_modified = urllib.error.HTTPError(
        "https://example.test/base", 304, "not modified", headers, None
    )
    unavailable = urllib.error.HTTPError(
        "https://example.test/base", 503, "unavailable", headers, None
    )

    assert refresh_module._http_error_result(cache, not_modified).status == "current", (
        "HTTP 304 must preserve a valid current cache"
    )
    assert (
        refresh_module._http_error_result(cache, unavailable).status == "stale-cache"
    ), "an unavailable authority must reuse a valid stale cache"
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
    ), "an older remote date must preserve the newer cache"
    assert refresh_module._remote_is_not_newer(
        {"last_modified": "invalid"}, {"Last-Modified": "invalid"}
    ), "matching malformed validators must preserve the existing cache"
    assert not refresh_module._remote_is_not_newer({}, {"Last-Modified": "invalid"}), (
        "invalid dates without saved metadata must not prove freshness"
    )
    assert not refresh_module._remote_is_not_newer(
        {
            "etag": '"estate-v1"',
            "last_modified": "Fri, 10 Jul 2026 08:00:00 GMT",
        },
        {
            "ETag": '"estate-v2"',
            "Last-Modified": "Fri, 10 Jul 2026 08:00:00 GMT",
        },
    ), "a changed ETag must prove the entity newer even when Last-Modified is unchanged"


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

    with (
        caplog.at_level(logging.WARNING, logger=refresh_module.__name__),
        pytest.raises(urllib.error.URLError),
    ):
        handler.redirect_request(
            request, None, 302, "Found", headers, "http://cdn.example.test/base.toml"
        )

    assert refresh_module.degradation_snapshot()["https_redirect_downgrade"] == 1, (
        "a refused downgrade must increment the bounded degradation counter"
    )
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "typos_rollout.https_redirect_downgrade"
    ]
    assert records, "the downgrade must emit a structured warning"
    assert getattr(records[0], "redirect_scheme", None) == "http", (
        "the rejected scheme must be recorded for diagnosis"
    )
    assert "cdn.example.test" not in caplog.text, (
        "the redirect URL must never be logged"
    )


def test_degradation_counters_survive_concurrent_increments(
    refresh_module: types.ModuleType,
) -> None:
    """Concurrent degradation records are counted without losing increments.

    ``+=`` on a dict value is a read-modify-write, so an unguarded counter
    drops increments under contention. The fixture resets the counters, so the
    expected total is exact rather than a delta.
    """
    workers, per_worker = 8, 50
    barrier = threading.Barrier(workers, timeout=5.0)

    def record() -> None:
        barrier.wait()
        for _ in range(per_worker):
            refresh_module._record_degradation("stale_cache")

    threads = [threading.Thread(target=record, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=10)
    finally:
        assert not any(thread.is_alive() for thread in threads), (
            "degradation recording threads must not outlive the test"
        )

    assert (
        refresh_module.degradation_snapshot()["stale_cache"] == workers * per_worker
    ), "every concurrent increment must be counted"


def test_degradation_reset_clears_every_counter(
    refresh_module: types.ModuleType,
) -> None:
    """The reset hook returns all counters to zero."""
    refresh_module._record_degradation("offline_cache")
    assert refresh_module.degradation_snapshot()["offline_cache"] == 1, (
        "offline cache reuse must increment its degradation counter"
    )

    refresh_module.reset_degradations()

    assert all(
        count == 0 for count in refresh_module.degradation_snapshot().values()
    ), "reset must zero every degradation counter"


class _OversizedResponse:
    """Return a body one byte past the dictionary size limit."""

    status = 200
    headers: typ.ClassVar[dict[str, str]] = {}

    def read(self, limit: int | None = None) -> bytes:
        """Return an oversized body, honouring any byte limit."""
        assert limit is not None, "the oversized-response test requires a byte limit"
        return b"x" * limit

    def __enter__(self) -> _OversizedResponse:
        """Enter the fake response context."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Leave the fake response context."""


def test_oversized_remote_body_falls_back_to_stale_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
) -> None:
    """An oversized download degrades to a valid stale cache."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    cache.write_text(dictionary_text(), encoding="utf-8")
    patch_https_opener(lambda *_args, **_kwargs: _OversizedResponse())

    result = rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    assert result.status == "stale-cache", (
        "an oversized response must fall back to the valid stale cache"
    )


def test_oversized_remote_body_without_cache_raises(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    refresh_module: types.ModuleType,
    tmp_path: Path,
    patch_https_opener: cabc.Callable[[cabc.Callable[..., object]], None],
) -> None:
    """An oversized download with no cache to fall back on is rejected."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    patch_https_opener(lambda *_args, **_kwargs: _OversizedResponse())

    with pytest.raises(ValueError, match="byte limit"):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)
    assert not cache.exists(), "an oversized download must not leave a cache behind"
