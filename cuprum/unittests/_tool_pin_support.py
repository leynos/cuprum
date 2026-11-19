"""Structured readers for the repository's tool pins.

The Ruff, ty, Pylint, Astroid and maturin versions are declared once in the
``pyproject.toml`` dev group and repeated in ``uv.lock``, the Makefile
``NAME ?=`` defaults, and the workflow and composite-action literals. The
parity tests read every site, and they have to read the *structure* of each
declaration — how many there are, and whether the value is an exact release —
rather than search for a version string. A search would accept a duplicate
declaration, a ranged requirement, or a value carrying an environment marker;
all three would leave the parity assertions passing over a pin that no longer
fixes anything.

This module is deliberately independent of ``scripts/tool_pins.py``. The
script decides what the repository *should* contain and rewrites it; these
readers decide what a *test observed*. Sharing one implementation would let a
misreading in the writer hide behind the same misreading in the assertion.

``uv.lock`` is read, never written. uv owns the lockfile, so a lock entry that
disagrees with the canonical pin is reported as drift here and resolved by
``uv lock``.
"""

from __future__ import annotations

import re
import tomllib
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

#: An exact release version: dotted digit groups and nothing else.
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

#: The leading distribution name of a PEP 508 requirement string.
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

#: An exact ``name==X.Y.Z`` requirement, matched against the whole string so a
#: range, an environment marker, or an extra leaves the declaration unmatched.
_EXACT_REQUIREMENT_RE = re.compile(r"[A-Za-z0-9._-]+==(\d+\.\d+\.\d+)")


def read_text(root: pth.Path, relative: str) -> str:
    """Read a repository-relative UTF-8 text file.

    Parameters
    ----------
    root : pathlib.Path
        The repository root, from `tests.helpers.docs.repo_root`.
    relative : str
        The path to read, relative to `root`.

    Returns
    -------
    str
        The file's decoded contents.

    Raises
    ------
    OSError
        If the file cannot be read — most often `FileNotFoundError` when a
        pinned CI file has been renamed or removed. Deliberately not caught:
        these tests assert that specific repository files carry specific pins,
        so a missing file is a genuine failure and its own traceback names the
        path more precisely than any wrapper would.
    UnicodeDecodeError
        If the file is not valid UTF-8. Every file read here is repository
        source, so this indicates corruption rather than an input to handle.

    Notes
    -----
    Deliberately uncached. A full run of the pin tests reads a handful of
    small files, and a memoized read would serve stale content to any future
    test that writes one. Paying that correctness hazard for a saving far
    below the noise is the wrong trade.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> "maturin" in read_text(repo_root(), "pyproject.toml")
    True
    """  # ruff: ignore[docstring-extraneous-exception] - propagates filesystem decoding and access errors
    return (root / relative).read_text(encoding="utf-8")


def _load_toml(root: pth.Path, relative: str) -> dict[str, object]:
    """Parse one repository-relative TOML document."""
    return tomllib.loads(read_text(root, relative))


def _normalized_name(requirement: str) -> str | None:
    """Return a requirement's PEP 503 normalized distribution name."""
    match = _REQUIREMENT_NAME_RE.match(requirement)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def read_dev_group(root: pth.Path) -> list[str]:
    """Read the ``pyproject.toml`` dev dependency group as requirement strings.

    Parameters
    ----------
    root : pathlib.Path
        The repository root.

    Returns
    -------
    list[str]
        Every requirement string in the ``dev`` group, in file order.

    Notes
    -----
    Fails the contract when ``pyproject.toml`` declares no dependency-groups
    table, no ``dev`` group, or any entry that is not a string.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> any(item.startswith("ruff==") for item in read_dev_group(repo_root()))
    True
    """
    document = _load_toml(root, "pyproject.toml")
    groups = document.get("dependency-groups")
    assert isinstance(groups, dict), "pyproject.toml must declare dependency-groups"
    dev = groups.get("dev")
    assert isinstance(dev, list), "pyproject.toml must declare a dev group"
    requirements: list[str] = []
    for entry in typ.cast("list[object]", dev):
        assert isinstance(entry, str), f"dev entries must be strings, found {entry!r}"
        requirements.append(entry)
    return requirements


def read_pyproject_pin(root: pth.Path, package: str) -> str:
    """Read one package's exact pin from the dev dependency group.

    The declaration must appear exactly once and must be an exact
    ``package==X.Y.Z`` requirement. A duplicate, a range, or a suffix is a
    failure rather than a value to interpret: each of them leaves the
    repository without a single version the other sites can mirror.

    Parameters
    ----------
    root : pathlib.Path
        The repository root.
    package : str
        The distribution name, such as ``"maturin"``.

    Returns
    -------
    str
        The pinned version, as ``"MAJOR.MINOR.PATCH"``.

    Notes
    -----
    Fails the contract when the declaration is absent, duplicated, or not an
    exact ``package==X.Y.Z`` release.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> read_pyproject_pin(repo_root(), "maturin").count(".")
    2
    """
    wanted = re.sub(r"[-_.]+", "-", package).lower()
    declarations = [
        requirement
        for requirement in read_dev_group(root)
        if _normalized_name(requirement) == wanted
    ]
    assert len(declarations) == 1, (
        f"expected exactly one {package} requirement in the pyproject dev "
        f"group, found {declarations!r}"
    )
    exact = _EXACT_REQUIREMENT_RE.fullmatch(declarations[0].strip())
    assert exact is not None, (
        f"{package} must be pinned as an exact '{package}==X.Y.Z' release, "
        f"found {declarations[0]!r}"
    )
    return exact.group(1)


def read_lock_pin(root: pth.Path, package: str) -> str:
    """Read one package's resolved version from ``uv.lock``.

    Parameters
    ----------
    root : pathlib.Path
        The repository root.
    package : str
        The distribution name, such as ``"maturin"``.

    Returns
    -------
    str
        The locked version recorded for that package.

    Notes
    -----
    Fails the contract when the lockfile records the package zero times or more
    than once, or gives its version as anything but a string.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> read_lock_pin(repo_root(), "maturin").count(".")
    2
    """
    document = _load_toml(root, "uv.lock")
    packages = document.get("package")
    assert isinstance(packages, list), "uv.lock must declare a package list"
    versions = [
        entry.get("version")
        for entry in typ.cast("list[object]", packages)
        if isinstance(entry, dict) and entry.get("name") == package
    ]
    assert len(versions) == 1, (
        f"uv.lock must record exactly one {package} entry, found {len(versions)}"
    )
    locked = versions[0]
    assert isinstance(locked, str), f"uv.lock must give {package} a string version"
    return locked


def read_single_match(
    text: str,
    pattern: re.Pattern[str],
    *,
    location: str,
    subject: str,
) -> str:
    r"""Return a declaration's one capture, refusing absent or duplicate matches.

    Parameters
    ----------
    text : str
        The full text of the file holding the declaration.
    pattern : re.Pattern[str]
        A pattern whose first group captures the pinned value.
    location : str
        The repository-relative path, named in the failure.
    subject : str
        What was being looked for, named in the failure.

    Returns
    -------
    str
        The first capture group of the single match.

    Notes
    -----
    Fails the contract when the pattern matches zero times or more than once.

    Examples
    --------
    >>> import re
    >>> read_single_match(
    ...     "MATURIN_VERSION: '1.2.3'\n",
    ...     re.compile(r"MATURIN_VERSION:\s*'(\S+)'"),
    ...     location="ci.yml",
    ...     subject="maturin pin",
    ... )
    '1.2.3'
    """
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, (
        f"{location} must declare exactly one {subject}, found {len(matches)}"
    )
    return matches[0].group(1)


def read_makefile_pin(root: pth.Path, name: str) -> str:
    """Read one ``NAME ?= value`` default from the repository Makefile.

    Parameters
    ----------
    root : pathlib.Path
        The repository root.
    name : str
        The Make variable, such as ``"RUFF_VERSION"``.

    Returns
    -------
    str
        The declared value.

    Notes
    -----
    Fails the contract when the Makefile declares the variable zero times or
    more than once. A second default is the failure that matters: ``?=`` keeps
    only the first, so a duplicate would leave the later line inert while the
    parity test still read it.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> read_makefile_pin(repo_root(), "RUFF_VERSION").count(".")
    2
    """
    # A trailing comment is part of the line but not of the value: Make keeps
    # the assignment and discards the comment, so a reader that skipped such a
    # line would miss the default that is actually in force.
    pattern = re.compile(
        rf"^{re.escape(name)}\s*\?=\s*([^\s#]+)\s*(?:#.*)?$",
        re.MULTILINE,
    )
    return read_single_match(
        read_text(root, "Makefile"),
        pattern,
        location="Makefile",
        subject=f"{name} default",
    )
