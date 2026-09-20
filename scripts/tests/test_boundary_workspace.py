"""Exercise isolated boundary workspace copying and probe topology."""

from __future__ import annotations

import dataclasses
import stat
import typing as typ

import pytest

from scripts import check_boundary_contract as contract
from scripts.boundary_compile import CompileTargets, compile_arguments
from scripts.boundary_workspace import copy_workspace
from scripts.check_boundary_contract import safe_target_roots

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

from scripts.tests.boundary_harness_support import (
    cargo_target_roots,
    copy_boundary_repository,
)

SAFE_SOURCE = "//! Safe target.\n#![forbid(unsafe_code)]\n"


@dataclasses.dataclass(frozen=True, slots=True)
class _ExternalSourceLink:
    """Record one external source link and the original file state."""

    link: Path
    source: Path
    contents: bytes
    mode: int


def test_main_keeps_nested_target_sources_and_omits_root_build_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested target directory is source, while root target is Cargo output."""
    root = copy_boundary_repository(tmp_path)
    rust = root / "rust"
    nested = rust / "cuprum-streams/src/bin/target/main.rs"
    nested.parent.mkdir(parents=True)
    nested.write_text(SAFE_SOURCE, encoding="utf-8")
    nested.chmod(stat.S_IRUSR)
    build_output = rust / "target/ignored.txt"
    build_output.parent.mkdir()
    build_output.write_text("build output", encoding="utf-8")
    source_targets = safe_target_roots(rust)
    workspace = root / ".cache/boundary-contract/workspace"

    copy_workspace(rust, workspace, source_targets)

    copied_nested = workspace / nested.relative_to(rust)
    assert copied_nested in cargo_target_roots(workspace / "cuprum-streams"), (
        "Cargo metadata must retain the nested source directory named target"
    )
    assert copied_nested.is_file(), "the nested source target must be copied"
    assert copied_nested.stat().st_mode & stat.S_IWUSR, (
        "the copied target must be writable for temporary probes"
    )
    assert not nested.stat().st_mode & stat.S_IWUSR, (
        "the source target mode must remain unchanged"
    )
    assert not (workspace / "target").exists(), (
        "only the workspace-root Cargo build output must be omitted"
    )
    scheduled: list[Path] = []
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(contract, "_compile", lambda _workspace: (0, "checked"))
    monkeypatch.setattr(
        contract,
        "_check_target",
        lambda _workspace, target, _logs: scheduled.append(target),
    )

    contract.main()

    assert copied_nested in scheduled, (
        "the copied nested target must be scheduled for unsafe probes"
    )


def _external_source_links(
    root: Path, crate: Path, tmp_path: Path
) -> tuple[_ExternalSourceLink, ...]:
    """Create relative and absolute external source-file symlinks."""
    paths = (
        (
            crate / "tests/relative.rs",
            root / "relative-source.rs",
            "../../../relative-source.rs",
        ),
        (crate / "tests/absolute.rs", tmp_path / "absolute-source.rs", None),
    )
    for _link, source, _target in paths:
        source.write_text(SAFE_SOURCE, encoding="utf-8")
        source.chmod(stat.S_IRUSR)
    try:
        for link, source, target in paths:
            link.symlink_to(source if target is None else target)
    except OSError as error:
        pytest.skip(f"the platform cannot create the symlink fixture: {error}")
    return tuple(
        _ExternalSourceLink(
            link,
            source,
            source.read_bytes(),
            stat.S_IMODE(source.stat().st_mode),
        )
        for link, source, target in paths
    )


def _recording_probe_compiler(
    observed_sources: list[str],
) -> cabc.Callable[[Path], tuple[int, str]]:
    """Return a compiler seam that rejects probes in the copied source tree."""

    def compile_workspace(workspace: Path) -> tuple[int, str]:
        """Reject only the probes applied to the isolated copied sources."""
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((workspace / "cuprum-streams").rglob("*.rs"))
        )
        observed_sources.append(source)
        if any(probe in source for probe in contract.PROBES):
            return 1, "error: forbid(unsafe_code)"
        return 0, "checked"

    return compile_workspace


def _assert_external_links_are_isolated(
    crate: Path,
    copied_crate: Path,
    links: tuple[_ExternalSourceLink, ...],
    original_targets: tuple[Path, ...],
) -> None:
    """Require metadata parity, materialization, and untouched external bytes."""
    copied_links = tuple(
        copied_crate / source_link.link.relative_to(crate) for source_link in links
    )
    assert cargo_target_roots(copied_crate) == tuple(
        copied_crate / path.relative_to(crate) for path in original_targets
    ), "Cargo metadata must retain the copied source-file targets"
    assert all(path.is_file() and not path.is_symlink() for path in copied_links), (
        "external source-file links must be materialized inside the copied workspace"
    )
    assert all(
        (
            source_link.source.read_bytes(),
            stat.S_IMODE(source_link.source.stat().st_mode),
        )
        == (source_link.contents, source_link.mode)
        for source_link in links
    ), "external source bytes and modes must survive the copied probes"


def _remove_copied_dev_dependencies(manifest: Path) -> None:
    """Omit test-only dependencies unused by the empty external target fixtures."""
    contents = manifest.read_text(encoding="utf-8")
    before, header, remainder = contents.partition("[dev-dependencies]\n")
    dependencies, separator, after = remainder.partition("\n[lints]\n")
    assert header, "the copied manifest must retain its dev-dependency section"
    assert separator, "the copied manifest must retain its lint section"
    assert dependencies, "the copied manifest must record test-only dependencies"
    manifest.write_text(before + "[lints]\n" + after, encoding="utf-8")


# The real isolated Cargo check can wait for the shared package-cache lock.
# This is the only test in the file that reaches the real `_compile`: `main()`
# and `_check_target` both resolve through the monkeypatched seam, so exactly
# one `cargo check` runs here. Even narrowed to the two materialized test
# targets, it measured 181s cold and 98s warm on this host once the Makefile
# forced serial cargo builds (`CARGO_BUILD_JOBS=1`). That is well above the
# suite-wide `timeout = 30` in pyproject.toml, so the bound is raised here
# rather than left to the default. `check_boundary_contract._compile` owns the
# real budget at 600s; this sits above it and only catches a wedged process.
@pytest.mark.timeout(900)
def test_main_materializes_external_source_file_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External source links become copied files before the unsafe probes run."""
    root = copy_boundary_repository(tmp_path)
    crate = root / "rust/cuprum-streams"
    links = _external_source_links(root, crate, tmp_path)
    before = cargo_target_roots(crate)
    assert links[0].link in before, "Cargo must recognize the relative source-file link"
    assert links[1].link in before, "Cargo must recognize the absolute source-file link"
    observed_sources: list[str] = []
    boundary_compile = contract._compile
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(
        contract, "_compile", _recording_probe_compiler(observed_sources)
    )

    contract.main()

    copied_crate = root / ".cache/boundary-contract/workspace/cuprum-streams"
    _remove_copied_dev_dependencies(copied_crate / "Cargo.toml")
    code, output = boundary_compile(
        copied_crate.parent,
        CompileTargets.integration_tests("relative", "absolute"),
    )
    assert code == 0, output
    _assert_external_links_are_isolated(crate, copied_crate, links, before)
    assert all(
        any(probe in source for source in observed_sources) for probe in contract.PROBES
    ), "each unsafe probe must reach the isolated copied sources"


def test_compile_arguments_preserve_full_production_coverage() -> None:
    """The production compiler command continues to compile every target."""
    arguments = compile_arguments()

    assert arguments == (
        "check",
        "--package",
        "cuprum-streams",
        "--all-targets",
        "--all-features",
    ), "the production boundary check must continue to cover every target"


def test_compile_arguments_scope_real_link_checks_to_named_tests() -> None:
    """The link integration check compiles only its materialized test targets."""
    targets = CompileTargets.integration_tests("relative", "absolute")
    arguments = compile_arguments(targets)

    assert arguments[:4] == (
        "check",
        "--package",
        "cuprum-streams",
        "--all-features",
    ), "the scoped check must retain its package and feature contract"
    assert arguments[4:] == (
        "--test",
        "relative",
        "--test",
        "absolute",
    ), "the scoped check must compile both materialized test targets"
    assert "--all-targets" not in arguments, (
        "the scoped check must not rebuild unrelated package targets"
    )


@pytest.mark.parametrize("name", ["", "--all-targets", "relative test", "../relative"])
def test_compile_targets_reject_invalid_cargo_names(name: str) -> None:
    """Test-target selections cannot inject Cargo options or paths."""
    with pytest.raises(ValueError, match="Cargo target names"):
        CompileTargets.integration_tests(name)


def test_compile_targets_accept_a_direct_valid_tuple() -> None:
    """Direct construction retains the shared target-name invariant."""
    targets = CompileTargets(("relative", "absolute"))

    assert targets.names == ("relative", "absolute"), (
        "direct construction must retain valid target names"
    )


@pytest.mark.parametrize(
    ("names", "error_type", "expected_error"),
    [
        (typ.cast("tuple[str, ...]", ["relative"]), TypeError, "must be a tuple"),
        ((), ValueError, "must not be empty"),
        (
            typ.cast("tuple[str, ...]", ("relative", 1)),
            ValueError,
            "must contain strings",
        ),
        (("relative test",), ValueError, "valid Cargo target names"),
    ],
)
def test_compile_targets_reject_invalid_direct_construction(
    names: tuple[str, ...], error_type: type[Exception], expected_error: str
) -> None:
    """Direct construction rejects malformed Cargo target collections."""
    with pytest.raises(error_type, match=expected_error):
        CompileTargets(names)


@pytest.mark.parametrize(
    ("target_path", "expected_error"),
    [
        ("../../outside/target.rs", "escapes the Rust workspace"),
        ("/unused", "escapes the Rust workspace"),
        ("tools/target.rs", "symlinked parent directory"),
    ],
)
def test_main_rejects_explicit_targets_that_escape_the_copy(
    target_path: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit paths cannot reach original sources through escapes or directories."""
    root = copy_boundary_repository(tmp_path)
    crate = root / "rust/cuprum-streams"
    external = root / "outside/target.rs"
    external.parent.mkdir()
    external.write_text(SAFE_SOURCE, encoding="utf-8")
    if target_path == "/unused":
        target_path = str(external)
    if target_path.startswith("tools/"):
        (crate / "tools").symlink_to("../../outside", target_is_directory=True)
    manifest = crate / "Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f'\n[[bin]]\nname = "escaped-target"\npath = "{target_path}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(contract, "ROOT", root)

    with pytest.raises(ValueError, match=expected_error):
        contract.main()


def test_probe_appended_preserves_crlf_bytes_after_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe restoration preserves raw bytes even when its first write fails."""
    target = tmp_path / "target.rs"
    original = b"//! CRLF source.\r\n#![forbid(unsafe_code)]\r\n"
    target.write_bytes(original)
    probe = "pub fn unsafe_probe() { unsafe {} }"
    with contract._probe_appended(target, probe):
        assert target.read_bytes() == original + b"\n" + probe.encode() + b"\n", (
            "the probe must preserve the original CRLF bytes"
        )
    assert target.read_bytes() == original, "the normal probe path must restore bytes"
    original_write = type(target).write_bytes

    def write_bytes(path: Path, data: bytes) -> int:
        """Simulate a partial first write while allowing the restoration write."""
        if path == target and data != original:
            original_write(path, data[:1])
            msg = "injected probe write failure"
            raise OSError(msg)
        return original_write(path, data)

    monkeypatch.setattr(type(target), "write_bytes", write_bytes)
    with (
        pytest.raises(OSError, match="injected probe write failure"),
        contract._probe_appended(target, probe),
    ):
        pass
    assert target.read_bytes() == original, (
        "the failed first probe write must still restore the original raw bytes"
    )
