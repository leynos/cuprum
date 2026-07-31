"""Tests for the ``toolchain_available`` build gate.

This gate decides whether ``test_maturin_wheel_build_snapshot`` runs at all, so
a wrong ``False`` disables that test silently and indefinitely — the failure
mode any skip-based guard is prone to, and one a green run cannot distinguish
from success. Covering the gate directly is what turns such a regression into a
failure instead.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from tests.helpers import maturin as maturin_helper
from tests.helpers.maturin import toolchain_available

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types


@dc.dataclass(frozen=True, slots=True)
class _ToolchainCase:
    """One arrangement of the gate's three dependencies and its verdict."""

    present: tuple[str, ...]
    maturin_imports: bool
    expected: bool
    reason: str


def _stub_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    present: cabc.Container[str],
    maturin_imports: bool,
) -> None:
    """Present only the tools in ``present``, and optionally a broken maturin.

    ``importlib.import_module`` is intercepted for ``maturin`` alone and
    delegates everything else, so patching it cannot disturb an unrelated
    import that happens to run while the patch is active.
    """
    monkeypatch.setattr(
        maturin_helper.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in present else None,
    )

    real_import = maturin_helper.importlib.import_module

    def fake_import(name: str, package: str | None = None) -> types.ModuleType:
        """Fail the maturin import on request; import anything else for real."""
        if name == "maturin" and not maturin_imports:
            msg = "No module named 'maturin'"
            raise ImportError(msg)
        return real_import(name, package)

    monkeypatch.setattr(maturin_helper.importlib, "import_module", fake_import)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _ToolchainCase(
                present=("cargo", "rustc"),
                maturin_imports=True,
                expected=True,
                reason="both binaries on PATH and an importable maturin means "
                "the toolchain is available",
            ),
            id="all_three_present",
        ),
        pytest.param(
            _ToolchainCase(
                present=("rustc",),
                maturin_imports=True,
                expected=False,
                reason="a missing cargo cannot build the wheel",
            ),
            id="cargo_missing",
        ),
        pytest.param(
            _ToolchainCase(
                present=("cargo",),
                maturin_imports=True,
                expected=False,
                reason="a missing rustc cannot compile the crate",
            ),
            id="rustc_missing",
        ),
        pytest.param(
            _ToolchainCase(
                present=("cargo", "rustc"),
                maturin_imports=False,
                expected=False,
                reason="the build runs `python -m maturin`, so an unimportable "
                "maturin is unusable",
            ),
            id="maturin_import_fails",
        ),
        pytest.param(
            _ToolchainCase(
                present=(),
                maturin_imports=False,
                expected=False,
                reason="nothing present must not report the toolchain as available",
            ),
            id="none_present",
        ),
    ],
)
def test_toolchain_available_requires_all_three_dependencies(
    case: _ToolchainCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``toolchain_available`` is the conjunction of its three dependencies."""
    _stub_toolchain(
        monkeypatch,
        present=case.present,
        maturin_imports=case.maturin_imports,
    )

    assert toolchain_available() is case.expected, case.reason


def test_toolchain_available_propagates_a_non_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``ImportError`` means "unavailable"; other failures must surface.

    A maturin that raises, say, ``RuntimeError`` at import time is broken, not
    absent. Swallowing that would report the toolchain as merely missing and
    skip the build test, hiding a real fault behind a routine-looking skip.
    """
    monkeypatch.setattr(maturin_helper.shutil, "which", lambda name: f"/usr/bin/{name}")

    def exploding_import(name: str, _package: str | None = None) -> types.ModuleType:
        """Raise a non-import failure for maturin."""
        if name == "maturin":
            msg = "maturin is installed but its import crashed"
            raise RuntimeError(msg)
        raise AssertionError(name)

    monkeypatch.setattr(maturin_helper.importlib, "import_module", exploding_import)

    with pytest.raises(RuntimeError, match="import crashed"):
        toolchain_available()
