"""Wheel-artefact inspection helpers for maturin build contract tests.

Split out of :mod:`tests.helpers.maturin` to keep each module focused: the
parent module owns pin/version parsing, toolchain probing, and the wheel
*build* invocation, while this module owns parsing a *built* wheel archive
into the stable snapshot consumed by ``test_maturin_wheel_build_snapshot``.
``tests.helpers.maturin`` re-exports :func:`wheel_build_snapshot` so existing
import sites remain unchanged.
"""

from __future__ import annotations

import re
import typing as typ
import zipfile

if typ.TYPE_CHECKING:
    from pathlib import Path

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


class WheelMetadata(typ.TypedDict):
    """Normalized ``.dist-info/METADATA`` fields captured in the snapshot.

    Attributes
    ----------
    name : str | None
        Value of the ``Name`` metadata header, or ``None`` if absent.
    version : str | None
        Value of the ``Version`` metadata header, or ``None`` if absent.
    requires_python : str | None
        Value of the ``Requires-Python`` header, or ``None`` if absent.
    requires_dist : list[str]
        Sorted values of the ``Requires-Dist`` headers.
    classifiers : list[str]
        Sorted values of the ``Classifier`` headers.
    """

    name: str | None
    version: str | None
    requires_python: str | None
    requires_dist: list[str]
    classifiers: list[str]


class WheelHeaders(typ.TypedDict):
    """Normalized ``.dist-info/WHEEL`` fields captured in the snapshot.

    Attributes
    ----------
    root_is_purelib : str
        Raw value of the ``Root-Is-Purelib`` header.
    tag : str
        Always the literal placeholder ``"<platform-tag>"``, normalized so
        snapshots stay platform-independent.
    """

    root_is_purelib: str
    tag: str


class WheelBuildSnapshot(typ.TypedDict):
    """Stable snapshot of a built wheel's metadata and layout.

    Attributes
    ----------
    generator : str
        Maturin version parsed from the WHEEL ``Generator`` header.
    metadata : WheelMetadata
        Normalized ``.dist-info/METADATA`` fields.
    wheel : WheelHeaders
        Normalized ``.dist-info/WHEEL`` fields.
    entries : list[str]
        Sorted, normalized archive entry names.
    """

    generator: str
    metadata: WheelMetadata
    wheel: WheelHeaders
    entries: list[str]


def _header_value(headers: dict[str, list[str]], key: str) -> str | None:
    """Return the first header value for the given key, or None if absent."""
    values = headers.get(key)
    if not values:
        return None
    return values[0]


def _parse_metadata(raw_metadata: str) -> WheelMetadata:
    """Parse RFC 2822-style metadata headers into a normalized dict."""
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


def _normalize_wheel_entry(name: str) -> str:
    """Normalize platform/version wheel entry names to stable placeholders."""
    if _EXTENSION_MODULE_RE.match(name):
        return "cuprum/_rust_backend_native.cpython-<platform>.so"
    if "/sboms/" in name:
        return "cuprum-<version>.dist-info/sboms/<sbom>.cyclonedx.json"
    for suffix, normalized in _DIST_INFO_SUFFIXES.items():
        if name.endswith(suffix):
            return normalized
    return name


def _locate_dist_info_wheel(entry_names: list[str]) -> str:
    """Return the .dist-info/WHEEL entry name from a wheel archive's namelist."""
    wheel_name = next(
        (name for name in entry_names if name.endswith(".dist-info/WHEEL")),
        None,
    )
    if wheel_name is None:
        msg = "wheel is missing .dist-info/WHEEL metadata"
        raise AssertionError(msg)
    return wheel_name


def _parse_wheel_header(wheel_payload: str, whl_path: Path) -> tuple[str, str]:
    """Extract the maturin generator string and Root-Is-Purelib value."""
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


def wheel_build_snapshot(whl_path: Path) -> WheelBuildSnapshot:
    """Return a normalized snapshot of wheel metadata and layout.

    Parameters
    ----------
    whl_path : Path
        Path to the built wheel archive to inspect.

    Returns
    -------
    WheelBuildSnapshot
        The normalized snapshot (generator string, parsed metadata, wheel
        headers, and normalized entry list).

    Raises
    ------
    AssertionError
        If the wheel is missing its ``.dist-info/METADATA`` member or the
        expected maturin metadata fields.
    OSError
        If the wheel file cannot be opened or read.
    zipfile.BadZipFile
        If the wheel file is not a valid zip archive.
    """
    with zipfile.ZipFile(whl_path) as archive:
        entry_names = archive.namelist()
        wheel_name = _locate_dist_info_wheel(entry_names)
        metadata_name = wheel_name.replace("/WHEEL", "/METADATA")
        if metadata_name not in entry_names:
            msg = "wheel is missing .dist-info/METADATA metadata"
            raise AssertionError(msg)
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
        "entries": sorted(_normalize_wheel_entry(name) for name in entry_names),
    }
