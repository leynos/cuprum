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
    ), "expected cuprum.rust_native_import_failed warning was not emitted"


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
    """Native extension reports availability when installed.

    Raises
    ------
    ImportError
        If importing the native module fails for a reason other than the
        module being absent.
    """
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


@pytest.mark.parametrize(
    "backend_available",
    [
        pytest.param(False, id="backend_unavailable"),
        pytest.param(True, id="backend_available"),
    ],
)
def test_public_probe_forwards_backend_availability(
    monkeypatch: pytest.MonkeyPatch, *, backend_available: bool
) -> None:
    """Public probe forwards the backend availability check verbatim."""
    monkeypatch.setattr(_rust_backend, "is_available", lambda: backend_available)
    assert c.is_rust_available() is backend_available, (
        f"expected public probe to report {backend_available} when backend "
        f"reports {backend_available}"
    )
