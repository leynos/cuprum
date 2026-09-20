#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Check audited workspace lint inheritance and safe-crate unsafe boundaries.

The isolated copy preserves Cargo target topology and leaves source untouched.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import shutil
import stat
import sys
import tomllib
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh
from scripts.boundary_workspace import copy_workspace

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = frozenset({"cuprum-rust", "cuprum-native-io"})
SAFE_CRATES = frozenset({"cuprum-streams"})
PROBES = (
    "pub fn unsafe_probe() { unsafe {} }",
    "pub unsafe fn unsafe_probe() {}",
    "unsafe trait Probe {} unsafe impl Probe for () {}",
)
AUTOMATIC_TARGET_ROOTS = (("autolib", "src/lib.rs"), ("autobins", "src/main.rs"))
AUTOMATIC_TARGET_DIRECTORIES = (
    ("autobins", "src/bin"),
    ("autoexamples", "examples"),
    ("autotests", "tests"),
    ("autobenches", "benches"),
)
EXPLICIT_TARGET_TYPES = ("lib", "bin", "example", "test", "bench")


def check_members(manifest: str) -> None:
    """Require review before the approved crate set can grow."""
    _check_members_manifest(tomllib.loads(manifest))


def _check_members_manifest(manifest: cabc.Mapping[str, object]) -> None:
    """Validate audited workspace membership from parsed manifest data."""
    workspace = manifest.get("workspace")
    members = workspace.get("members") if isinstance(workspace, dict) else None
    if not isinstance(members, list) or not all(
        isinstance(member, str) for member in members
    ):
        msg = "workspace membership differs from the audited boundary inventory"
        raise ValueError(msg)
    if set(members) != BOUNDARIES | SAFE_CRATES or len(members) != len(
        BOUNDARIES | SAFE_CRATES
    ):
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
    for member in BOUNDARIES | SAFE_CRATES:
        manifest_path = workspace / member / "Cargo.toml"
        manifest = _load_toml(manifest_path)
        if manifest.get("lints") != {"workspace": True}:
            msg = f"{member} must inherit the workspace lint baseline"
            raise ValueError(msg)


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


def _compile(workspace: Path) -> tuple[int, str]:
    """Compile the copied safe library and all its targets with all features."""
    cargo_program = Program("cargo")
    project = ProjectSettings(
        name="boundary-contract",
        programs=(cargo_program,),
        documentation_locations=("docs/rust-boundary-verification.md",),
        noise_rules=(),
    )
    cargo = sh.make(cargo_program, catalogue=ProgramCatalogue(projects=(project,)))
    context = sh.ExecutionContext(
        cwd=workspace,
        env={"CARGO_TARGET_DIR": str(ROOT / "rust/target/boundary-contract")},
        timeout=600,
    )
    with scoped(ScopeConfig(allowlist=frozenset({cargo_program}))):
        result = cargo(
            "check", "--package", "cuprum-streams", "--all-targets", "--all-features"
        ).run_sync(context=context)
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def main() -> None:
    """Check the allowlist and actual compiler rejection on isolated source copies."""
    _check_members_manifest(_load_toml(ROOT / "rust/Cargo.toml"))
    check_member_lint_inheritance(ROOT / "rust")
    safe_targets = check_safe_policy(ROOT / "rust")
    workspace = ROOT / ".cache/boundary-contract/workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    copy_workspace(ROOT / "rust", workspace, safe_targets)
    logs = ROOT / "rust/target/boundary-verification"
    logs.mkdir(parents=True, exist_ok=True)
    code, output = _compile(workspace)
    baseline_log = logs / "safe-positive.log"
    baseline_log.write_text(output, encoding="utf-8")
    if code != 0:
        print(
            f"safe baseline did not compile (cargo exit {code}); see {baseline_log}",
            file=sys.stderr,
        )
        raise SystemExit(code)
    for target in safe_targets:
        _check_target(workspace, workspace / target.relative_to(ROOT / "rust"), logs)


def _unsafe_was_forbidden(code: int, output: str) -> bool:
    """Return whether a compiler result names the required unsafe prohibition."""
    markers = ("forbid(unsafe_code)", "-F unsafe-code")
    return code != 0 and any(marker in output for marker in markers)


@contextlib.contextmanager
def _probe_appended(path: Path, probe: str) -> cabc.Iterator[None]:
    """Temporarily append a byte-preserving probe, then restore the source."""
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n" + probe.encode() + b"\n")
        yield
    finally:
        path.write_bytes(original)


def _probe_log_name(workspace: Path, path: Path, index: int) -> str:
    """Return a filesystem-safe, reversible workspace-relative probe log name."""
    relative = path.relative_to(workspace).as_posix()
    encoded = base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
    return f"{path.stem}-{encoded}-unsafe-{index}.log"


def _check_target(workspace: Path, path: Path, logs: Path) -> None:
    """Probe one target and archive each compiler result."""
    for index, probe in enumerate(PROBES):
        with _probe_appended(path, probe):
            code, output = _compile(workspace)
            name = _probe_log_name(workspace, path, index)
            (logs / name).write_text(output, encoding="utf-8")
            if not _unsafe_was_forbidden(code, output):
                msg = f"safe target did not reject unsafe probe: {name}"
                raise RuntimeError(msg)


if __name__ == "__main__":
    main()
