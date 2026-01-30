"""Shared pytest fixtures for optional Rust stream tests.

Use these fixtures to access optional backends without repeating availability
checks in each test module.

Example
-------
def test_pumps_bytes(rust_streams):
    rust_streams.rust_pump_stream(reader_fd, writer_fd)
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import _rust_backend

if typ.TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture(name="rust_streams")
def fixture_rust_streams() -> ModuleType:
    """Provide the Rust streams module when available.

    Parameters
    ----------
    None

    Returns
    -------
    ModuleType
        The imported ``cuprum._streams_rs`` module.

    Raises
    ------
    pytest.Skip
        If the Rust extension is not installed.
    """
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed.")
    from cuprum import _streams_rs

    return _streams_rs
