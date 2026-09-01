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
from tests.helpers.maturin import maturin_script_locatable, toolchain_available

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth
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

    # `match=` searches rather than fullmatches, so a diagnostic that gained a
    # prefix or suffix would still pass. Compare the message exactly, so the
    # failure a caller sees is the one the import actually raised.
    with pytest.raises(RuntimeError) as exc_info:
        toolchain_available()

    assert str(exc_info.value) == "maturin is installed but its import crashed", (
        f"toolchain_available must propagate the import failure verbatim, "
        f"found {str(exc_info.value)!r}"
    )


#: (filename written to the scripts dir, expected `maturin_script_locatable`
#: result) for the scenarios below. The scheme name and scripts directory
#: name are both mocked and never inspected by the logic under test, so a
#: single directory layout exercises all three cases.
_SCRIPT_LOCATABLE_CASES = (
    pytest.param("maturin", True, id="script_present"),
    pytest.param("mutmut", False, id="unrelated_script_only"),
    pytest.param("maturin.exe", True, id="windows_exe_launcher"),
)


@pytest.mark.parametrize(("filename", "expected"), _SCRIPT_LOCATABLE_CASES)
def test_maturin_script_locatable(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    expected: bool,
) -> None:
    """Detection matches a maturin script by stem, tolerating unrelated files.

    A ``maturin`` script in a scheme's scripts directory is discovered, and
    so is ``maturin.exe`` — the real launcher on Windows, where matching is
    stem-based on purpose (maturin's own ``get_maturin_path`` compares
    ``os.path.splitext(f)[0]`` against ``"maturin"``, accepting any
    extension; the ``windows-2022`` wheel target in
    ``.github/workflows/build-wheels.yml`` installs ``maturin.exe``, and
    narrowing this to an exact ``maturin`` filename would make the probe
    report unavailable there and silently skip the native-wheel contract).

    Conversely, a scripts directory populated only with unrelated tools
    (such as ``mutmut``) reports unavailable. This reproduces the layered
    ``uv run --with ...`` overlay from issue #211, in which the scripts
    directory exists but contains no file named ``maturin`` — exactly the
    condition under which maturin's own ``python -m maturin`` entry point
    fails with "Unable to find `maturin` script".
    """
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    (scripts_dir / filename).write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("posix_prefix",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert maturin_script_locatable() is expected, (
        f"expected maturin_script_locatable() to be {expected} with only "
        f"{filename!r} present"
    )
