"""Shared helpers for maturin build and pin contract tests."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess  # noqa: S404 - tests invoke pinned maturin build commands.
import sys
import sysconfig
from pathlib import Path

from tests.helpers import maturin_wheel as _maturin_wheel

_MATURIN_PIN_RE = re.compile(r"maturin==(\d+\.\d+\.\d+)")
_WORKFLOW_PIN_RE = re.compile(r'MATURIN_VERSION:\s*"(\d+\.\d+\.\d+)"')
_ACTION_PIN_RE = re.compile(r'default:\s*"(\d+\.\d+\.\d+)"')
_AARCH64_CONTAINER_PIN_RE = re.compile(
    r"^\s*MANYLINUX_AARCH64_CONTAINER:\s*([^\s#]+)\s+#\s*\S.*$",
    re.MULTILINE,
)
_AARCH64_CONTAINER_USAGE_RE = re.compile(
    r"^\s*container:\s*\$\{\{\s*env\.MANYLINUX_AARCH64_CONTAINER\s*\}\}\s*$",
    re.MULTILINE,
)


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


def read_expected_maturin_version(root: Path) -> str:
    """Read the maturin version pinned in ``pyproject.toml``.

    Raises
    ------
    AssertionError
        If the maturin dependency pin is missing.
    FileNotFoundError
        If ``pyproject.toml`` is absent.
    OSError
        If ``pyproject.toml`` cannot be read.
    UnicodeDecodeError
        If ``pyproject.toml`` is not valid UTF-8.
    """
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = _MATURIN_PIN_RE.search(pyproject)
    if match is None:
        msg = "Could not locate maturin dev dependency pin in pyproject.toml"
        raise AssertionError(msg)
    return match.group(1)


def _require_pin_match(
    match: re.Match[str] | None,
    location: str,
    *,
    subject: str = "maturin version pin",
) -> str:
    """Extract a version from a regex match or raise AssertionError with location."""
    if match is None:
        msg = f"Could not locate {subject} in {location}"
        raise AssertionError(msg)
    return match.group(1)


def read_maturin_pins(root: Path) -> dict[str, str]:
    """Read maturin version pins from the synchronized locations.

    Raises
    ------
    AssertionError
        If any maturin version pin is missing.
    FileNotFoundError
        If any pin source file is absent.
    OSError
        If any pin source file cannot be read.
    UnicodeDecodeError
        If any pin source file is not valid UTF-8.
    """
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-wheels.yml").read_text(encoding="utf-8")
    action = (root / ".github/actions/build-wheels/action.yml").read_text(
        encoding="utf-8"
    )

    return {
        "pyproject.toml": _require_pin_match(
            _MATURIN_PIN_RE.search(pyproject),
            "pyproject.toml",
        ),
        "build-wheels.yml": _require_pin_match(
            _WORKFLOW_PIN_RE.search(workflow),
            ".github/workflows/build-wheels.yml",
        ),
        "build-wheels/action.yml": _require_pin_match(
            _ACTION_PIN_RE.search(action),
            ".github/actions/build-wheels/action.yml",
        ),
    }


def read_manylinux_aarch64_container_ref(root: Path) -> str:
    """Read the pinned manylinux aarch64 container reference.

    Parameters
    ----------
    root
        Repository root containing ``.github/workflows/build-wheels.yml``.

    Returns
    -------
    str
        The pinned ``MANYLINUX_AARCH64_CONTAINER`` image reference.

    Raises
    ------
    AssertionError
        If the ``MANYLINUX_AARCH64_CONTAINER`` pin is missing.
    FileNotFoundError
        If ``.github/workflows/build-wheels.yml`` is absent.
    OSError
        If ``.github/workflows/build-wheels.yml`` cannot be read.
    UnicodeDecodeError
        If ``.github/workflows/build-wheels.yml`` is not valid UTF-8.
    """
    workflow = (root / ".github/workflows/build-wheels.yml").read_text(encoding="utf-8")
    return _require_pin_match(
        _AARCH64_CONTAINER_PIN_RE.search(workflow),
        ".github/workflows/build-wheels.yml",
        subject="MANYLINUX_AARCH64_CONTAINER pin",
    )


def workflow_uses_manylinux_aarch64_container_ref(root: Path) -> bool:
    """Report whether the workflow references the pinned manylinux container.

    Parameters
    ----------
    root
        Repository root containing ``.github/workflows/build-wheels.yml``.

    Returns
    -------
    bool
        ``True`` when the Linux aarch64 build step uses
        ``env.MANYLINUX_AARCH64_CONTAINER``; otherwise ``False``.

    Raises
    ------
    FileNotFoundError
        If ``.github/workflows/build-wheels.yml`` is absent.
    OSError
        If ``.github/workflows/build-wheels.yml`` cannot be read.
    UnicodeDecodeError
        If ``.github/workflows/build-wheels.yml`` is not valid UTF-8.
    """
    workflow = (root / ".github/workflows/build-wheels.yml").read_text(encoding="utf-8")
    return _AARCH64_CONTAINER_USAGE_RE.search(workflow) is not None


def _maturin_module_available() -> bool:
    """Return whether the maturin module can be resolved."""
    try:
        return importlib.util.find_spec("maturin") is not None
    except ImportError:
        return False


def toolchain_available() -> bool:
    """Return whether the Rust toolchain and maturin are available."""
    return (
        shutil.which("cargo") is not None
        and shutil.which("rustc") is not None
        and _maturin_module_available()
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
    whose ``importlib.util.find_spec`` check only confirms the ``maturin``
    module is importable.

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


def build_native_wheel_artifact(root: Path, out_dir: Path) -> Path:
    """Build a native wheel with the pinned maturin version.

    Raises
    ------
    AssertionError
        If the build does not produce exactly one wheel.
    OSError
        If the output directory cannot be created or inspected.
    MaturinBuildError
        If the maturin build command exits non-zero.
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


# The wheel-artifact snapshot helpers live in a sibling module to keep this
# module focused; re-exported here so existing import sites keep working.
wheel_build_snapshot = _maturin_wheel.wheel_build_snapshot
