"""Validate the reviewed crate inventory and inherited workspace lints."""

from __future__ import annotations

import tomllib
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_BOUNDARIES = frozenset({"cuprum-rust", "cuprum-native-io"})
_SAFE_CRATES = frozenset({"cuprum-streams"})


def check_members(manifest: str) -> None:
    """Require review before the audited crate set can grow.

    Parameters
    ----------
    manifest : str
        TOML text containing the workspace member list.

    Raises
    ------
    ValueError
        If the workspace member list is missing, malformed, or unaudited.
    tomllib.TOMLDecodeError
        If ``manifest`` is not valid TOML.
    """  # ruff: ignore[docstring-extraneous-exception] - validation failures propagate from the parser and manifest checker.
    _check_members_manifest(tomllib.loads(manifest))


def _check_members_manifest(manifest: cabc.Mapping[str, object]) -> None:
    """Validate audited workspace membership from parsed manifest data."""
    workspace = manifest.get("workspace")
    members = workspace.get("members") if isinstance(workspace, dict) else None
    expected_members = _BOUNDARIES | _SAFE_CRATES
    if not isinstance(members, list) or not all(
        isinstance(member, str) for member in members
    ):
        msg = "workspace membership differs from the audited boundary inventory"
        raise ValueError(msg)
    if set(members) != expected_members or len(members) != len(expected_members):
        msg = "workspace membership differs from the audited boundary inventory"
        raise ValueError(msg)


def check_member_lint_inheritance(workspace: Path) -> None:
    """Require each audited package to inherit the workspace lint baseline.

    Parameters
    ----------
    workspace : Path
        Root of the Rust workspace containing audited member manifests.

    Raises
    ------
    ValueError
        If an audited package replaces or weakens the shared policy locally.
    """
    for member in _BOUNDARIES | _SAFE_CRATES:
        manifest_path = workspace / member / "Cargo.toml"
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("lints") != {"workspace": True}:
            msg = f"{member} must inherit the workspace lint baseline"
            raise ValueError(msg)
