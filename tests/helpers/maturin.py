"""Shared helpers for maturin build and pin contract tests."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess  # noqa: S404 - tests invoke pinned maturin build commands.
import sys
import typing as typ
import zipfile

if typ.TYPE_CHECKING:
    from pathlib import Path

_MATURIN_PIN_RE = re.compile(r"maturin==(\d+\.\d+\.\d+)")
_WORKFLOW_PIN_RE = re.compile(r'MATURIN_VERSION:\s*"(\d+\.\d+\.\d+)"')
_ACTION_PIN_RE = re.compile(r'default:\s*"(\d+\.\d+\.\d+)"')
_GENERATOR_RE = re.compile(r"^Generator:\s*maturin\s*\(([^)]+)\)\s*$", re.MULTILINE)
_EXTENSION_MODULE_RE = re.compile(
    r"^cuprum/_rust_backend_native\.cpython-[^/]+\.so$",
)
_DIST_INFO_SUFFIXES: dict[str, str] = {
    ".dist-info/RECORD": "cuprum-<version>.dist-info/RECORD",
    ".dist-info/METADATA": "cuprum-<version>.dist-info/METADATA",
    ".dist-info/WHEEL": "cuprum-<version>.dist-info/WHEEL",
    ".dist-info/licenses/LICENSE": "cuprum-<version>.dist-info/licenses/LICENSE",
}


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


def _require_pin_match(match: re.Match[str] | None, location: str) -> str:
    """Extract a version from a regex match or raise AssertionError with location."""
    if match is None:
        msg = f"Could not locate maturin version pin in {location}"
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


def build_native_wheel_artifact(root: Path, out_dir: Path) -> Path:
    """Build a native wheel with the pinned maturin version.

    Raises
    ------
    AssertionError
        If the build does not produce exactly one wheel.
    OSError
        If the output directory cannot be created or inspected.
    subprocess.CalledProcessError
        If the maturin build command exits non-zero.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "maturin",
        "build",
        "--release",
        "--out",
        str(out_dir),
        "--manifest-path",
        str(root / "rust/cuprum-rust/Cargo.toml"),
    ]
    subprocess.run(  # noqa: S603 - command list uses only trusted paths and pinned maturin
        command,
        check=True,
        cwd=root,
    )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        msg = f"Expected exactly one wheel in {out_dir}, found {wheels!r}"
        raise AssertionError(msg)
    return wheels[0]


def _header_value(headers: dict[str, list[str]], key: str) -> str | None:
    """Return the first header value for the given key, or None if absent."""
    values = headers.get(key)
    if not values:
        return None
    return values[0]


def _parse_metadata(raw_metadata: str) -> dict[str, typ.Any]:
    """Parse RFC 2822-style metadata headers into a normalised dict."""
    headers: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in raw_metadata.splitlines():
        if line.startswith((" ", "\t")) and current_key is not None:
            headers[current_key][-1] = f"{headers[current_key][-1]} {line.strip()}"
            continue
        if ":" not in line:
            break
        key, value = line.split(":", 1)
        current_key = key.strip()
        headers.setdefault(current_key, []).append(value.strip())

    return {
        "name": _header_value(headers, "Name"),
        "version": _header_value(headers, "Version"),
        "requires_python": _header_value(headers, "Requires-Python"),
        "requires_dist": sorted(headers.get("Requires-Dist", [])),
        "classifiers": sorted(headers.get("Classifier", [])),
    }


def _normalise_wheel_entry(name: str) -> str:
    """Normalize platform/version wheel entry names to stable placeholders."""
    if _EXTENSION_MODULE_RE.match(name):
        return "cuprum/_rust_backend_native.cpython-<platform>.so"
    if "/sboms/" in name:
        return "cuprum-<version>.dist-info/sboms/<sbom>.cyclonedx.json"
    for suffix, normalised in _DIST_INFO_SUFFIXES.items():
        if name.endswith(suffix):
            return normalised
    return name


def _locate_dist_info_wheel(entry_names: list[str]) -> str:
    """Return the .dist-info/WHEEL entry name from a wheel archive's namelist.

    Parameters
    ----------
    entry_names:
        All entry names returned by ``zipfile.ZipFile.namelist()``.

    Raises
    ------
    AssertionError
        If no ``.dist-info/WHEEL`` entry is present.
    """
    wheel_name = next(
        (name for name in entry_names if name.endswith(".dist-info/WHEEL")),
        None,
    )
    if wheel_name is None:
        msg = "wheel is missing .dist-info/WHEEL metadata"
        raise AssertionError(msg)
    return wheel_name


def _parse_wheel_header(wheel_payload: str, whl_path: Path) -> tuple[str, str]:
    """Extract the maturin generator string and Root-Is-Purelib value.

    Parameters
    ----------
    wheel_payload:
        Decoded text content of the ``.dist-info/WHEEL`` file.
    whl_path:
        Path to the wheel archive; used only in error messages.

    Returns
    -------
    tuple[str, str]
        ``(generator, root_is_purelib)`` extracted from the WHEEL headers.

    Raises
    ------
    AssertionError
        If either field cannot be parsed.
    """
    generator_match = _GENERATOR_RE.search(wheel_payload)
    if generator_match is None:
        msg = f"Could not parse maturin generator from WHEEL metadata: {whl_path}"
        raise AssertionError(msg)
    root_is_purelib = next(
        (
            line.removeprefix("Root-Is-Purelib: ")
            for line in wheel_payload.splitlines()
            if line.startswith("Root-Is-Purelib:")
        ),
        None,
    )
    if root_is_purelib is None:
        msg = "wheel is missing Root-Is-Purelib metadata"
        raise AssertionError(msg)
    return generator_match.group(1), root_is_purelib


def wheel_build_snapshot(whl_path: Path) -> dict[str, typ.Any]:
    """Return a normalised snapshot of wheel metadata and layout.

    Raises
    ------
    AssertionError
        If the wheel metadata is missing expected maturin fields.
    OSError
        If the wheel file cannot be opened or read.
    zipfile.BadZipFile
        If the wheel file is not a valid zip archive.
    """
    with zipfile.ZipFile(whl_path) as archive:
        entry_names = archive.namelist()
        wheel_name = _locate_dist_info_wheel(entry_names)
        metadata_name = wheel_name.replace("/WHEEL", "/METADATA")
        wheel_payload = archive.read(wheel_name).decode("utf-8")
        metadata_payload = archive.read(metadata_name).decode("utf-8")
    generator, root_is_purelib = _parse_wheel_header(wheel_payload, whl_path)
    return {
        "generator": generator,
        "metadata": _parse_metadata(metadata_payload),
        "wheel": {
            "root_is_purelib": root_is_purelib,
            "tag": "<platform-tag>",
        },
        "entries": sorted(_normalise_wheel_entry(name) for name in entry_names),
    }
