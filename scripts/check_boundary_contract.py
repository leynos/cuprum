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
AUTOMATIC_TARGET_ROOTS = ("src/lib.rs", "src/main.rs", "build.rs")
AUTOMATIC_TARGET_DIRECTORIES = ("src/bin", "examples", "tests", "benches")
EXPLICIT_TARGET_TYPES = ("lib", "bin", "example", "test", "bench")


def check_members(manifest: str) -> None:
    """Require an explicit review before the approved crate set can grow.

    Parameters
    ----------
    manifest : str
        Workspace manifest text.

    Raises
    ------
    ValueError
        If workspace membership differs from the audited set.
    """
    members = tomllib.loads(manifest)["workspace"]["members"]
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
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
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
    crate = workspace / "cuprum-streams"
    manifest = tomllib.loads((crate / "Cargo.toml").read_text(encoding="utf-8"))
    candidates = itertools.chain(
        _automatic_target_roots(crate), _explicit_target_roots(crate, manifest)
    )
    return tuple(sorted({path for path in candidates if path.is_file()}))


def _automatic_target_roots(crate: Path) -> cabc.Iterator[Path]:
    """Yield roots Cargo discovers from the safe crate's conventional layout."""
    directory_roots = (crate / directory for directory in AUTOMATIC_TARGET_DIRECTORIES)
    return itertools.chain(
        (crate / path for path in AUTOMATIC_TARGET_ROOTS),
        *(root.glob("*.rs") for root in directory_roots),
        *(
            (crate / directory).glob("*/main.rs")
            for directory in AUTOMATIC_TARGET_DIRECTORIES
        ),
    )


def _explicit_target_roots(
    crate: Path, manifest: cabc.Mapping[str, object]
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
    return itertools.chain(target_paths, _build_script_target_root(crate, manifest))


def _target_definitions(value: object) -> tuple[cabc.Mapping[str, object], ...]:
    """Normalize a Cargo target table into a sequence of target definitions."""
    match value:
        case dict() as target:
            return (target,)
        case list() as targets:
            return tuple(target for target in targets if isinstance(target, dict))
        case _:
            return ()


def _build_script_target_root(
    crate: Path, manifest: cabc.Mapping[str, object]
) -> tuple[Path, ...]:
    """Return the package's explicitly configured build-script root, if any."""
    package = manifest.get("package")
    if not isinstance(package, dict):
        return ()
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

    Raises
    ------
    ValueError
        If no source roots exist or any root omits the unsafe prohibition.
    """
    targets = safe_target_roots(workspace)
    if not targets:
        msg = "safe crate has no discoverable Cargo target roots"
        raise ValueError(msg)
    for target in targets:
        source = target.read_text(encoding="utf-8")
        if "#![forbid(unsafe_code)]" not in source:
            msg = (
                f"safe target must forbid unsafe code: {target.relative_to(workspace)}"
            )
            raise ValueError(msg)
    return targets


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
    manifest = (ROOT / "rust/Cargo.toml").read_text(encoding="utf-8")
    check_members(manifest)
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
