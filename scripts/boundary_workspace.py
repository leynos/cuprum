"""Copy the Rust boundary workspace without changing Cargo target topology."""

from __future__ import annotations

import os
import shutil
import stat
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def copy_workspace(
    source: Path, destination: Path, source_targets: cabc.Iterable[Path]
) -> None:
    """Copy one workspace, materializing safe source links before mutation."""
    source = _lexical_absolute(source)
    targets = tuple(source_targets)
    copied_targets = tuple(
        _copied_target_path(source, destination, target) for target in targets
    )
    copy_source_tree(source, destination)
    for target, copied_target in zip(targets, copied_targets, strict=True):
        if _is_symlink(target):
            copied_target.unlink()
            shutil.copyfile(target, copied_target)
        _make_writable(copied_target)


def copy_source_tree(source: Path, destination: Path) -> None:
    """Copy a workspace tree while ignoring only root Cargo build output."""
    source = _lexical_absolute(source)

    def ignore_root_build_output(directory: str, _names: list[str]) -> set[str]:
        """Ignore Cargo build output only when copying the workspace root."""
        return {"target"} if Path(directory) == source else set()

    shutil.copytree(source, destination, ignore=ignore_root_build_output, symlinks=True)


def _lexical_absolute(path: Path) -> Path:
    """Normalize a path without resolving its symlink topology."""
    return Path(os.path.normpath(Path.cwd() / path))


def _copied_target_path(source: Path, destination: Path, target: Path) -> Path:
    """Map a target into the copy only when no directory link can escape it."""
    lexical_target = _lexical_absolute(target)
    try:
        relative = lexical_target.relative_to(source)
    except ValueError as error:
        msg = f"source target escapes the Rust workspace: {target}"
        raise ValueError(msg) from error
    _reject_symlinked_parents(source, relative, target)
    return destination / relative


def _reject_symlinked_parents(source: Path, relative: Path, target: Path) -> None:
    """Reject target paths whose copied parent would still point outside the copy."""
    parent = source
    for component in relative.parts[:-1]:
        parent /= component
        if _is_symlink(parent):
            msg = f"source target has a symlinked parent directory: {target}"
            raise ValueError(msg)


def _is_symlink(path: Path) -> bool:
    """Inspect a source target without turning access errors into omissions."""
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except OSError as error:
        msg = f"cannot inspect source target {path}: {error}"
        raise ValueError(msg) from error


def _make_writable(path: Path) -> None:
    """Give the copied probe target an owner write bit without altering its source."""
    try:
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
    except OSError as error:
        msg = f"cannot make copied source target writable {path}: {error}"
        raise ValueError(msg) from error
