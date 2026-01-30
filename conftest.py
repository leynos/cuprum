"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _rust_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture(name="rust_streams")
def fixture_rust_streams() -> ModuleType:
    """Provide the Rust streams module when available."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs
