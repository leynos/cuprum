#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Check audited workspace lint inheritance and safe-crate unsafe boundaries.

The workspace copy avoids touching a developer's sources while proving that
the real library and integration-test targets reject unsafe code. Cargo keeps
its shared package cache. Only build output uses a separate target directory.
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = frozenset({"cuprum-rust", "cuprum-native-io"})
SAFE_CRATES = frozenset({"cuprum-streams"})
PROBES = (
    "pub fn unsafe_probe() { unsafe {} }",
    "pub unsafe fn unsafe_probe() {}",
    "unsafe trait Probe {} unsafe impl Probe for () {}",
)
AUTOMATIC_TARGET_ROOTS = (
    ("autolib", "src/lib.rs"),
    ("autobins", "src/main.rs"),
)
AUTOMATIC_TARGET_DIRECTORIES = (
    ("autobins", "src/bin"),
    ("autoexamples", "examples"),
    ("autotests", "tests"),
    ("autobenches", "benches"),
)
EXPLICIT_TARGET_TYPES = ("lib", "bin", "example", "test", "bench")


def check_members(manifest: str) -> None:
    """Require an explicit review before the approved crate set can grow.

    Parameters
    ----------
    manifest : str
        Workspace manifest text.

    """
    _check_members_manifest(tomllib.loads(manifest))


def _check_members_manifest(manifest: cabc.Mapping[str, object]) -> None:
    """Evaluate the loaded workspace membership without filesystem access."""
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
    """Return every automatic and explicitly configured safe-crate target root.

    Cargo auto-discovers crate roots only at these locations. Explicit target
    tables may select another path, which remains part of the safe boundary.
    Keeping this scan local and deterministic lets the contract reject a new
    target before a compiler invocation can hide it behind dependency output.

    Returns
    -------
    tuple[Path, ...]
        Existing automatic and explicitly configured crate-root source paths.
    """
    crate, manifest = _load_safe_manifest(workspace)
    package = _package_table(manifest)
    automatic = _load_automatic_target_roots(crate, package)
    explicit = _explicit_target_roots(crate, manifest, package)
    return discover_safe_target_roots(automatic, explicit)


def discover_safe_target_roots(
    automatic: cabc.Iterable[Path], explicit: cabc.Iterable[Path]
) -> tuple[Path, ...]:
    """Deduplicate loaded automatic and declared explicit Cargo target roots."""
    return tuple(sorted(set(itertools.chain(automatic, explicit))))


def evaluate_safe_target_policy(sources: cabc.Mapping[Path, str]) -> tuple[Path, ...]:
    """Require every loaded safe target source to forbid unsafe code."""
    if not sources:
        msg = "safe crate has no discoverable Cargo target roots"
        raise ValueError(msg)
    for target, source in sources.items():
        if "#![forbid(unsafe_code)]" not in source:
            msg = f"safe target must forbid unsafe code: {target}"
            raise ValueError(msg)
    return tuple(sources)


def _load_safe_manifest(workspace: Path) -> tuple[Path, cabc.Mapping[str, object]]:
    """Load the safe crate manifest before evaluating its target policy."""
    crate = workspace / "cuprum-streams"
    return crate, _load_toml(crate / "Cargo.toml")


def _load_toml(path: Path) -> cabc.Mapping[str, object]:
    """Read and parse one manifest while retaining the affected path on failure."""
    text = _read_text(path)
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"cannot parse TOML manifest {path}: {error}"
        raise ValueError(msg) from error
    return manifest


def _read_text(path: Path) -> str:
    """Read UTF-8 text, failing closed when the source cannot be inspected."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        msg = f"cannot read {path}: {error}"
        raise ValueError(msg) from error


def _load_automatic_target_roots(
    crate: Path, package: cabc.Mapping[str, object]
) -> tuple[Path, ...]:
    """Load the existing Cargo auto-target roots selected by package switches."""
    direct = (
        crate / path
        for switch, path in AUTOMATIC_TARGET_ROOTS
        if _automatic_target_is_enabled(package, switch)
    )
    directories = (
        crate / directory
        for switch, directory in AUTOMATIC_TARGET_DIRECTORIES
        if _automatic_target_is_enabled(package, switch)
    )
    candidates = itertools.chain(
        direct,
        itertools.chain.from_iterable(
            _load_directory_target_roots(path) for path in directories
        ),
        _load_default_build_script_root(crate, package),
    )
    return tuple(path for path in candidates if _is_regular_file(path))


def _automatic_target_is_enabled(
    package: cabc.Mapping[str, object], switch: str
) -> bool:
    """Return whether Cargo's default-enabled automatic target switch is active."""
    return package.get(switch) is not False


def _load_directory_target_roots(directory: Path) -> cabc.Iterator[Path]:
    """Load Cargo's flat and one-level-nested conventional target roots."""
    for entry in _directory_entries(directory):
        if entry.suffix == ".rs":
            yield entry
        if _is_directory(entry):
            yield entry / "main.rs"


def _directory_entries(directory: Path) -> tuple[Path, ...]:
    """List one optional automatic-target directory without masking I/O errors."""
    try:
        return tuple(directory.iterdir())
    except FileNotFoundError:
        return ()
    except NotADirectoryError:
        return ()
    except OSError as error:
        msg = f"cannot inspect automatic target directory {directory}: {error}"
        raise ValueError(msg) from error


def _is_regular_file(path: Path) -> bool:
    """Check a candidate root while treating inspection failures as contract errors."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        msg = f"cannot inspect target root {path}: {error}"
        raise ValueError(msg) from error


def _is_directory(path: Path) -> bool:
    """Check a nested target directory without masking an access failure."""
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        msg = f"cannot inspect target directory {path}: {error}"
        raise ValueError(msg) from error


def _explicit_target_roots(
    crate: Path,
    manifest: cabc.Mapping[str, object],
    package: cabc.Mapping[str, object],
) -> cabc.Iterator[Path]:
    """Yield crate roots declared through Cargo target or build-script fields."""
    target_definitions = itertools.chain.from_iterable(
        _target_definitions(manifest.get(target_type))
        for target_type in EXPLICIT_TARGET_TYPES
    )
    target_paths = (
        crate / path
        for target in target_definitions
        if isinstance((path := target.get("path")), str)
    )
    return itertools.chain(
        target_paths, _configured_build_script_target_root(crate, package)
    )


def _target_definitions(value: object) -> tuple[cabc.Mapping[str, object], ...]:
    """Normalize a Cargo target table into a sequence of target definitions."""
    match value:
        case dict() as target:
            return (target,)
        case list() as targets:
            return tuple(target for target in targets if isinstance(target, dict))
        case _:
            return ()


def _package_table(manifest: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return the package table or an empty table for malformed optional input."""
    package = manifest.get("package")
    return package if isinstance(package, dict) else {}


def _load_default_build_script_root(
    crate: Path, package: cabc.Mapping[str, object]
) -> tuple[Path, ...]:
    """Return Cargo's default build script unless the manifest disables it."""
    return (crate / "build.rs",) if package.get("build", True) is True else ()


def _configured_build_script_target_root(
    crate: Path, package: cabc.Mapping[str, object]
) -> tuple[Path, ...]:
    """Return a non-default package build-script target, if one is declared."""
    build_script = package.get("build")
    return (crate / build_script,) if isinstance(build_script, str) else ()


def check_safe_policy(workspace: Path) -> tuple[Path, ...]:
    """Require every safe-crate target root to forbid unsafe source code.

    The shared baseline deliberately omits ``unsafe_code = forbid`` because
    the other audited members implement syscall and FFI boundaries. Every
    automatic or explicit safe-crate target must therefore state the
    non-negotiable source-level prohibition itself.

    Returns
    -------
    tuple[Path, ...]
        The checked source roots, for the compiler-probe phase.

    """
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
    shutil.copytree(ROOT / "rust", workspace, ignore=shutil.ignore_patterns("target"))
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
    """Require compilation failure with an explicit unsafe-forbid diagnostic."""
    markers = ("forbid(unsafe_code)", "-F unsafe-code")
    return code != 0 and any(marker in output for marker in markers)


@contextlib.contextmanager
def _probe_appended(path: Path, probe: str) -> cabc.Iterator[None]:
    """Temporarily append a probe to a source file, then restore it."""
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n" + probe + "\n", encoding="utf-8")
    try:
        yield
    finally:
        path.write_text(original, encoding="utf-8")


def _check_target(workspace: Path, path: Path, logs: Path) -> None:
    """Probe one actual target and restore it even if compiler checking fails."""
    for index, probe in enumerate(PROBES):
        with _probe_appended(path, probe):
            code, output = _compile(workspace)
            name = f"{path.stem}-unsafe-{index}.log"
            (logs / name).write_text(output, encoding="utf-8")
            if not _unsafe_was_forbidden(code, output):
                msg = f"safe target did not reject unsafe probe: {name}"
                raise RuntimeError(msg)


if __name__ == "__main__":
    main()
