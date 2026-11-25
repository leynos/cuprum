"""Fixture edits shared by the tool-pin synchronization contracts.

``scripts/tests/test_sync_tool_pins.py`` drives the command against a scratch
copy of the live pin-bearing files, and each case has to advance or break one
literal. The helpers here make those edits and derive every replacement value
from the copied files, so a live pin bump cannot leave a case failing in its
setup before it has exercised anything.
"""

from __future__ import annotations

import typing as typ

from scripts import tool_pins

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The six files whose pins the command reads or derives.
PIN_FILES = (
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".github/workflows/ci.yml",
    ".github/workflows/build-wheels.yml",
    ".github/actions/build-wheels/action.yml",
)


def text(repository: Path, relative: str) -> str:
    """Return one fixture file's decoded text."""
    return (repository / relative).read_text(encoding="utf-8")


def replace(repository: Path, relative: str, old: str, new: str) -> None:
    """Replace one unique occurrence in a fixture file."""
    body = text(repository, relative)
    if body.count(old) != 1:
        msg = f"{relative} must hold exactly one {old!r}"
        raise AssertionError(msg)
    (repository / relative).write_text(body.replace(old, new), encoding="utf-8")


def to_crlf(repository: Path) -> None:
    """Rewrite every fixture file with CRLF line endings."""
    for relative in PIN_FILES:
        path = repository / relative
        body = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        path.write_bytes(body)


def pin(repository: Path, tool: str) -> str:
    """Return the canonical pin the copied ``pyproject.toml`` declares."""
    return tool_pins.canonical_pins(repository)[tool]


def next_patch(version: str) -> str:
    """Return the next patch release of ``version``."""
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def bump(repository: Path, tool: str, *, lock: bool = True) -> tuple[str, str]:
    """Advance a canonical pin, and the lock entry unless asked not to."""
    old = pin(repository, tool)
    new = next_patch(old)
    replace(repository, "pyproject.toml", f"{tool}=={old}", f"{tool}=={new}")
    if lock:
        replace(
            repository,
            "uv.lock",
            f'name = "{tool}"\nversion = "{old}"',
            f'name = "{tool}"\nversion = "{new}"',
        )
    return old, new


def remove_ruff_requirement(repository: Path) -> None:
    """Drop the canonical Ruff declaration."""
    ruff = pin(repository, "ruff")
    replace(repository, "pyproject.toml", f'    "ruff=={ruff}",\n', "")


def range_ruff_requirement(repository: Path) -> None:
    """Turn the canonical Ruff pin into a range."""
    ruff = pin(repository, "ruff")
    replace(repository, "pyproject.toml", f'"ruff=={ruff}"', f'"ruff>={ruff}"')


def suffix_ruff_requirement(repository: Path) -> None:
    """Append an environment marker to the canonical Ruff pin."""
    ruff = pin(repository, "ruff")
    replace(
        repository,
        "pyproject.toml",
        f'"ruff=={ruff}"',
        f"\"ruff=={ruff}; python_version < '3.14'\"",
    )


def duplicate_ruff_requirement(repository: Path) -> None:
    """Declare Ruff a second time."""
    declaration = f'    "ruff=={pin(repository, "ruff")}",\n'
    replace(repository, "pyproject.toml", declaration, declaration * 2)


def remove_makefile_declaration(repository: Path) -> None:
    """Drop the Makefile Ruff default."""
    declaration = f"RUFF_VERSION ?= {pin(repository, 'ruff')}\n"
    replace(repository, "Makefile", declaration, "")


def duplicate_makefile_declaration(repository: Path) -> None:
    """Declare the Makefile ty default twice."""
    declaration = f"TY_VERSION ?= {pin(repository, 'ty')}\n"
    replace(repository, "Makefile", declaration, declaration * 2)


def empty_leading_makefile_declaration(repository: Path) -> None:
    """Define the Makefile Ruff default empty ahead of the real assignment."""
    declaration = f"RUFF_VERSION ?= {pin(repository, 'ruff')}\n"
    replace(repository, "Makefile", declaration, f"RUFF_VERSION ?=\n{declaration}")


def empty_makefile_declaration(repository: Path) -> None:
    """Empty the Makefile Ruff default."""
    declaration = f"RUFF_VERSION ?= {pin(repository, 'ruff')}\n"
    replace(repository, "Makefile", declaration, "RUFF_VERSION ?=\n")


def indented_leading_makefile_declaration(repository: Path) -> None:
    """Indent a first Makefile Ruff default ahead of the real assignment."""
    declaration = f"RUFF_VERSION ?= {pin(repository, 'ruff')}\n"
    replace(repository, "Makefile", declaration, f" {declaration}{declaration}")


def malformed_leading_makefile_declaration(repository: Path) -> None:
    """Give the first Makefile Ruff default a multi-token value."""
    declaration = f"RUFF_VERSION ?= {pin(repository, 'ruff')}\n"
    replace(
        repository,
        "Makefile",
        declaration,
        f"RUFF_VERSION ?= 0.1.0 stale\n{declaration}",
    )


def multi_token_makefile_declaration(repository: Path) -> None:
    """Give the Makefile Ruff default a multi-token value."""
    ruff = pin(repository, "ruff")
    replace(
        repository,
        "Makefile",
        f"RUFF_VERSION ?= {ruff}\n",
        f"RUFF_VERSION ?= {ruff} stale\n",
    )


def unquoted_workflow_declaration(repository: Path) -> None:
    """Remove the quoting the workflow site depends on."""
    ruff = pin(repository, "ruff")
    replace(
        repository,
        ".github/workflows/ci.yml",
        f"RUFF_VERSION: '{ruff}'",
        f"RUFF_VERSION: {ruff}",
    )


def ambiguous_action_default(repository: Path) -> None:
    """Add a second version-shaped default to the composite action."""
    maturin = pin(repository, "maturin")
    replace(
        repository,
        ".github/actions/build-wheels/action.yml",
        f'    default: "{maturin}"\n',
        f'    default: "{maturin}"\n    default: "9.9.9"\n',
    )
