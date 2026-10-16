"""Shared time budgets for tests that build Python distributions.

The ``uv`` frontend does not compile Rust. The ``maturin`` frontend does and
can wait for shared Cargo package-cache locks and compiler admission. Pytest's
deadline leaves an extra minute for subprocess failure diagnostics.
"""

from __future__ import annotations

import pytest

UV_PACKAGING_TIMEOUT_SECONDS = 3 * 60
MATURIN_PACKAGING_TIMEOUT_SECONDS = 60 * 60

PACKAGING_BACKENDS = (
    pytest.param(
        "uv",
        UV_PACKAGING_TIMEOUT_SECONDS,
        marks=pytest.mark.timeout(UV_PACKAGING_TIMEOUT_SECONDS + 60),
    ),
    pytest.param(
        "maturin",
        MATURIN_PACKAGING_TIMEOUT_SECONDS,
        marks=pytest.mark.timeout(MATURIN_PACKAGING_TIMEOUT_SECONDS + 60),
    ),
)
