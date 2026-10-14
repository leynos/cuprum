"""Contract tests for the configured nose scan scope.

``[tool.nose]`` decides what the blocking duplication gate can see, so a scope
misconfiguration is indistinguishable from a clean result: a root that does not
exist, or an exclusion spelled relative to the wrong directory, leaves the gate
reporting success over a surface nobody chose. These tests read the checked-in
configuration and the tree it names, and fail rather than let an empty or
mistyped scope masquerade as a passing gate.
"""

from __future__ import annotations

import re
import tomllib
import typing as typ
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
MAKEFILE = REPOSITORY_ROOT / "Makefile"

#: The Makefile's detector pin, as the installer target defines it.
MAKEFILE_PIN_PATTERN = re.compile(r"^NOSE_VERSION\s*\?=\s*(\S+)\s*$", re.MULTILINE)

#: The detection channels the gate may select, and must select in full.
KNOWN_CHANNELS = ("syntax", "semantic", "near")

#: The smallest unit size, in nose IL tokens, the gate may be pinned to. A
#: lower floor hides small families rather than adjudicating them.
MINIMUM_SIZE_FLOOR = 24

#: The surface name that admits every family, hidden ones included.
WIDENED_SURFACE = "all"


def _nose_table() -> dict[str, object]:
    """Return the raw ``[tool.nose]`` table from the repository manifest.

    Returns
    -------
    dict[str, object]
        The configuration table exactly as checked in.
    """
    with PYPROJECT.open("rb") as handle:
        data: dict[str, typ.Any] = tomllib.load(handle)
    return typ.cast("dict[str, object]", data["tool"]["nose"])


def _roots() -> tuple[str, ...]:
    """Return the configured scan roots.

    Returns
    -------
    tuple[str, ...]
        Repository-relative root paths, in configured order.
    """
    roots = _nose_table().get("roots")
    assert isinstance(roots, list), "tool.nose.roots must be a TOML array"
    return tuple(typ.cast("list[str]", roots))


def _exclude_globs() -> tuple[str, ...]:
    """Return the configured exclusion globs.

    Returns
    -------
    tuple[str, ...]
        Globs matched against each root-relative path, not the repository one.
    """
    globs = _nose_table().get("exclude", [])
    assert isinstance(globs, list), "tool.nose.exclude must be a TOML array"
    return tuple(typ.cast("list[str]", globs))


def _discovered_python_files(root: str) -> list[Path]:
    """Return the Python files beneath one root, minus bytecode caches.

    Returns
    -------
    list[Path]
        Absolute paths of the candidate source files.
    """
    directory = REPOSITORY_ROOT / root
    return [
        path
        for path in sorted(directory.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _relative_to(path: Path, root: str) -> PurePosixPath:
    """Return ``path`` expressed relative to one configured root.

    Returns
    -------
    PurePosixPath
        The root-relative path the detector matches exclusion globs against.
    """
    return PurePosixPath(path.relative_to(REPOSITORY_ROOT / root).as_posix())


def _selected_files(root: str) -> list[Path]:
    """Return the Python files nose selects beneath one configured root.

    Returns
    -------
    list[Path]
        Files beneath an existing root, minus caches and excluded paths.
    """
    globs = _exclude_globs()
    return [
        path
        for path in _discovered_python_files(root)
        if not any(_relative_to(path, root).full_match(glob) for glob in globs)
    ]


def _existing_roots() -> tuple[str, ...]:
    """Return the configured roots that really exist as directories.

    Returns
    -------
    tuple[str, ...]
        Existing repository-relative roots, in configured order.
    """
    return tuple(root for root in _roots() if (REPOSITORY_ROOT / root).is_dir())


def test_every_configured_root_exists() -> None:
    """Each configured root is a real directory under the repository root."""
    missing = [root for root in _roots() if not (REPOSITORY_ROOT / root).is_dir()]
    assert not missing, (
        f"tool.nose.roots names directories that do not exist: {missing}."
    )


def test_roots_are_configured() -> None:
    """The gate names at least one root rather than scanning nothing."""
    assert _roots(), "tool.nose.roots must name at least one scan root."


def test_a_configured_root_really_yields_python_files() -> None:
    """Some configured root holds Python files the detector would select."""
    selected = {root: len(_selected_files(root)) for root in _existing_roots()}
    assert any(count > 0 for count in selected.values()), (
        f"No configured root yields a selectable Python file: {selected}."
    )


def test_exclusions_match_files_beneath_their_root() -> None:
    """Every exclusion matches a real file, so no glob is silently inert."""
    unmatched = [
        glob
        for glob in _exclude_globs()
        if not any(
            _relative_to(path, root).full_match(glob)
            for root in _existing_roots()
            for path in _discovered_python_files(root)
        )
    ]
    assert not unmatched, (
        "An exclude glob matches no file beneath any configured root, so the "
        f"scan is wider than its label claims: {unmatched}."
    )


def test_mode_selects_every_known_channel() -> None:
    """The configured channels are exactly the known detection channels."""
    mode = _nose_table().get("mode")
    assert isinstance(mode, str), "tool.nose.mode must be a string"
    channels = tuple(part.strip() for part in mode.split(",") if part.strip())
    unknown = sorted(set(channels) - set(KNOWN_CHANNELS))
    assert not unknown, f"tool.nose.mode names unknown channels: {unknown}."
    assert set(channels) == set(KNOWN_CHANNELS), (
        f"tool.nose.mode must select every known channel: {KNOWN_CHANNELS}."
    )


def test_size_floor_and_ranking_bound_are_positive() -> None:
    """The size floor is at least the minimum, and the ranking bound is positive."""
    table = _nose_table()
    min_size = table.get("min-size")
    top = table.get("top")
    assert isinstance(min_size, int), "tool.nose.min-size must be an integer."
    assert min_size >= MINIMUM_SIZE_FLOOR, (
        f"tool.nose.min-size must be at least {MINIMUM_SIZE_FLOOR}."
    )
    assert isinstance(top, int), "tool.nose.top must be an integer."
    assert top > 0, (
        "tool.nose.top must be a positive integer bounding the adjudicated view."
    )


def test_surface_admits_every_family() -> None:
    """The configured surface is the widened one, not nose's ranked dashboard."""
    assert _nose_table().get("surface") == WIDENED_SURFACE, (
        f"tool.nose.surface must be {WIDENED_SURFACE!r}."
    )


def test_makefile_pins_the_configured_version() -> None:
    """The Makefile's detector pin agrees with the manifest's pinned version."""
    match = MAKEFILE_PIN_PATTERN.search(MAKEFILE.read_text(encoding="utf-8"))
    assert match is not None, "the Makefile must define NOSE_VERSION"
    version = _nose_table().get("version")
    assert isinstance(version, str), "tool.nose.version must be a string"
    assert match.group(1) == version, (
        f"The Makefile installs NOSE_VERSION {match.group(1)!r} but the gate "
        f"pins {version!r}."
    )
