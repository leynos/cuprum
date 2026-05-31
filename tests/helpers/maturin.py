"""Shared helpers for maturin build and pin contract tests."""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - tests invoke pinned maturin build commands.
import sys
import typing as typ
import zipfile

from tests.helpers.docs import repo_root

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


def expected_maturin_version() -> str:
    """Return the maturin version pinned in ``pyproject.toml``."""
    pyproject = (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
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


def collect_maturin_pins(root: Path | None = None) -> dict[str, str]:
    """Collect maturin version pins from the synchronized locations."""
    base = root or repo_root()
    pyproject = (base / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (base / ".github/workflows/build-wheels.yml").read_text(encoding="utf-8")
    action = (base / ".github/actions/build-wheels/action.yml").read_text(
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


def toolchain_available() -> bool:
    """Return whether the Rust toolchain and maturin are available."""
    return shutil.which("cargo") is not None and shutil.which("rustc") is not None


def build_native_wheel(out_dir: Path) -> Path:
    """Build a native wheel with the pinned maturin version."""
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
        str(repo_root() / "rust/cuprum-rust/Cargo.toml"),
    ]
    subprocess.run(  # noqa: S603 - command list uses only trusted paths and pinned maturin
        command,
        check=True,
        cwd=repo_root(),
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
    """Normalise platform/version wheel entry names to stable placeholders."""
    if _EXTENSION_MODULE_RE.match(name):
        return "cuprum/_rust_backend_native.cpython-<platform>.so"
    if "/sboms/" in name:
        return "cuprum-<version>.dist-info/sboms/<sbom>.cyclonedx.json"
    for suffix, normalised in _DIST_INFO_SUFFIXES.items():
        if name.endswith(suffix):
            return normalised
    return name


def wheel_build_snapshot(whl_path: Path) -> dict[str, typ.Any]:
    """Return a normalised snapshot of wheel metadata and layout."""
    with zipfile.ZipFile(whl_path) as archive:
        wheel_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        )
        metadata_name = wheel_name.replace("/WHEEL", "/METADATA")
        wheel_payload = archive.read(wheel_name).decode("utf-8")
        metadata_payload = archive.read(metadata_name).decode("utf-8")
        generator_match = _GENERATOR_RE.search(wheel_payload)
        if generator_match is None:
            msg = f"Could not parse maturin generator from WHEEL metadata: {whl_path}"
            raise AssertionError(msg)

        return {
            "generator": generator_match.group(1),
            "metadata": _parse_metadata(metadata_payload),
            "wheel": {
                "root_is_purelib": next(
                    line.removeprefix("Root-Is-Purelib: ")
                    for line in wheel_payload.splitlines()
                    if line.startswith("Root-Is-Purelib:")
                ),
                "tag": "<platform-tag>",
            },
            "entries": sorted(
                _normalise_wheel_entry(name) for name in archive.namelist()
            ),
        }
