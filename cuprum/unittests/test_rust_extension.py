"""Unit tests for the optional Rust extension probe."""

from __future__ import annotations

import importlib
import types

import pytest

from cuprum import _rust_backend


def test_is_available_returns_false_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns False when the native module is absent."""

    def _raise_missing(_: str) -> types.ModuleType:
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", _raise_missing)

    assert _rust_backend.is_available() is False


def test_is_available_returns_true_when_module_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns True when the native module reports availability."""

    class _Native(types.SimpleNamespace):
        @staticmethod
        def is_available() -> bool:
            return True

    monkeypatch.setattr(importlib, "import_module", lambda _: _Native())

    assert _rust_backend.is_available() is True


def test_native_module_reports_availability_when_installed() -> None:
    """Native extension reports availability when installed."""
    try:
        native = importlib.import_module("cuprum._rust_backend_native")
    except ModuleNotFoundError:
        pytest.skip("Rust extension is not installed.")
    assert native.is_available() is True
