"""The unsafe-bearing crate allowlist must reject unreviewed workspace growth.

The compiler path is driven through a recorded command seam, so the tests
assert what the harness compiles and requires of each result — including that
every probe is restored — without invoking Cargo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_boundary_contract as contract
from scripts.check_boundary_contract import (
    check_member_lint_inheritance,
    check_members,
    check_safe_policy,
    safe_target_roots,
)
from scripts.tests.boundary_harness_support import copy_boundary_repository

# The one diagnostic string that distinguishes a rejected probe from a compile
# that failed for some unrelated reason.
UNSAFE_MARKERS = ("forbid(unsafe_code)", "-F unsafe-code")

TARGETS = ("src/lib.rs", "tests/compile_tests.rs")
FUTURE_TARGETS = (
    "src/main.rs",
    "src/bin/future_target.rs",
    "build.rs",
    "examples/future_target.rs",
    "tests/future_target.rs",
    "benches/future_target.rs",
)


class _RecordedCompile:
    """Stand in for the harness compiler seam, recording every invocation.

    The result reflects the sources under test: a run whose probed target still
    contains one of the probes fails with the unsafe-forbid diagnostic, and any
    other run compiles cleanly. Deriving the result from the sources rather than
    from call order keeps the seam honest — a harness that skipped a probe, or
    that failed to remove one, changes the sequence the test observes.
    """

    def __init__(self) -> None:
        self.probes: list[str] = []

    def __call__(self, workspace: Path) -> tuple[int, str]:
        """Record one compile and return the result its sources imply."""
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((workspace / "cuprum-streams").rglob("*.rs"))
        )
        self.probes.append(source)
        if any(probe in source for probe in contract.PROBES):
            return 1, f"error: use of unsafe code is forbidden by {UNSAFE_MARKERS[0]}"
        return 0, "finished checking"


def _drive_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, _RecordedCompile]:
    """Run the whole contract harness against a temporary repository copy."""
    root = copy_boundary_repository(tmp_path)
    compiler = _RecordedCompile()
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(contract, "_compile", compiler)
    contract.main()
    return root, compiler


@pytest.mark.parametrize("extra", [", 'new-boundary'", ", 'cuprum-native-io'", ", '*'"])
def test_unreviewed_members_are_rejected(extra: str) -> None:
    """Extra, repeated, and wildcard members all require an explicit audit."""
    manifest = (
        "[workspace]\nmembers = ['cuprum-rust', 'cuprum-native-io', 'cuprum-streams'"
        + extra
        + "]"
    )
    with pytest.raises(ValueError, match="audited boundary inventory"):
        check_members(manifest)


def test_audited_members_are_accepted() -> None:
    """The documented three-crate dependency split is accepted."""
    check_members(
        "[workspace]\nmembers = ['cuprum-rust', 'cuprum-native-io', 'cuprum-streams']"
    )


def test_all_audited_members_inherit_the_workspace_lint_baseline() -> None:
    """Every package must use the root tables without a local replacement."""
    root = Path(__file__).resolve().parents[2] / "rust"
    check_member_lint_inheritance(root)


@pytest.mark.parametrize(
    "member", ["cuprum-rust", "cuprum-native-io", "cuprum-streams"]
)
def test_member_lint_inheritance_rejects_a_local_override(
    member: str, tmp_path: Path
) -> None:
    """No audited member may opt out of the shared lint baseline."""
    root = copy_boundary_repository(tmp_path) / "rust"
    manifest = root / member / "Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[lints]\nworkspace = true", "[lints]\nworkspace = false", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"{member} must inherit"):
        check_member_lint_inheritance(root)


def test_safe_target_roots_cover_every_current_automatic_target(tmp_path: Path) -> None:
    """The target scan includes the library and compile-contract integration test."""
    root = copy_boundary_repository(tmp_path) / "rust"
    actual = tuple(
        path.relative_to(root / "cuprum-streams").as_posix()
        for path in safe_target_roots(root)
    )
    assert actual == TARGETS, "the safe-target scanner must cover every Cargo root"


@pytest.mark.parametrize("target", FUTURE_TARGETS)
def test_new_safe_target_without_an_unsafe_prohibition_is_rejected(
    target: str, tmp_path: Path
) -> None:
    """A future automatic target cannot weaken the safe-crate boundary."""
    root = copy_boundary_repository(tmp_path) / "rust"
    check_safe_policy(root)
    future_target = root / "cuprum-streams" / target
    future_target.parent.mkdir(parents=True, exist_ok=True)
    future_target.write_text(
        "//! Deliberately incomplete safety contract.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        check_safe_policy(root)


def test_configured_build_script_without_an_unsafe_prohibition_is_rejected(
    tmp_path: Path,
) -> None:
    """An explicit build-script path is also an unsafe-policy target root."""
    root = copy_boundary_repository(tmp_path) / "rust"
    manifest = root / "cuprum-streams/Cargo.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "publish = false", 'publish = false\nbuild = "tools/build.rs"'
        ),
        encoding="utf-8",
    )
    target = root / "cuprum-streams/tools/build.rs"
    target.parent.mkdir()
    target.write_text(
        "//! Deliberately incomplete safety contract.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="safe target must forbid unsafe code"):
        check_safe_policy(root)


def test_main_probes_every_target_with_every_unsafe_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline compiles, then each explicit safe root receives every probe."""
    root, compiler = _drive_contract(tmp_path, monkeypatch)

    for probe in contract.PROBES:
        assert sum(probe in text for text in compiler.probes) == len(TARGETS), (
            f"the {probe!r} probe must be compiled against every target"
        )
    logs = root / "rust/target/boundary-verification"
    assert (logs / "safe-positive.log").read_text(encoding="utf-8") == (
        "finished checking"
    ), "the baseline result must be archived"
    expected = [
        f"{Path(target).stem}-unsafe-{index}.log"
        for target in TARGETS
        for index in range(3)
    ]
    assert sorted(path.name for path in logs.glob("*.log")) == sorted([
        "safe-positive.log",
        *expected,
    ]), "every probe result must be archived under its target and form"


def test_every_probed_target_is_restored_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe left behind would poison every later compile."""
    root, _compiler = _drive_contract(tmp_path, monkeypatch)
    workspace = root / ".cache/boundary-contract/workspace"
    for target in TARGETS:
        source = (workspace / "cuprum-streams" / target).read_text(encoding="utf-8")
        for probe in contract.PROBES:
            assert probe not in source, f"{target} was not restored after probing"


def test_a_probe_that_compiles_must_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A safe target that accepts unsafe code must abort the harness."""
    root = copy_boundary_repository(tmp_path)
    monkeypatch.setattr(contract, "ROOT", root)
    # A compiler that never rejects anything stands in for a target whose lint
    # stopped applying; the harness must not report success.
    monkeypatch.setattr(
        contract, "_compile", lambda _workspace: (0, "finished checking")
    )
    with pytest.raises(RuntimeError, match="did not reject unsafe probe"):
        contract.main()


def test_a_failing_baseline_aborts_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline that cannot compile leaves the targets unprobed."""
    root = copy_boundary_repository(tmp_path)
    probed: list[Path] = []
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setattr(
        contract, "_compile", lambda _workspace: (101, "error: no compiler")
    )
    monkeypatch.setattr(
        contract,
        "_check_target",
        lambda _workspace, path, _logs: probed.append(path),
    )
    with pytest.raises(SystemExit):
        contract.main()
    assert not probed, "a broken baseline must not be reported as a probed workspace"


def test_unsafe_detection_requires_both_failure_and_the_forbid_diagnostic() -> None:
    """An unrelated compile failure must not be read as a successful rejection."""
    assert contract._unsafe_was_forbidden(1, "error: forbid(unsafe_code)"), (
        "a failed compile naming forbid(unsafe_code) is a rejection"
    )
    assert contract._unsafe_was_forbidden(1, "error: -F unsafe-code"), (
        "the -F unsafe-code spelling is the same rejection"
    )
    assert not contract._unsafe_was_forbidden(0, "error: forbid(unsafe_code)"), (
        "a diagnostic without a failure exit is not a rejection"
    )
    assert not contract._unsafe_was_forbidden(1, "error: unused import"), (
        "an unrelated failure is not a rejection"
    )


def test_a_probe_is_removed_even_when_compilation_fails(tmp_path: Path) -> None:
    """The context manager restores the source on both exit paths."""
    target = tmp_path / "lib.rs"
    target.write_text("pub fn production() {}\n", encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    probe = "pub fn unsafe_probe() { unsafe {} }"
    with contract._probe_appended(target, probe):
        assert probe in target.read_text(encoding="utf-8"), "the probe was not applied"
    assert target.read_text(encoding="utf-8") == original, (
        "the probe was not removed on the normal path"
    )
    with (
        pytest.raises(ValueError, match="boom"),
        contract._probe_appended(target, probe),
    ):
        raise ValueError("boom")
    assert target.read_text(encoding="utf-8") == original, (
        "the probe was not removed when the body raised"
    )
