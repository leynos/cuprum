"""Hold the Rust dev-build footprint to its committed Cargo and concordat contract."""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Cargo argv, pinned manifest.
import tomllib
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

CONCORDAT = ".concordat"
WORKSPACE_MANIFEST = "rust/Cargo.toml"
TOOLCHAIN_FILE = "rust/rust-toolchain.toml"
MEMBER_MANIFESTS = (
    "rust/cuprum-native-io/Cargo.toml",
    "rust/cuprum-rust/Cargo.toml",
    "rust/cuprum-streams/Cargo.toml",
)
CARGO_CONFIG_FILES = (".cargo/config.toml", "rust/.cargo/config.toml")
UNSTABLE_METADATA_KEY = "embed-metadata"
NUMBERED_RELEASE = re.compile(r"\d+\.\d+\.\d+")
# Cargo spellings that switch debug information off; any other value keeps it.
DEBUG_OFF = (0, False, "none")
MEMBER_CRATE = "cuprum-streams"
# A cold build of one small member from a fresh target directory, including its
# dependencies, with a finite budget for compiler admission queues.
MEMBER_BUILD_TIMEOUT_SECONDS = 1800


def _load_toml(relative: str) -> dict[str, object]:
    """Parse one committed TOML file beneath the repository root."""
    return tomllib.loads((repo_root() / relative).read_text(encoding="utf-8"))


def _table(value: object, subject: str) -> dict[str, object]:
    """Return ``value`` as a table, naming the subject when it is malformed."""
    assert isinstance(value, dict), f"{subject} must be a table"
    return value


def _declares_key(node: object, key: str) -> bool:
    """Return whether ``key`` names a table entry anywhere beneath ``node``."""
    if isinstance(node, dict):
        return key in node or any(_declares_key(child, key) for child in node.values())
    if isinstance(node, list):
        return any(_declares_key(child, key) for child in node)
    return False


def _rust_surface_paths(document: object) -> tuple[str, ...]:
    """Return the path of every Rust surface a parsed concordat document declares."""
    root = _table(document, "the concordat document")
    languages = _table(root.get("language"), "language")
    rust = _table(languages.get("rust"), "language.rust")
    surfaces = rust.get("surfaces")
    assert isinstance(surfaces, list), "language.rust.surfaces must be a list"
    paths: list[str] = []
    for surface in surfaces:
        path = _table(surface, "each Rust surface").get("path")
        assert isinstance(path, str), "each Rust surface must declare a string path"
        paths.append(path)
    return tuple(paths)


def _enables_debug(setting: object) -> bool:
    """Return whether a Cargo ``debug`` value requests any debug information."""
    return setting not in DEBUG_OFF


def _reenables_build_output(profile: dict[str, object]) -> bool:
    """Return whether an override re-enables incremental builds or debug info."""
    incremental = profile.get("incremental", False)
    return incremental is not False or _enables_debug(profile.get("debug", 0))


def test_concordat_declares_only_the_governing_rust_manifest() -> None:
    """The Rust surface is the workspace manifest, and no member is declared."""
    text = (repo_root() / CONCORDAT).read_text(encoding="utf-8")
    paths = _rust_surface_paths(yaml.safe_load(text))
    assert paths == (WORKSPACE_MANIFEST,), (
        "the concordat must declare exactly the governing Rust workspace manifest"
    )
    for member in MEMBER_MANIFESTS:
        assert member not in paths, f"{member} must not be declared as a surface"


def test_dev_profile_keeps_incremental_state_and_debug_info_out() -> None:
    """The workspace dev profile disables incremental builds and debug info."""
    manifest = _load_toml(WORKSPACE_MANIFEST)
    assert "workspace" in manifest, "the root manifest must declare [workspace]"
    profile = _table(manifest.get("profile"), "[profile]")
    dev = _table(profile.get("dev"), "[profile.dev]")
    assert dev.get("incremental") is False, "[profile.dev] must set incremental = false"
    assert dev.get("debug") == 0, "[profile.dev] must set debug = 0"

    packages = _table(dev.get("package", {}), "[profile.dev.package]")
    overrides: list[tuple[str, object]] = [
        (f"[profile.dev.package.{name}]", value) for name, value in packages.items()
    ]
    if "build-override" in dev:
        overrides.append(("[profile.dev.build-override]", dev["build-override"]))
    for label, value in overrides:
        assert not _reenables_build_output(_table(value, label)), (
            f"{label} must not re-enable incremental compilation or debug info"
        )


def test_member_manifests_carry_no_profile_or_incremental_setting() -> None:
    """Cargo reads profiles at the workspace root only, so members must set none."""
    workspace = _table(_load_toml(WORKSPACE_MANIFEST).get("workspace"), "[workspace]")
    members = workspace.get("members")
    assert isinstance(members, list), "[workspace].members must be a list"
    for member in members:
        manifest_path = f"rust/{member}/Cargo.toml"
        manifest = _load_toml(manifest_path)
        assert "profile" not in manifest, (
            f"{manifest_path} must not declare a profile; Cargo ignores members"
        )
        assert not _declares_key(manifest, "incremental"), (
            f"{manifest_path} must not set incremental"
        )


def test_rust_toolchain_pins_a_numbered_stable_release() -> None:
    """A numbered stable pin never needs the unstable metadata key."""
    toolchain = _table(_load_toml(TOOLCHAIN_FILE).get("toolchain"), "[toolchain]")
    channel = toolchain.get("channel")
    assert isinstance(channel, str), "[toolchain].channel must be a string"
    assert NUMBERED_RELEASE.fullmatch(channel), (
        f"the Rust toolchain must pin a numbered stable release, not {channel!r}"
    )


def test_cargo_configs_do_not_request_unstable_metadata() -> None:
    """No existing Cargo configuration may request the unstable metadata key."""
    for relative in CARGO_CONFIG_FILES:
        config = repo_root() / relative
        if config.exists():
            assert UNSTABLE_METADATA_KEY not in config.read_text(encoding="utf-8"), (
                f"{relative} must not request {UNSTABLE_METADATA_KEY}"
            )


def test_surface_reader_returns_declared_paths() -> None:
    """A well-formed concordat document yields its declared Rust surface paths."""
    document = {"language": {"rust": {"surfaces": [{"path": WORKSPACE_MANIFEST}]}}}
    assert _rust_surface_paths(document) == (WORKSPACE_MANIFEST,)


def test_surface_reader_rejects_a_bare_string_surface() -> None:
    """A surface written as a bare string cannot satisfy the mapping contract."""
    document = {"language": {"rust": {"surfaces": [WORKSPACE_MANIFEST]}}}
    with pytest.raises(AssertionError, match="must be a table"):
        _rust_surface_paths(document)


def test_override_reader_accepts_a_quiet_override() -> None:
    """An override that leaves incremental and debug settings off is accepted."""
    assert not _reenables_build_output({"incremental": False, "debug": 0})


def test_override_reader_flags_restored_incremental_state() -> None:
    """An override that restores incremental state is reported."""
    assert _reenables_build_output({"incremental": True})


def test_key_search_ignores_absent_keys_and_finds_nested_ones() -> None:
    """The member guard reports a nested incremental key and nothing else."""
    assert not _declares_key({"package": {"name": "member"}}, "incremental")
    nested = {"profile": {"dev": {"package": {"*": {"incremental": True}}}}}
    assert _declares_key(nested, "incremental")


def _member_rustc_lines(output: str, crate: str) -> list[str]:
    """Return the verbose Cargo lines that compile ``crate`` itself."""
    return [
        line
        for line in output.splitlines()
        if "Running" in line and f"--crate-name {crate.replace('-', '_')} " in line
    ]


def test_member_rustc_lines_pick_only_the_named_crate() -> None:
    """The reader keeps the member's rustc line and drops its dependencies."""
    output = (
        "Running `rustc --crate-name cuprum_streams --edition=2021 -C strip=none`\n"
        "Running `rustc --crate-name syn --edition=2021 -C debuginfo=2`\n"
    )
    lines = _member_rustc_lines(output, MEMBER_CRATE)
    assert len(lines) == 1, "only the named crate's rustc line must be kept"
    assert "debuginfo=2" not in lines[0], "a dependency's flags must not leak in"


def test_a_member_dev_build_passes_no_debuginfo_or_incremental_flag(
    tmp_path: Path,
) -> None:
    """Build one member from a fresh target and read the flags rustc received."""
    cargo = shutil.which("cargo")
    assert cargo is not None, "the Cargo-boundary check needs Cargo on PATH"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"RUSTFLAGS", "CARGO_INCREMENTAL"}
    }
    environment["CARGO_TARGET_DIR"] = str(tmp_path)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv, no shell needed.
        [
            cargo,
            "build",
            "-v",
            "-p",
            MEMBER_CRATE,
            "--manifest-path",
            str(repo_root() / WORKSPACE_MANIFEST),
        ],
        capture_output=True,
        check=True,
        env=environment,
        text=True,
        timeout=MEMBER_BUILD_TIMEOUT_SECONDS,
    )
    lines = _member_rustc_lines(completed.stdout + completed.stderr, MEMBER_CRATE)
    assert lines, f"Cargo must have compiled {MEMBER_CRATE} with a verbose rustc line"
    for line in lines:
        assert " -C incremental=" not in line, "dev builds must not be incremental"
        assert " -C debuginfo=" not in line, "dev builds must pass no debuginfo level"
