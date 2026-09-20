"""Exercise safe Cargo target discovery without compiling a fixture workspace."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts import check_boundary_contract as contract
from scripts.check_boundary_contract import (
    check_safe_policy,
    discover_safe_target_roots,
    evaluate_safe_target_policy,
    safe_target_roots,
)
from scripts.tests.boundary_harness_support import copy_boundary_repository

AUTO_DISABLED_TARGETS = (
    ("autolib", "src/lib.rs"),
    ("autobins", "src/main.rs"),
    ("autobins", "src/bin/flat.rs"),
    ("autobins", "src/bin/nested/main.rs"),
    ("autoexamples", "examples/example.rs"),
    ("autotests", "tests/integration.rs"),
    ("autobenches", "benches/benchmark.rs"),
)
AUTOMATIC_PROPERTY_TARGETS = (
    "src/lib.rs",
    "src/bin/nested/main.rs",
)
EXPLICIT_PROPERTY_TARGETS = (
    "src/lib.rs",
    "tools/explicit.rs",
    "tools/build.rs",
)
SAFE_SOURCE = "//! Safe target.\n#![forbid(unsafe_code)]\n"


def _write_target(crate: Path, relative_path: str, source: str = SAFE_SOURCE) -> Path:
    """Create one synthetic Cargo target source and return its absolute path."""
    target = crate / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _set_package_field(manifest: Path, field: str) -> None:
    """Add one tested package field beside the existing package metadata."""
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "publish = false", f"publish = false\n{field}", 1
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("switch", "target"), AUTO_DISABLED_TARGETS)
def test_disabled_automatic_targets_are_not_checked(
    switch: str, target: str, tmp_path: Path
) -> None:
    """Cargo's package auto-target switches remove only their matching roots."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    target_path = _write_target(crate, target)
    _set_package_field(crate / "Cargo.toml", f"{switch} = false")

    roots = safe_target_roots(root)

    assert target_path not in roots, f"{switch} = false must disable {target}"
    check_safe_policy(root)


def test_build_false_disables_the_default_build_script(tmp_path: Path) -> None:
    """Cargo's ``build = false`` removes the otherwise automatic build script."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    build_script = _write_target(crate, "build.rs")
    assert build_script in safe_target_roots(root), (
        "the default build script is automatic"
    )

    _set_package_field(crate / "Cargo.toml", "build = false")

    assert build_script not in safe_target_roots(root), (
        "build = false must remove the automatic build-script target"
    )
    check_safe_policy(root)


def test_explicit_target_without_unsafe_prohibition_is_rejected(tmp_path: Path) -> None:
    """An explicit target does not inherit the safe crate's source attribute."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    manifest = crate / "Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[bin]]\nname = "unsafe-contract-probe"\npath = "tools/probe.rs"\n',
        encoding="utf-8",
    )
    _write_target(crate, "tools/probe.rs", "//! Missing safety attribute.\n")

    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        check_safe_policy(root)


def test_malformed_manifest_fails_closed_with_its_path(tmp_path: Path) -> None:
    """Target discovery must report malformed TOML instead of losing coverage."""
    root = copy_boundary_repository(tmp_path) / "rust"
    manifest = root / "cuprum-streams/Cargo.toml"
    manifest.write_text("[package\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"cannot parse TOML manifest .*Cargo.toml"):
        safe_target_roots(root)


def test_non_utf8_manifest_fails_closed_with_its_path(tmp_path: Path) -> None:
    """Manifest decoding failure is an explicit unmeasured-contract error."""
    root = copy_boundary_repository(tmp_path) / "rust"
    manifest = root / "cuprum-streams/Cargo.toml"
    manifest.write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"cannot read .*Cargo.toml"):
        safe_target_roots(root)


def test_unreadable_source_fails_closed_with_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source read error is an unmeasured boundary, never an omitted target."""
    root = copy_boundary_repository(tmp_path) / "rust"
    target = root / "cuprum-streams/src/lib.rs"
    original_read_text = Path.read_text

    def read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        """Refuse only the selected target while retaining real fixture reads."""
        if path == target:
            raise PermissionError
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(ValueError, match=r"cannot read .*src/lib.rs"):
        check_safe_policy(root)


def test_unreadable_automatic_target_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory access failure must not be treated as an empty target set."""
    root = copy_boundary_repository(tmp_path) / "rust"
    directory = root / "cuprum-streams/tests"
    original_iterdir = Path.iterdir

    def iterdir(path: Path) -> object:
        """Refuse only the directory whose targets the contract must inspect."""
        if path == directory:
            raise PermissionError
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    with pytest.raises(
        ValueError, match=r"cannot inspect automatic target directory .*tests"
    ):
        safe_target_roots(root)


def test_probe_rejection_cannot_replace_source_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator rejects a missing attribute before it can append a probe."""
    root = copy_boundary_repository(tmp_path)
    target = root / "rust/cuprum-streams/src/lib.rs"
    target.write_text("//! Missing safety attribute.\n", encoding="utf-8")
    compile_calls: list[Path] = []

    def compile_workspace(workspace: Path) -> tuple[int, str]:
        """Record a forbidden probe attempt without invoking Cargo."""
        compile_calls.append(workspace)
        return 1, "probe rejected"

    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(contract, "_compile", compile_workspace)

    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        contract.main()
    assert not compile_calls, "source policy must fail before the probe seam runs"


@settings(max_examples=24, deadline=None)
@given(
    st.lists(st.sampled_from(AUTOMATIC_PROPERTY_TARGETS), max_size=6),
    st.lists(st.sampled_from(EXPLICIT_PROPERTY_TARGETS), max_size=6),
)
def test_discovery_is_order_independent_and_policy_requires_every_root(
    automatic_paths: list[str], explicit_paths: list[str]
) -> None:
    """Generated automatic, nested, explicit, and build roots stay deduplicated."""
    crate = Path("/synthetic/cuprum-streams")
    automatic = tuple(crate / path for path in automatic_paths)
    explicit = tuple(crate / path for path in explicit_paths)
    expected = tuple(sorted({*automatic, *explicit}))

    discovered = discover_safe_target_roots(automatic, explicit)

    assert discovered == expected, "the pure discovery oracle must ignore input order"
    if not discovered:
        with pytest.raises(ValueError, match="no discoverable Cargo target roots"):
            evaluate_safe_target_policy({})
        return
    safe_sources = dict.fromkeys(discovered, SAFE_SOURCE)
    assert evaluate_safe_target_policy(safe_sources) == discovered, (
        "policy evaluation must retain every discovered root"
    )
    missing_attribute = dict(safe_sources)
    missing_attribute[discovered[0]] = "//! Missing safety attribute.\n"
    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        evaluate_safe_target_policy(missing_attribute)
