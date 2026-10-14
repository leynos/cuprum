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

#: The reviewed unit size, in nose IL tokens, that the gate is pinned to.
#:
#: Both directions of drift are scope changes. A lower floor admits families
#: the reviewed surface excluded; a higher one hides families the reviewed
#: surface included. Neither is visible in the gate's output, so the contract
#: pins the value rather than bounding it.
MINIMUM_SIZE_FLOOR = 24

#: The surface name that admits every family, hidden ones included.
WIDENED_SURFACE = "all"

#: nose's unbounded ranking, spelled as an explicit bound of zero.
#:
#: This is the only setting under which the stale-entry report is trustworthy.
#: `partition_findings` marks an entry stale when no returned family matches it,
#: and the returned families are the ranked ones the bound admits, so any
#: positive bound hides the tail of the ranking and reports the entries
#: covering that tail as removable. Measured on this tree, a bound of 30
#: falsely reports 24 of the 52 entries as stale. A contract that required a
#: positive bound would therefore require the failure mode.
UNBOUNDED_TOP = 0


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


def test_size_floor_and_ranking_bound_adjudicate_the_whole_surface() -> None:
    """The size floor is exactly the reviewed one, and the ranking is unbounded."""
    table = _nose_table()
    min_size = table.get("min-size")
    top = table.get("top")
    assert isinstance(min_size, int), "tool.nose.min-size must be an integer."
    # Equality rather than a floor comparison. A `>=` comparison only guards
    # against lowering; it accepts any raising, and raising narrows — measured
    # on this tree, min-size 16 admits 104 families, 24 admits 62, and 40 admits
    # 43. Narrowing is the same defect mirrored: a raised floor hides small
    # families below it exactly as a lowered one admits noise above it. Neither
    # direction is adjudicated by the gate, because a family under the floor is
    # never reported, so a silent change either way moves the surface the gate
    # claims to enforce. An approved change to the floor changes this constant
    # in the same commit.
    assert min_size == MINIMUM_SIZE_FLOOR, (
        f"tool.nose.min-size must be exactly {MINIMUM_SIZE_FLOOR}; found "
        f"{min_size!r}. Raising it hides families below the new floor without "
        "adjudicating them, which is a scope change, not a tuning knob."
    )
    assert top == UNBOUNDED_TOP, (
        f"tool.nose.top must be {UNBOUNDED_TOP} to adjudicate the whole "
        f"surface; any positive bound hides the tail of the ranking and "
        f"reports its allow entries as stale. Found {top!r}."
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
