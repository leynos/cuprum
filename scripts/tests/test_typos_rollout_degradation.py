"""Tests for the HTTPS redirect policy, body limits, and degradation counters."""

from __future__ import annotations

import email.message
import logging
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


def test_https_redirect_to_http_is_rejected(
    degradation_module: types.ModuleType,
) -> None:
    """A redirect that downgrades HTTPS to HTTP is refused before reissue."""
    handler = degradation_module._HttpsOnlyRedirectHandler()
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
    degradation_module: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused downgrade emits a structured warning and bumps the counter."""
    handler = degradation_module._HttpsOnlyRedirectHandler()
    request = urllib.request.Request("https://example.test/base.toml")
    headers = email.message.Message()

    with (
        caplog.at_level(logging.WARNING, logger=degradation_module.__name__),
        pytest.raises(urllib.error.URLError),
    ):
        handler.redirect_request(
            request, None, 302, "Found", headers, "http://cdn.example.test/base.toml"
        )

    assert degradation_module.degradation_snapshot()["https_redirect_downgrade"] == 1, (
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
    degradation_module: types.ModuleType,
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
            degradation_module._record_degradation("stale_cache")

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
        degradation_module.degradation_snapshot()["stale_cache"] == workers * per_worker
    ), "every concurrent increment must be counted"


def test_degradation_reset_clears_every_counter(
    degradation_module: types.ModuleType,
) -> None:
    """The reset hook returns all counters to zero."""
    degradation_module._record_degradation("offline_cache")
    assert degradation_module.degradation_snapshot()["offline_cache"] == 1, (
        "offline cache reuse must increment its degradation counter"
    )

    degradation_module.reset_degradations()

    assert all(
        count == 0 for count in degradation_module.degradation_snapshot().values()
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
    degradation_module: types.ModuleType,
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
