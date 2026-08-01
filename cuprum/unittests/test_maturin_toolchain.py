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


def test_maturin_script_locatable_true_when_script_present(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection succeeds when a ``maturin`` script sits in a scheme's dir."""
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    (scripts_dir / "maturin").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("posix_prefix",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert maturin_script_locatable(), (
        "a maturin script in a scheme's scripts directory must be discovered"
    )


def test_maturin_script_locatable_false_when_script_absent(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection reports unavailable when no scheme dir has the script.

    This reproduces the layered ``uv run --with ...`` overlay from issue
    #211: the scripts directory exists (populated with unrelated tools such
    as mutmut itself) but contains no file named ``maturin``, which is
    exactly the condition under which maturin's own ``python -m maturin``
    entry point fails with "Unable to find `maturin` script".
    """
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    (scripts_dir / "mutmut").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("posix_prefix",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert not maturin_script_locatable(), (
        "no maturin script is present, so discovery must report unavailable"
    )


def test_maturin_script_locatable_matches_windows_exe_launcher(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection accepts ``maturin.exe``, the real launcher on Windows.

    Matching is stem-based on purpose: maturin's own ``get_maturin_path``
    compares ``os.path.splitext(f)[0]`` against ``"maturin"``, so it accepts
    any extension. On the ``windows-2022`` wheel target in
    ``.github/workflows/build-wheels.yml`` the installed launcher *is*
    ``maturin.exe``. Narrowing this to an exact ``maturin`` filename would
    make the probe report unavailable on Windows and silently skip the
    native-wheel contract there, so this test pins the mirrored behaviour.
    """
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    (scripts_dir / "maturin.exe").write_bytes(b"MZ")
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_scheme_names", lambda: ("nt",)
    )
    monkeypatch.setattr(
        "tests.helpers.maturin.sysconfig.get_path", lambda *_a, **_k: str(scripts_dir)
    )

    assert maturin_script_locatable(), (
        "stem-based matching must accept maturin.exe, the Windows launcher"
    )
