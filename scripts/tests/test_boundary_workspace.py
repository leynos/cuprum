"""Exercise isolated boundary workspace copying and probe topology."""

from __future__ import annotations

import dataclasses
import stat
import tomllib
import typing as typ
from pathlib import Path

import pytest

from scripts import check_boundary_contract as contract
from scripts.boundary_compile import (
    COMPILE_TIMEOUT_SECONDS,
    CompileTargets,
    compile_arguments,
)
from scripts.boundary_workspace import copy_workspace
from scripts.cargo_state import create_run_cargo_home
from scripts.check_boundary_contract import safe_target_roots
from scripts.tests import boundary_harness_support

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.prepared_cargo_run import PreparedCargoRun

from scripts.tests.boundary_harness_support import copy_boundary_repository

SAFE_SOURCE = "//! Safe target.\n#![forbid(unsafe_code)]\n"
CARGO_METADATA_CALLS_PER_SYMLINK_TEST = 2
SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS = 3900


class _MarkedTestFunction(typ.Protocol):
    """Describe a test function decorated with pytest marks."""

    pytestmark: list[pytest.Mark]


@dataclasses.dataclass(frozen=True, slots=True)
class _ExternalSourceLink:
    """Record one external source link and the original file state."""

    link: Path
    source: Path
    contents: bytes
    mode: int


@dataclasses.dataclass(frozen=True, slots=True)
class _MaterializedWorkspaceCheck:
    """Group the source links and run inputs for one real compile check."""

    root: Path
    crate: Path
    links: tuple[_ExternalSourceLink, ...]
    original_targets: tuple[Path, ...]
    prepared_run: PreparedCargoRun
    compile_workspace: cabc.Callable[[Path, CompileTargets], tuple[int, str]]


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
    assert copied_nested in boundary_harness_support.cargo_target_roots(
        workspace / "cuprum-streams"
    ), "Cargo metadata must retain the nested source directory named target"
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
        "_prepare_run_cargo_home",
        lambda _run, _workspace: 0.0,
    )
    monkeypatch.setattr(
        contract,
        "_check_target",
        lambda _workspace, target, _logs: scheduled.append(target),
    )

    contract.main()

    run_roots = sorted((root / ".cache/boundary-contract/runs").glob("run-*"))
    assert len(run_roots) == 1, "the contract must use one private run root"
    run_nested = run_roots[0] / "workspace" / nested.relative_to(rust)
    assert run_nested in scheduled, (
        "the run-local nested target must be scheduled for unsafe probes"
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
    for link, source, _target in paths:
        source.write_text(SAFE_SOURCE, encoding="utf-8")
        source.chmod(stat.S_IRUSR)
        # The crate has no `tests/`; a missing parent must not read as a skip.
        link.parent.mkdir(exist_ok=True)
    try:
        for link, source, target in paths:
            link.symlink_to(source if target is None else target)
    except OSError as error:
        pytest.skip(f"the platform cannot create the symlink fixture: {error}")
    return tuple(
        _ExternalSourceLink(
            link, source, source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
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
    assert boundary_harness_support.cargo_target_roots(copied_crate) == tuple(
        copied_crate / path.relative_to(crate) for path in original_targets
    ), "Cargo metadata must retain the copied source-file targets"
    assert all(path.is_file() for path in copied_links), (
        "external source-file links must become regular files inside the copy"
    )
    assert all(not path.is_symlink() for path in copied_links), (
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


# This test uses a recording seam for unsafe probes, then runs one real Cargo
# check. Its two 120 s metadata calls and 3600 s compile fit inside the 3900 s
# function watchdog with 60 s for assertions. The compile bound sizes the
# marker; the measured 181 s cold `CARGO_BUILD_JOBS=1` build does not. Callers
# control `-j`, independently of this budget.
# The global `timeout = 30` remains unchanged for all other tests.
@pytest.mark.timeout(SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS, func_only=True)
def test_main_materializes_external_source_file_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared_cargo_run: PreparedCargoRun,
) -> None:
    """External source links become copied files before the unsafe probes run."""
    root = copy_boundary_repository(tmp_path)
    crate = root / "rust/cuprum-streams"
    links = _external_source_links(root, crate, tmp_path)
    metadata_calls = boundary_harness_support.record_cargo_metadata_calls(monkeypatch)
    before = boundary_harness_support.cargo_target_roots(crate)
    assert links[0].link in before, "Cargo must recognize the relative source-file link"
    assert links[1].link in before, "Cargo must recognize the absolute source-file link"
    observed_sources: list[str] = []
    compile_workspace = contract._compile
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(
        contract, "_compile", _recording_probe_compiler(observed_sources)
    )
    monkeypatch.setattr(
        contract,
        "_prepare_run_cargo_home",
        lambda _run, _workspace: 0.0,
    )

    contract.main()

    _assert_materialized_workspace_compiles(
        _MaterializedWorkspaceCheck(
            root,
            crate,
            links,
            before,
            prepared_cargo_run,
            compile_workspace,
        )
    )
    assert len(metadata_calls) == CARGO_METADATA_CALLS_PER_SYMLINK_TEST, (
        "the real test must bound both Cargo metadata calls"
    )
    assert all(
        any(probe in source for source in observed_sources) for probe in contract.PROBES
    ), "each unsafe probe must reach the isolated copied sources"


def _assert_materialized_workspace_compiles(
    check: _MaterializedWorkspaceCheck,
) -> None:
    """Build the copied linked targets and verify source isolation."""
    run_roots = sorted((check.root / ".cache/boundary-contract/runs").glob("run-*"))
    assert len(run_roots) == 1, "the contract must use one private run root"
    run_root = run_roots[0]
    copied_crate = run_root / "workspace/cuprum-streams"
    create_run_cargo_home(check.prepared_run.cargo_home, run_root)
    code, output = check.compile_workspace(
        copied_crate.parent,
        CompileTargets.integration_tests("relative", "absolute"),
    )
    assert code == 0, output
    _assert_external_links_are_isolated(
        check.crate, copied_crate, check.links, check.original_targets
    )


def test_compile_arguments_preserve_full_production_coverage() -> None:
    """The production compiler command continues to compile every target."""
    arguments = compile_arguments()

    assert arguments == (
        "check",
        "--package",
        "cuprum-streams",
        "--all-targets",
        "--all-features",
        "--frozen",
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
    assert arguments[4:-1] == (
        "--test",
        "relative",
        "--test",
        "absolute",
    ), "the scoped check must compile both materialized test targets"
    assert arguments[-1] == "--frozen", (
        "the scoped check must preserve the copied lockfile and stay offline"
    )
    assert "--all-targets" not in arguments, (
        "the scoped check must not rebuild unrelated package targets"
    )


def test_real_compile_test_timeout_exceeds_compile_budget() -> None:
    """Keep the isolated compile and metadata budgets inside the test tier."""
    marked_test = typ.cast(
        "_MarkedTestFunction", test_main_materializes_external_source_file_symlinks
    )
    markers = marked_test.pytestmark
    timeout_markers = [marker for marker in markers if marker.name == "timeout"]
    assert len(timeout_markers) == 1, (
        "the real compile test must have one timeout marker"
    )
    assert timeout_markers[0].args == (3900,), (
        "the test marker must set a 3900-second outer timeout"
    )
    subprocess_budget = COMPILE_TIMEOUT_SECONDS + (
        CARGO_METADATA_CALLS_PER_SYMLINK_TEST
        * boundary_harness_support.CARGO_METADATA_TIMEOUT_SECONDS
    )
    assert subprocess_budget < SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS, (
        "the test marker must exceed both metadata and compile budgets"
    )

    repository_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    global_timeout = pyproject["tool"]["pytest"]["ini_options"]["timeout"]
    assert isinstance(global_timeout, int), "the global pytest timeout must be numeric"
    assert global_timeout == 30, "the suite-wide pytest timeout must remain 30 seconds"
    assert global_timeout < SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS, (
        "the real compile test must exceed the unchanged global timeout"
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
