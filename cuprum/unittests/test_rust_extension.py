"""Unit tests for the optional Rust extension probe."""

from __future__ import annotations

import importlib
import logging
import types

import pytest

import cuprum as c
from cuprum import _rust_backend


def test_is_available_returns_false_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns False when the native module is absent."""

    def _raise_missing(_: str) -> types.ModuleType:
        """Raise ModuleNotFoundError to simulate a missing native module."""
        raise ModuleNotFoundError(name="cuprum._rust_backend_native")

    monkeypatch.setattr(importlib, "import_module", _raise_missing)

    assert _rust_backend.is_available() is False, (
        "expected probe to report unavailable when module missing"
    )


def test_is_available_warns_and_reraises_unexpected_import_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Probe preserves unexpected import failures after logging them."""

    def _raise_import_error(_: str) -> types.ModuleType:
        """Raise ImportError to simulate a broken native module."""
        msg = "broken native module"
        raise ImportError(msg)

    monkeypatch.setattr(importlib, "import_module", _raise_import_error)

    with (
        caplog.at_level(logging.WARNING, logger="cuprum._rust_backend"),
        pytest.raises(ImportError, match="broken native module"),
    ):
        _rust_backend.is_available()

    assert any(
        record.levelno == logging.WARNING
        and record.__dict__.get("event") == "cuprum.rust_native_import_failed"
        for record in caplog.records
    )


def test_is_available_returns_true_when_module_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns True when the native module reports availability."""

    class _Native(types.SimpleNamespace):
        """Stub native module that reports availability."""

        @staticmethod
        def is_available() -> bool:
            """Report that the native backend is available."""
            return True

    monkeypatch.setattr(importlib, "import_module", lambda _: _Native())

    assert _rust_backend.is_available() is True, (
        "expected probe to report available when native module present"
    )


def test_native_module_reports_availability_when_installed() -> None:
    """Native extension reports availability when installed."""
    try:
        native = importlib.import_module("cuprum._rust_backend_native")
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == (
            "cuprum._rust_backend_native"
        ):
            pytest.skip("Rust extension is not installed.")
        raise
    assert native.is_available() is True, (
        "expected native extension to report available when installed"
    )


def test_public_probe_reports_unavailable_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public probe forwards to the backend availability check."""
    monkeypatch.setattr(_rust_backend, "is_available", lambda: False)
    assert c.is_rust_available() is False, (
        "expected public probe to report unavailable when backend is unavailable"
    )


def test_public_probe_reports_available_when_backend_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public probe forwards True from the backend availability check."""
    monkeypatch.setattr(_rust_backend, "is_available", lambda: True)
    assert c.is_rust_available() is True, (
        "expected public probe to report available when backend is available"
    )
