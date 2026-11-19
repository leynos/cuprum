"""Read Cuprum's canonical tool pins and the derived sites that mirror them.

``pyproject.toml``'s ``dev`` dependency group is the one authoritative
declaration of the Ruff, ty and maturin versions Cuprum develops and builds
with. Several executable build inputs cannot read that file: the Makefile
defaults, the workflow-level ``env`` pins, and the composite action's
``maturin-version`` default are plain Make or YAML text. This module holds the
canonical reader and the site table shared by the ``check`` and ``sync``
commands, including the structural rules each site must satisfy.

``uv.lock`` is deliberately not rewritten here. uv owns the lockfile and
resolving it can also move unrelated transitive packages, so the reader only
reports an outstanding lock entry as drift that ``uv lock`` must resolve.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: Repository-relative files the pin model reads.
PYPROJECT = "pyproject.toml"
LOCKFILE = "uv.lock"

#: The leading distribution name of a PEP 508 requirement string.
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class PinError(RuntimeError):
    """A pin declaration is missing, malformed or ambiguous."""


@dataclasses.dataclass(frozen=True, slots=True)
class Drift:
    """One site whose value differs from the canonical pin."""

    location: str
    expected: str
    found: str


@dataclasses.dataclass(frozen=True, slots=True)
class Site:
    """One derived literal that must mirror a canonical pin.

    Attributes
    ----------
    relative_path:
        Repository-relative file holding the literal.
    label:
        Human-readable name used in diagnostics.
    pattern:
        Multiline pattern whose ``value`` group is the literal to compare.
    validator:
        Optional extra structural check run once the pattern has matched.
    """

    relative_path: str
    label: str
    pattern: re.Pattern[str]
    validator: cabc.Callable[[str], None] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ToolPin:
    """A canonical tool pin and the derived sites that must follow it."""

    name: str
    sites: tuple[Site, ...]


def _makefile_site(name: str) -> Site:
    """Return the Makefile ``NAME ?= value`` site for one variable."""
    return Site(
        relative_path="Makefile",
        label=f"Makefile {name}",
        pattern=re.compile(
            rf"^(?P<prefix>{re.escape(name)}\s*\?=\s*)(?P<value>[^\s#]+)"
            rf"(?P<suffix>\s*(?:#.*)?)$",
            re.MULTILINE,
        ),
    )


def _workflow_site(name: str) -> Site:
    """Return the single-quoted workflow ``env`` site for one variable."""
    return Site(
        relative_path=".github/workflows/ci.yml",
        label=f"ci.yml {name}",
        pattern=re.compile(
            rf"^(?P<prefix>[ \t]*{re.escape(name)}:\s*')(?P<value>[^']+)"
            rf"(?P<suffix>'[ \t]*)$",
            re.MULTILINE,
        ),
    )


#: The maturin version is a workflow-level ``env`` literal in the wheel
#: workflow and the default of the composite action's ``maturin-version``
#: input. Both are bound to the same canonical pin.
_MATURIN_WORKFLOW_SITE = Site(
    relative_path=".github/workflows/build-wheels.yml",
    label="build-wheels.yml MATURIN_VERSION",
    pattern=re.compile(
        r'^(?P<prefix>[ \t]*MATURIN_VERSION:\s*")(?P<value>[^"]+)'
        r'(?P<suffix>"[ \t]*)$',
        re.MULTILINE,
    ),
)

_ACTION_INPUT_BLOCK_RE = re.compile(
    r"^  maturin-version:\n(?:    [^\n]*\n)*",
    re.MULTILINE,
)


def _require_one_action_default(text: str) -> None:
    """Refuse a ``maturin-version`` block carrying more than one default."""
    block = _ACTION_INPUT_BLOCK_RE.search(text)
    if block is None:
        return
    defaults = re.findall(r'^    default: "', block.group(0), re.MULTILINE)
    if len(defaults) != 1:
        msg = (
            ".github/actions/build-wheels/action.yml: the maturin-version "
            f"default must appear exactly once, found {len(defaults)}"
        )
        raise PinError(msg)


_MATURIN_ACTION_SITE = Site(
    relative_path=".github/actions/build-wheels/action.yml",
    label="build-wheels action maturin-version default",
    pattern=re.compile(
        r"^(?P<prefix>  maturin-version:\n(?:    [^\n]*\n)*?    default: \")"
        r'(?P<value>[^"]+)'
        r'(?P<suffix>"[ \t]*)$',
        re.MULTILINE,
    ),
    validator=_require_one_action_default,
)

#: The canonical tool pins and every literal derived from each one.
TOOLS: tuple[ToolPin, ...] = (
    ToolPin(
        name="ruff",
        sites=(_makefile_site("RUFF_VERSION"), _workflow_site("RUFF_VERSION")),
    ),
    ToolPin(
        name="ty",
        sites=(_makefile_site("TY_VERSION"), _workflow_site("TY_VERSION")),
    ),
    ToolPin(
        name="maturin",
        sites=(_MATURIN_WORKFLOW_SITE, _MATURIN_ACTION_SITE),
    ),
)


def _normalized_name(requirement: str) -> str | None:
    """Return the normalized distribution name of a requirement string."""
    match = _REQUIREMENT_NAME_RE.match(requirement)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _read_utf8(path: Path) -> str:
    """Read a file as UTF-8, naming it in any decoding failure."""
    try:
        return path.read_bytes().decode("utf-8")
    except OSError as error:
        msg = f"cannot read {path}: {error}"
        raise PinError(msg) from error
    except UnicodeDecodeError as error:
        msg = f"{path} is not valid UTF-8: {error}"
        raise PinError(msg) from error


def _dev_group(root: Path) -> list[str]:
    """Return the pyproject dev dependency strings."""
    text = _read_utf8(root / PYPROJECT)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"{PYPROJECT} is not valid TOML: {error}"
        raise PinError(msg) from error
    groups = document.get("dependency-groups")
    if not isinstance(groups, dict):
        msg = f"{PYPROJECT} must declare a dependency-groups table"
        raise PinError(msg)
    dev = groups.get("dev")
    if not isinstance(dev, list):
        msg = f"{PYPROJECT} must declare a dev dependency group"
        raise PinError(msg)
    requirements: list[str] = []
    for entry in dev:
        if not isinstance(entry, str):
            msg = f"{PYPROJECT} dev entries must be strings, found {entry!r}"
            raise PinError(msg)
        requirements.append(entry)
    return requirements


def canonical_pins(root: Path) -> dict[str, str]:
    """Read the exact Ruff, ty and maturin pins from the dev group.

    Parameters
    ----------
    root:
        Repository root holding ``pyproject.toml``.

    Returns
    -------
    dict[str, str]
        Canonical version per tool name.

    Raises
    ------
    PinError
        If a tool is declared zero times or more than once, or its declaration
        is not an exact ``name==X.Y.Z`` requirement.
    """
    requirements = _dev_group(root)
    pins: dict[str, str] = {}
    for tool in TOOLS:
        declarations = [
            requirement
            for requirement in requirements
            if _normalized_name(requirement) == tool.name
        ]
        if len(declarations) != 1:
            msg = (
                f"{PYPROJECT}: expected exactly one {tool.name} requirement in "
                f"the dev group, found {declarations!r}"
            )
            raise PinError(msg)
        declaration = declarations[0]
        match = re.fullmatch(
            rf"{re.escape(tool.name)}==(\d+\.\d+\.\d+)",
            declaration.strip(),
        )
        if match is None:
            msg = (
                f"{PYPROJECT}: {tool.name} must be pinned as an exact "
                f"'{tool.name}==X.Y.Z' release, found {declaration!r}"
            )
            raise PinError(msg)
        pins[tool.name] = match.group(1)
    return pins


def read_text(root: Path, relative: str) -> str:
    """Read one repository-relative file as UTF-8.

    Parameters
    ----------
    root:
        Repository root.
    relative:
        Repository-relative path of the file to read.

    Returns
    -------
    str
        The file's decoded text.

    Raises
    ------
    PinError
        If the file cannot be read or is not valid UTF-8.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the reader this delegates to
    return _read_utf8(root / relative)


def match_site(text: str, site: Site) -> re.Match[str]:
    """Return a site's one match, refusing absent or ambiguous declarations.

    Parameters
    ----------
    text:
        The full text of the file holding the site.
    site:
        The derived literal's declaration rule.

    Returns
    -------
    re.Match[str]
        The single match whose ``value`` group holds the literal.

    Raises
    ------
    PinError
        If the site's declaration is absent, ambiguous or structurally
        malformed.
    """
    matches = list(site.pattern.finditer(text))
    if len(matches) != 1:
        msg = (
            f"{site.relative_path}: expected exactly one {site.label} "
            f"declaration, found {len(matches)}"
        )
        raise PinError(msg)
    if site.validator is not None:
        site.validator(text)
    return matches[0]


def sites_by_file() -> dict[str, list[tuple[Site, str]]]:
    """Group every derived site by the file that holds it."""
    grouped: dict[str, list[tuple[Site, str]]] = {}
    for tool in TOOLS:
        for site in tool.sites:
            grouped.setdefault(site.relative_path, []).append((site, tool.name))
    return grouped


def derived_drift(root: Path, pins: dict[str, str]) -> list[Drift]:
    """Return every derived literal that differs from its canonical pin."""
    drifts: list[Drift] = []
    for tool in TOOLS:
        for site in tool.sites:
            text = read_text(root, site.relative_path)
            value = match_site(text, site).group("value")
            if value != pins[tool.name]:
                drifts.append(Drift(site.label, pins[tool.name], value))
    return drifts


def lock_drift(root: Path, pins: dict[str, str]) -> list[Drift]:
    """Return lock entries that already disagree with the canonical pins."""
    text = _read_utf8(root / LOCKFILE)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"{LOCKFILE} is not valid TOML: {error}"
        raise PinError(msg) from error
    packages = document.get("package")
    if not isinstance(packages, list):
        msg = f"{LOCKFILE} must declare a package table"
        raise PinError(msg)
    drifts: list[Drift] = []
    for tool in TOOLS:
        versions = [
            entry.get("version")
            for entry in packages
            if isinstance(entry, dict) and entry.get("name") == tool.name
        ]
        if len(versions) != 1:
            msg = (
                f"{LOCKFILE}: expected exactly one package entry for "
                f"{tool.name}, found {len(versions)}"
            )
            raise PinError(msg)
        locked = versions[0]
        if not isinstance(locked, str):
            msg = f"{LOCKFILE}: {tool.name} must carry a string version"
            raise PinError(msg)
        if locked != pins[tool.name]:
            drifts.append(Drift(f"{LOCKFILE} {tool.name}", pins[tool.name], locked))
    return drifts


def check_pins(root: Path) -> list[Drift]:
    """Return every drifted site without writing any file.

    Parameters
    ----------
    root:
        Repository root.

    Returns
    -------
    list[Drift]
        Empty when every derived input and lock entry agrees with the
        canonical pins.

    Raises
    ------
    PinError
        If a canonical pin or a site declaration is missing, malformed or
        ambiguous.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the pin readers this delegates to
    pins = canonical_pins(root)
    return [*derived_drift(root, pins), *lock_drift(root, pins)]
