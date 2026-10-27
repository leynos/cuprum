#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Check audited workspace lint inheritance and safe-crate unsafe boundaries.

The isolated copy preserves Cargo target topology and leaves source untouched.
"""

from __future__ import annotations

import itertools
import stat
import sys
import tomllib
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from scripts.boundary_cargo import (
    boundary_cargo_run as _boundary_cargo_run,
)
from scripts.boundary_cargo import (
    new_run_root as _new_boundary_run_root,
)
from scripts.boundary_cargo import (
    prepare_run_cargo_home as _prepare_boundary_cargo_home,
)
from scripts.boundary_compile import CompileTargets
from scripts.boundary_compile import _compile as _compile_workspace
from scripts.boundary_members import (
    _check_members_manifest,
    check_member_lint_inheritance,
)
from scripts.boundary_probe import (
    UNSAFE_PROBES as PROBES,
)
from scripts.boundary_probe import _ProbeConfiguration
from scripts.boundary_probe import (
    check_target as _check_probe_target,
)
from scripts.boundary_workspace import copy_workspace

ROOT = Path(__file__).resolve().parent.parent
AUTOMATIC_TARGET_ROOTS = (("autolib", "src/lib.rs"), ("autobins", "src/main.rs"))
AUTOMATIC_TARGET_DIRECTORIES = (
    ("autobins", "src/bin"),
    ("autoexamples", "examples"),
    ("autotests", "tests"),
    ("autobenches", "benches"),
)
EXPLICIT_TARGET_TYPES = ("lib", "bin", "example", "test", "bench")


def safe_target_roots(workspace: Path) -> tuple[Path, ...]:
    """Return effective automatic and explicit Cargo target roots.

    Explicit tables replace matching conventional roots, as Cargo metadata does.

    Returns
    -------
    tuple[Path, ...]
        Effective crate-root source paths.
    """
    crate = workspace / "cuprum-streams"
    manifest = _load_toml(crate / "Cargo.toml")
    package = manifest.get("package")
    package = package if isinstance(package, dict) else {}
    automatic = _load_automatic_target_roots(crate, package)
    explicit = _explicit_target_roots(crate, manifest, package)
    overrides = _automatic_roots_overridden_by_explicit_targets(
        crate, manifest, package, automatic
    )
    active_automatic = (path for path in automatic if path not in overrides)
    return discover_safe_target_roots(active_automatic, explicit)


def discover_safe_target_roots(
    automatic: cabc.Iterable[Path], explicit: cabc.Iterable[Path]
) -> tuple[Path, ...]:
    """Deduplicate automatic and explicit crate roots.

    Returns
    -------
    tuple[Path, ...]
        Sorted effective crate-root source paths.
    """
    return tuple(sorted(set(itertools.chain(automatic, explicit))))


def evaluate_safe_target_policy(sources: cabc.Mapping[Path, str]) -> tuple[Path, ...]:
    """Require every loaded source to forbid unsafe code.

    Returns
    -------
    tuple[Path, ...]
        Validated crate-root source paths.

    Raises
    ------
    ValueError
        If no target root exists or a root lacks the required prohibition.
    """
    if not sources:
        msg = "safe crate has no discoverable Cargo target roots"
        raise ValueError(msg)
    for target, source in sources.items():
        if "#![forbid(unsafe_code)]" not in source:
            msg = f"safe target must forbid unsafe code: {target}"
            raise ValueError(msg)
    return tuple(sources)


def _load_toml(path: Path) -> cabc.Mapping[str, object]:
    """Read and parse a manifest while retaining its path on failure."""
    text = _read_text(path)
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"cannot parse TOML manifest {path}: {error}"
        raise ValueError(msg) from error
    return manifest


def _read_text(path: Path) -> str:
    """Read UTF-8 text and fail closed when inspection is impossible."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        msg = f"cannot read {path}: {error}"
        raise ValueError(msg) from error


def _load_automatic_target_roots(
    crate: Path, package: cabc.Mapping[str, object]
) -> tuple[Path, ...]:
    """Load existing automatic Cargo target roots enabled by package switches."""
    direct = (
        crate / path
        for switch, path in AUTOMATIC_TARGET_ROOTS
        if package.get(switch) is not False
    )
    directories = (
        crate / directory
        for switch, directory in AUTOMATIC_TARGET_DIRECTORIES
        if package.get(switch) is not False
    )
    candidates = itertools.chain(
        direct,
        itertools.chain.from_iterable(
            _load_directory_target_roots(path) for path in directories
        ),
        (crate / "build.rs",) if package.get("build", True) is True else (),
    )
    return tuple(path for path in candidates if _is_mode(path, stat.S_ISREG))


def _load_directory_target_roots(directory: Path) -> cabc.Iterator[Path]:
    """Yield flat and nested conventional Cargo target roots."""
    for entry in _directory_entries(directory):
        if entry.name.startswith("."):
            continue
        if entry.suffix == ".rs" and _is_mode(entry, stat.S_ISREG):
            yield entry
            continue
        if _is_mode(entry, stat.S_ISLNK, follow=False):
            continue
        if not _is_mode(entry, stat.S_ISDIR):
            continue
        candidate = entry / "main.rs"
        if _is_mode(candidate, stat.S_ISREG):
            yield candidate


def _directory_entries(directory: Path) -> tuple[Path, ...]:
    """List an automatic-target directory without masking access failures."""
    try:
        return tuple(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except OSError as error:
        msg = f"cannot inspect automatic target directory {directory}: {error}"
        raise ValueError(msg) from error


def _is_mode(
    path: Path, check: cabc.Callable[[int], bool], *, follow: bool = True
) -> bool:
    """Inspect one target path with a stat-mode predicate."""
    try:
        return check(path.stat(follow_symlinks=follow).st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        msg = f"cannot inspect target path {path}: {error}"
        raise ValueError(msg) from error


def _automatic_roots_overridden_by_explicit_targets(
    crate: Path,
    manifest: cabc.Mapping[str, object],
    package: cabc.Mapping[str, object],
    automatic: cabc.Iterable[Path],
) -> frozenset[Path]:
    """Identify automatic roots replaced by explicit Cargo target tables."""
    candidates = itertools.chain.from_iterable(
        _inferred_target_roots(crate, target_type, target, package)
        for target_type in EXPLICIT_TARGET_TYPES
        for target in _target_definitions(manifest.get(target_type))
    )
    return frozenset(automatic).intersection(candidates)


def _explicit_target_roots(
    crate: Path,
    manifest: cabc.Mapping[str, object],
    package: cabc.Mapping[str, object],
) -> cabc.Iterator[Path]:
    """Yield source roots from explicit Cargo tables and build configuration."""
    target_roots = itertools.chain.from_iterable(
        (
            (crate / path,)
            if isinstance((path := target.get("path")), str)
            else _inferred_target_roots(crate, target_type, target, package)
        )
        for target_type in EXPLICIT_TARGET_TYPES
        for target in _target_definitions(manifest.get(target_type))
    )
    build_script = package.get("build")
    configured_build = (crate / build_script,) if isinstance(build_script, str) else ()
    return itertools.chain(target_roots, configured_build)


def _inferred_target_roots(
    crate: Path,
    target_type: str,
    target: cabc.Mapping[str, object],
    package: cabc.Mapping[str, object],
) -> tuple[Path, ...]:
    """Return existing conventional paths Cargo infers for one target table."""
    match target_type:
        case "lib":
            candidates = (crate / "src/lib.rs",)
        case "bin" | "example" | "test" | "bench":
            name = target.get("name")
            if not isinstance(name, str):
                return ()
            directory = {
                "bin": "src/bin",
                "example": "examples",
                "test": "tests",
                "bench": "benches",
            }[target_type]
            candidates = (
                crate / directory / f"{name}.rs",
                crate / directory / name / "main.rs",
            )
            if target_type == "bin" and name == package.get("name"):
                candidates = (crate / "src/main.rs", *candidates)
        case _:
            return ()
    return tuple(path for path in candidates if _is_mode(path, stat.S_ISREG))


def _target_definitions(value: object) -> tuple[cabc.Mapping[str, object], ...]:
    """Normalize a Cargo target section into target definitions."""
    match value:
        case dict() as target:
            return (target,)
        case list() as targets:
            return tuple(target for target in targets if isinstance(target, dict))
        case _:
            return ()


def check_safe_policy(workspace: Path) -> tuple[Path, ...]:
    """Require every effective safe target root to forbid unsafe source code."""
    targets = safe_target_roots(workspace)
    sources = {target: _read_text(target) for target in targets}
    return evaluate_safe_target_policy(sources)


def _compile(workspace: Path, targets: CompileTargets | None = None) -> tuple[int, str]:
    """Keep the harness's compiler seam injectable for its probe tests."""
    target_directory = workspace.parent / "target/boundary-contract-debug-all-features"
    return _compile_workspace(workspace, target_directory, targets)


def _new_run_root() -> Path:
    """Create a unique workspace root so concurrent harnesses cannot collide."""
    return _new_boundary_run_root(ROOT / ".cache/boundary-contract/runs")


def _prepare_run_cargo_home(run_root: Path, workspace: Path) -> float:
    """Prepare the copied workspace with this harness's selected repository root."""
    return _prepare_boundary_cargo_home(ROOT, run_root, workspace)


def main() -> None:
    """Check the allowlist and actual compiler rejection on isolated source copies."""
    _check_members_manifest(_load_toml(ROOT / "rust/Cargo.toml"))
    check_member_lint_inheritance(ROOT / "rust")
    safe_targets = check_safe_policy(ROOT / "rust")
    run_root = _new_run_root()
    workspace = run_root / "workspace"
    copy_workspace(ROOT / "rust", workspace, safe_targets)
    with _boundary_cargo_run(run_root, workspace, _prepare_run_cargo_home):
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        code, output = _compile(workspace)
        baseline_log = logs / "safe-positive.log"
        baseline_log.write_text(output, encoding="utf-8")
        if code != 0:
            print(
                "safe baseline did not compile "
                f"(cargo exit {code}); see {baseline_log}",
                file=sys.stderr,
            )
            raise SystemExit(code)
        for target in safe_targets:
            _check_target(
                workspace, workspace / target.relative_to(ROOT / "rust"), logs
            )


def _check_target(workspace: Path, path: Path, logs: Path) -> None:
    """Probe one target through the shared byte-safe compiler harness."""
    _check_probe_target(
        workspace=workspace,
        path=path,
        logs=logs,
        configuration=_ProbeConfiguration(PROBES, _compile),
    )


if __name__ == "__main__":
    main()
