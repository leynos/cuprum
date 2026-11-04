"""Build child environments with isolated Cargo and pinned Rustup homes."""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def cargo_environment(
    cargo_home: Path,
    caller_environment: cabc.Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment while selecting its Cargo and Rustup home paths.

    Other caller-selected variables, including Cargo job settings, are kept.

    Parameters
    ----------
    cargo_home : Path
        Writable Cargo home owned by the current build run.
    caller_environment : Mapping of str to str, optional
        Environment to copy. Defaults to the current process environment.

    Returns
    -------
    dict of str to str
        Copy with ``CARGO_HOME`` set to ``cargo_home`` and ``RUSTUP_HOME``
        pinned to the caller's configured or default Rustup home.
    """
    environment = dict(os.environ if caller_environment is None else caller_environment)
    return _cargo_environment(cargo_home, _pinned_rustup_home(environment), environment)


def _caller_cargo_home(environment: cabc.Mapping[str, str]) -> Path:
    """Return the caller's Cargo home without logging its contents."""
    return Path(environment.get("CARGO_HOME", str(Path.home() / ".cargo"))).expanduser()


def _pinned_rustup_home(environment: cabc.Mapping[str, str]) -> str:
    """Return the caller's Rustup home, materializing its default path."""
    home = environment.get("RUSTUP_HOME", str(Path.home() / ".rustup"))
    return str(Path(home).expanduser())


def _cargo_environment(
    cargo_home: Path,
    rustup_home: str,
    caller_environment: cabc.Mapping[str, str],
) -> dict[str, str]:
    """Copy the caller environment with both prepared cache paths pinned."""
    environment = dict(caller_environment)
    environment["CARGO_HOME"] = str(cargo_home)
    environment["RUSTUP_HOME"] = rustup_home
    return environment
