"""Unit integration tests for pipeline stream backend pathway selection.

These tests exercise the dispatch layer used by pipeline pumping to verify
backend overrides, forced fallback to Python, and forced-rust error handling.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import _pipeline_streams, _rust_backend


def test_dispatch_uses_python_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Python backend routes dispatch directly to Python pump.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to override environment and internal helpers.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "python")
    called = {"pump": 0}

    async def fake_pump(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        await asyncio.sleep(0)
        called["pump"] += 1

    def unexpected_fd_extraction(_: object) -> int:
        msg = "FD extraction should not be called in forced Python mode"
        raise AssertionError(msg)

    monkeypatch.setattr(_pipeline_streams, "_pump_stream", fake_pump)
    monkeypatch.setattr(
        _pipeline_streams,
        "_extract_reader_fd",
        unexpected_fd_extraction,
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_extract_writer_fd",
        unexpected_fd_extraction,
    )

    reader = typ.cast("asyncio.StreamReader", object())
    writer = typ.cast("asyncio.StreamWriter", object())
    asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

    assert called["pump"] == 1, "expected Python pump to handle forced python mode"


def test_dispatch_falls_back_to_python_when_rust_fd_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode falls back to Python when FD extraction fails.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to override environment and internal helpers.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)
    calls = {"reader_fd": 0, "writer_fd": 0, "pump": 0}

    def missing_reader_fd(_: object) -> None:
        calls["reader_fd"] += 1

    def missing_writer_fd(_: object) -> None:
        calls["writer_fd"] += 1

    monkeypatch.setattr(_pipeline_streams, "_extract_reader_fd", missing_reader_fd)
    monkeypatch.setattr(_pipeline_streams, "_extract_writer_fd", missing_writer_fd)

    async def fake_pump(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        await asyncio.sleep(0)
        calls["pump"] += 1

    monkeypatch.setattr(_pipeline_streams, "_pump_stream", fake_pump)

    reader = typ.cast("asyncio.StreamReader", object())
    writer = typ.cast("asyncio.StreamWriter", object())
    asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

    assert calls["reader_fd"] == 1, "expected Rust reader FD extraction to be attempted"
    assert calls["writer_fd"] == 1, "expected Rust writer FD extraction to be attempted"
    assert calls["pump"] == 1, (
        "expected Python pump fallback when Rust backend cannot extract FDs"
    )


def test_dispatch_raises_import_error_when_rust_forced_but_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced Rust mode surfaces ImportError when extension is unavailable.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to override environment and internal helpers.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)

    called = {"pump": 0}

    async def fake_pump(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        await asyncio.sleep(0)
        called["pump"] += 1

    monkeypatch.setattr(_pipeline_streams, "_pump_stream", fake_pump)

    reader = typ.cast("asyncio.StreamReader", object())
    writer = typ.cast("asyncio.StreamWriter", object())
    with pytest.raises(ImportError, match="CUPRUM_STREAM_BACKEND"):
        asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

    assert called["pump"] == 0, "Python pump should not run when forced Rust is missing"
