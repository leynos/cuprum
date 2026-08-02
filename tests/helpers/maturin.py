"""Helpers for building and introspecting the native maturin wheel.

Maturin pin-synchronization checks are not here. They belong to
``cuprum/unittests/test_maturin_pins.py``, which owns the readers and patterns
that compare the pins across CI files. The subset with a second consumer — the
pyproject reader and the container-reference pattern — sits in
``cuprum/unittests/_maturin_pin_support.py``, from which
``cuprum/unittests/test_maturin_build.py`` takes the expected version to assert
against the built wheel's ``Generator``.

This module retains the toolchain detectors and the wheel *build*, which wrap
``subprocess`` and ``sysconfig`` probing that does not inline cleanly; wheel
*inspection* lives in ``tests.helpers.maturin_wheel`` and is re-exported here
so existing import sites keep working. Do not re-externalize further helpers
here until a second concrete consumer exists and the shared interface can be
designed against real requirements.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess  # noqa: S404 - tests invoke pinned maturin build commands.
import sys
import sysconfig
from pathlib import Path

from tests.helpers import maturin_wheel as _maturin_wheel


class MaturinBuildError(subprocess.CalledProcessError):
    """Maturin build failure with raw output and rendered diagnostics.

    Attributes
    ----------
    build_command : tuple[str, ...]
        Command used to invoke the maturin wheel build.
    returncode : int
        Process exit status, inherited from ``CalledProcessError``.
    stderr : str | bytes | None
        Raw captured standard error, inherited from ``CalledProcessError``.
    """

    def __init__(self, error: subprocess.CalledProcessError) -> None:
        """Store raw process diagnostics separately from ``str(error)``."""
        super().__init__(
            error.returncode,
            error.cmd,
            output=error.stdout,
            stderr=error.stderr,
        )
        if isinstance(error.cmd, list | tuple):
            self.build_command = tuple(str(part) for part in error.cmd)
        else:
            self.build_command = (str(error.cmd),)

    def __str__(self) -> str:
        """Return an enriched diagnostic while preserving raw stderr."""
        rendered_command = " ".join(self.build_command)
        return (
            f"maturin wheel build failed for command: {rendered_command}\n"
            f"stderr:\n{self.stderr}"
        )


def toolchain_available() -> bool:
    """Report whether the Rust toolchain and the maturin module are available.

    Returns
    -------
    bool
        ``True`` only when ``cargo`` and ``rustc`` are both on ``PATH`` and the
        ``maturin`` module imports successfully in the current interpreter;
        ``False`` if any of the three is missing.

    The module is imported rather than located with ``find_spec``: the build
    runs ``python -m maturin``, which needs the module to import, and a module
    that is findable can still fail to import.
    """
    try:
        importlib.import_module("maturin")
    except ImportError:
        maturin_available = False
    else:
        maturin_available = True
    return (
        shutil.which("cargo") is not None
        and shutil.which("rustc") is not None
        and maturin_available
    )


def _script_named_maturin_exists(directory: Path) -> bool:
    """Return whether ``directory`` contains a file stem named ``maturin``."""
    return any(
        entry.is_file() and entry.stem == "maturin" for entry in directory.rglob("*")
    )


def maturin_script_locatable() -> bool:
    """Return whether maturin's own lookup can find its compiled script.

    Mirrors ``maturin.__main__.get_maturin_path``: the ``maturin`` PyPI
    package resolves its bundled binary by walking each ``sysconfig``
    scheme's ``scripts`` directory for a file named ``maturin``, keyed off
    ``sys.prefix``/``sys.exec_prefix`` of the *running* interpreter — not
    ``sys.path`` or ``PATH``. This diverges from :func:`toolchain_available`,
    whose ``importlib.import_module`` check only confirms the ``maturin``
    module imports.

    The two checks disagree in layered/ephemeral interpreters such as a
    ``uv run --with ...`` overlay: the overlay's ``sys.path`` includes the
    project's own virtual environment (so the module imports fine and
    :func:`toolchain_available` reports ``True``), but ``sys.prefix`` points
    at a temporary environment that never received maturin's script, so
    ``python -m maturin`` fails with ``Unable to find `maturin` script``
    even though a real build would succeed in the project's own virtualenv.
    Checking this separately lets callers skip precisely where the build is
    genuinely unreachable, without masking a regression in normal CI or
    local-development environments, where ``sys.prefix`` matches the
    virtualenv that installed maturin's script.

    Returns
    -------
    bool
        Whether maturin's own lookup can locate its compiled script in this
        interpreter.
    """
    script_dirs = (
        Path(sysconfig.get_path("scripts", scheme))
        for scheme in sysconfig.get_scheme_names()
    )
    return any(
        _script_named_maturin_exists(directory)
        for directory in script_dirs
        if directory.exists()
    )


def build_native_wheel_artefact(root: Path, out_dir: Path) -> Path:
    """Build a native wheel using the current interpreter's maturin.

    Invokes ``python -m maturin`` with the running interpreter, so the maturin
    that builds the wheel is whichever version is installed in that
    environment. Alignment with the declared pin is asserted separately by
    ``test_installed_maturin_matches_expected_pin`` and by the snapshot test's
    generator check, not selected here.

    Parameters
    ----------
    root
        Repository root containing ``rust/cuprum-rust/Cargo.toml``.
    out_dir
        Directory that receives the built wheel.

    Returns
    -------
    pathlib.Path
        Path to the single wheel produced in ``out_dir``.

    Raises
    ------
    AssertionError
        If the build does not produce exactly one wheel.
    OSError
        If the output directory cannot be created or inspected.
    MaturinBuildError
        If the maturin build command exits non-zero.

    Examples
    --------
    Build the native wheel into a repository-local distribution directory:

    >>> root = Path.cwd()
    >>> wheel = build_native_wheel_artefact(root, root / "dist")
    >>> wheel.suffix
    '.whl'
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "maturin",
        "build",
        "--release",
        "--locked",
        "--out",
        str(out_dir),
        "--manifest-path",
        str(root / "rust/cuprum-rust/Cargo.toml"),
    ]
    try:
        subprocess.run(  # noqa: S603 - trusted paths and pinned maturin
            command,
            check=True,
            cwd=root,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise MaturinBuildError(exc) from exc
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        msg = f"Expected exactly one wheel in {out_dir}, found {wheels!r}"
        raise AssertionError(msg)
    return wheels[0]


# The wheel-artefact snapshot helpers live in a sibling module to keep this
# module focused; re-exported here so existing import sites keep working.
wheel_build_snapshot = _maturin_wheel.wheel_build_snapshot
