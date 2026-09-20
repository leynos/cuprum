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
from scripts.tests.boundary_harness_support import (
    cargo_target_roots,
    copy_boundary_repository,
)

HIDDEN_AUTOMATIC_TARGETS = (
    ("src/bin/.scratch.rs", "src/bin/visible.rs"),
    ("src/bin/.scratch/main.rs", "src/bin/visible/main.rs"),
)
AUTO_DISABLED_TARGETS = (
    ("autolib", "src/lib.rs"),
    ("autobins", "src/main.rs"),
    ("autobins", "src/bin/flat.rs"),
    ("autobins", "src/bin/nested/main.rs"),
    ("autoexamples", "examples/example.rs"),
    ("autotests", "tests/integration.rs"),
    ("autobenches", "benches/benchmark.rs"),
)
AUTOMATIC_PROPERTY_TARGETS = ("src/lib.rs", "src/bin/nested/main.rs")
EXPLICIT_PROPERTY_TARGETS = ("src/lib.rs", "tools/explicit.rs", "tools/build.rs")
OVERRIDING_EXPLICIT_TARGETS = (
    ("lib", None, "src/lib.rs", "tools/custom_lib.rs"),
    ("bin", "cuprum-streams", "src/main.rs", "tools/custom_main.rs"),
    ("bin", "client", "src/bin/client.rs", "tools/custom_client.rs"),
    ("example", "client", "examples/client.rs", "tools/custom_example.rs"),
    ("test", "client", "tests/client.rs", "tools/custom_test.rs"),
    ("bench", "client", "benches/client.rs", "tools/custom_bench.rs"),
)
PATHLESS_EXPLICIT_TARGETS = (
    ("lib", None, "src/lib.rs"),
    ("bin", "cuprum-streams", "src/main.rs"),
    ("bin", "flat", "src/bin/flat.rs"),
    ("bin", "nested", "src/bin/nested/main.rs"),
    ("example", "flat", "examples/flat.rs"),
    ("example", "nested", "examples/nested/main.rs"),
    ("test", "flat", "tests/flat.rs"),
    ("test", "nested", "tests/nested/main.rs"),
    ("bench", "flat", "benches/flat.rs"),
    ("bench", "nested", "benches/nested/main.rs"),
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


def _append_explicit_target(
    manifest: Path, target_type: str, name: str | None, source: str
) -> None:
    """Declare one named Cargo target table with a custom source path."""
    table = "[lib]" if target_type == "lib" else f"[[{target_type}]]"
    name_entry = "" if name is None else chr(10) + f'name = "{name}"'
    content = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        f'{content}{chr(10)}{table}{name_entry}{chr(10)}path = "{source}"{chr(10)}',
        encoding="utf-8",
    )


def _append_pathless_target(manifest: Path, target_type: str, name: str | None) -> None:
    """Declare a target table whose source path Cargo must infer."""
    table = "[lib]" if target_type == "lib" else f"[[{target_type}]]"
    name_entry = "" if name is None else f'\nname = "{name}"'
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"\n{table}{name_entry}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(("hidden", "visible"), HIDDEN_AUTOMATIC_TARGETS)
def test_hidden_automatic_targets_are_ignored(
    hidden: str, visible: str, tmp_path: Path
) -> None:
    """Cargo ignores hidden automatic targets but retains their visible siblings."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    hidden_path = _write_target(crate, hidden, "//! Hidden automatic root." + chr(10))
    visible_path = _write_target(crate, visible)
    roots = safe_target_roots(root)

    assert hidden_path not in roots, "Cargo must ignore a hidden automatic target"
    assert visible_path in roots, "Cargo must retain a visible automatic target"
    assert roots == cargo_target_roots(crate), (
        "the scanner and Cargo metadata must agree about hidden target entries"
    )
    check_safe_policy(root)


@pytest.mark.parametrize(
    "nested_target",
    [
        "src/bin/behaviour.rs/main.rs",
        "examples/behaviour.rs/main.rs",
        "tests/behaviour.rs/main.rs",
        "benches/behaviour.rs/main.rs",
    ],
)
def test_visible_support_directory_without_main_is_not_a_target(
    nested_target: str, tmp_path: Path
) -> None:
    """Only a visible nested main.rs is an automatic Cargo target."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    (crate / "tests/support").mkdir(parents=True)
    _write_target(crate, "shared/main.rs")
    try:
        (crate / "tests/shared").symlink_to("../shared", target_is_directory=True)
        (crate / "tests/shared.rs").symlink_to("../shared/main.rs")
    except OSError as error:
        pytest.skip(f"the platform cannot create the symlink fixture: {error}")
    roots = safe_target_roots(root)
    assert crate / "tests/support/main.rs" not in roots, (
        "support/main.rs is not a target"
    )
    assert crate / "tests/shared/main.rs" not in roots, (
        "directory symlink is not a target"
    )
    assert crate / "tests/shared.rs" in roots, (
        "a direct source symlink must retain Cargo's target behaviour"
    )
    assert roots == cargo_target_roots(crate), "scanner must match Cargo metadata"
    check_safe_policy(root)
    nested = _write_target(crate, nested_target, "//! Missing." + chr(10))
    assert nested in safe_target_roots(root), "nested main.rs remains a target"
    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        check_safe_policy(root)


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


@pytest.mark.parametrize("case", OVERRIDING_EXPLICIT_TARGETS)
def test_explicit_target_replaces_its_dormant_automatic_root(
    case: tuple[str, str | None, str, str], tmp_path: Path
) -> None:
    """Cargo metadata and the safe policy must agree on explicit overrides."""
    target_type, name, dormant, custom = case
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    manifest = crate / "Cargo.toml"
    dormant_path = _write_target(
        crate, dormant, "//! Dormant automatic root." + chr(10)
    )
    custom_path = _write_target(crate, custom)
    _append_explicit_target(manifest, target_type, name, custom)

    roots = safe_target_roots(root)

    assert custom_path in roots, "the explicit target path must remain audited"
    assert dormant_path not in roots, (
        "the explicit target must replace its dormant conventional root"
    )
    assert roots == cargo_target_roots(crate), (
        "the scanner and Cargo metadata must resolve identical target roots"
    )
    check_safe_policy(root)


@pytest.mark.parametrize(("target_type", "name", "target"), PATHLESS_EXPLICIT_TARGETS)
def test_pathless_explicit_target_is_discovered_and_checked(
    target_type: str, name: str | None, target: str, tmp_path: Path
) -> None:
    """Cargo-inferred source paths must remain inside the safe-target contract."""
    root = copy_boundary_repository(tmp_path) / "rust"
    crate = root / "cuprum-streams"
    manifest = crate / "Cargo.toml"
    _set_package_field(
        manifest,
        "\n".join((
            "autolib = false",
            "autobins = false",
            "autoexamples = false",
            "autotests = false",
            "autobenches = false",
        )),
    )
    _append_pathless_target(manifest, target_type, name)
    target_path = _write_target(crate, target, "//! Missing safety attribute.\n")

    assert target_path in safe_target_roots(root), (
        "the pathless Cargo target table must discover its inferred source"
    )
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
