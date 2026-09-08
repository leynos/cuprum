#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Compile deliberate unsafe probes against a copy of the actual safe crate.

The workspace copy avoids touching a developer's sources while proving that
the real library and integration-test targets reject unsafe code. Cargo keeps
its shared package cache. Only build output uses a separate target directory.
"""

import shutil
import tomllib
from pathlib import Path

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = frozenset({"cuprum-rust", "cuprum-native-io"})
SAFE_CRATES = frozenset({"cuprum-streams"})
PROBES = (
    "pub fn unsafe_probe() { unsafe {} }",
    "pub unsafe fn unsafe_probe() {}",
    "unsafe trait Probe {} unsafe impl Probe for () {}",
)


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


def check_safe_policy(workspace_manifest: str, safe_manifest: str) -> None:
    """Require workspace lint parity plus an unrelaxable safe-target prohibition.

    Parameters
    ----------
    workspace_manifest : str
        Workspace manifest containing the shared lint policy.
    safe_manifest : str
        Safe crate manifest, whose lint policy applies to every Cargo target.

    Raises
    ------
    ValueError
        If the safe crate weakens or diverges from the workspace policy.
    """
    expected = tomllib.loads(workspace_manifest)["workspace"]["lints"]
    expected["rust"]["unsafe_code"] = "forbid"
    actual = tomllib.loads(safe_manifest)["lints"]
    if actual != expected:
        msg = "safe crate must retain workspace lints and forbid unsafe in all targets"
        raise ValueError(msg)


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
    check_safe_policy(
        manifest,
        (ROOT / "rust/cuprum-streams/Cargo.toml").read_text(encoding="utf-8"),
    )
    workspace = ROOT / ".cache/boundary-contract/workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(ROOT / "rust", workspace, ignore=shutil.ignore_patterns("target"))
    future_target = workspace / "cuprum-streams/tests/future_target.rs"
    future_target.write_text(
        "//! Probe automatic Cargo target lint inheritance.\n", encoding="utf-8"
    )
    logs = ROOT / "rust/target/boundary-verification"
    logs.mkdir(parents=True, exist_ok=True)
    code, output = _compile(workspace)
    (logs / "safe-positive.log").write_text(output, encoding="utf-8")
    if code != 0:
        msg = "safe baseline did not compile; see safe-positive.log"
        raise RuntimeError(msg)
    targets = ("src/lib.rs", "tests/compile_tests.rs", "tests/future_target.rs")
    for target in targets:
        _check_target(workspace, workspace / "cuprum-streams" / target, logs)


def _check_target(workspace: Path, path: Path, logs: Path) -> None:
    """Probe one actual target and restore it even if compiler checking fails."""
    original = path.read_text(encoding="utf-8")
    for index, probe in enumerate(PROBES):
        try:
            path.write_text(original + "\n" + probe + "\n", encoding="utf-8")
            code, output = _compile(workspace)
            name = f"{path.stem}-unsafe-{index}.log"
            (logs / name).write_text(output, encoding="utf-8")
            if code == 0 or not (
                "forbid(unsafe_code)" in output or "-F unsafe-code" in output
            ):
                msg = f"safe target did not reject unsafe probe: {name}"
                raise RuntimeError(msg)
        finally:
            path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    main()
