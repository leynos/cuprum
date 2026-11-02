"""Contract keeping Rust compile-time checks inside the ordinary build graph.

`trybuild` compiled a scratch crate for every UI case in a target directory of
its own. It added `--cfg trybuild` to every crate's flags and folded every
dev-dependency into the scratch graph, so no artefact of the workspace build
could serve it, and the second build ran inside one test's timer while queuing
behind the host-wide compiler admission pool. No allowance bounds that queue;
the tier was raised from 300 s to 1800 s and still expired mid-build. ADR-027
moved the compile-pass cases into ordinary doctests and the compile-fail
cases into `compile_fail` doctests, which rustdoc compiles against the
artefacts the test build already produced.

This module keeps it that way. No workspace manifest, nor the lockfile, may
bring `trybuild` back; no Rust source may spawn Cargo or the compiler; every
`compile_fail` doctest must name the error it expects and sit in a doc comment
that also holds a compiling positive control; and that control must import
everything the negative case does. Stable rustdoc ignores the error code, so
on those toolchains the control is what stops a broken include or import from
making the negative case pass for the wrong reason. The doc-comment scanner is
in ``_rust_doctest_support``.

See ``docs/adrs/adr-0027-compile-contracts-in-the-build-graph.md``.
"""

from __future__ import annotations

import re
import tomllib
import typing as typ

import pytest

from cuprum.unittests._rust_doctest_support import (
    compile_fail_count,
    compile_fail_defects,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The dependency tables a manifest can declare, at the top level, under
#: ``[workspace]``, or under ``[target.'cfg(...)']``.
DEPENDENCY_TABLES: typ.Final[tuple[str, ...]] = (
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
)

#: Rust idioms that start a nested Cargo or compiler from inside a test.
NESTED_BUILD_PATTERNS: typ.Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r'Command::new\(\s*"(?:cargo|rustc|rustdoc)"'),
    re.compile(r'env!\(\s*"CARGO"\s*\)'),
    re.compile(r'var(?:_os)?\(\s*"CARGO"\s*\)'),
)

#: Every `compile_fail` doctest in the workspace, counted per source file.
#:
#: Pinned so a deleted contract fails here rather than vanishing silently:
#: each scanned assertion below is satisfied by a file that holds no
#: doctests at all.
EXPECTED_COMPILE_FAIL_DOCTESTS: typ.Final[dict[str, int]] = {
    "rust/cuprum-native-io/src/memory.rs": 1,
    "rust/cuprum-native-io/src/windows.rs": 2,
    "rust/cuprum-rust/src/lib.rs": 1,
    "rust/cuprum-streams/src/lib.rs": 2,
}


def _rust_root() -> Path:
    """Return the Cargo workspace directory."""
    return repo_root() / "rust"


def _workspace_files(pattern: str) -> list[Path]:
    """Return workspace files matching ``pattern``, excluding build output."""
    target = _rust_root() / "target"
    return sorted(
        path for path in _rust_root().rglob(pattern) if target not in path.parents
    )


def _relative(path: Path) -> str:
    """Return ``path`` relative to the repository root, POSIX-separated."""
    return path.relative_to(repo_root()).as_posix()


def _dependency_scopes(manifest: dict[str, object]) -> list[dict[str, object]]:
    """Return the manifest tables that may hold dependency tables."""
    targets = manifest.get("target", {})
    target_scopes = list(targets.values()) if isinstance(targets, dict) else []
    candidates = [manifest, manifest.get("workspace", {}), *target_scopes]
    return [scope for scope in candidates if isinstance(scope, dict)]


def _dependency_names(manifest: dict[str, object]) -> set[str]:
    """Return every dependency name a parsed manifest declares."""
    return {
        str(name)
        for scope in _dependency_scopes(manifest)
        for table in DEPENDENCY_TABLES
        if isinstance(declared := scope.get(table), dict)
        for name in declared
    }


def test_no_workspace_manifest_depends_on_trybuild() -> None:
    """`trybuild` rebuilds the whole graph inside a timed test, so it is banned."""
    offenders = [
        _relative(manifest)
        for manifest in _workspace_files("Cargo.toml")
        if "trybuild" in _dependency_names(tomllib.loads(manifest.read_text()))
    ]
    assert not offenders, (
        f"{offenders} declare `trybuild`; it compiles a second, never-shared "
        f"dependency graph inside one test's timer (ADR-027). Use a "
        f"doctest for a compile-pass case and a `compile_fail,EXXXX` "
        f"doctest beside a positive control for a compile-fail case"
    )


def test_the_lockfile_resolves_no_trybuild() -> None:
    """A lockfile entry would show a manifest still pulls `trybuild` in."""
    lock = tomllib.loads((_rust_root() / "Cargo.lock").read_text())
    names = {str(package.get("name")) for package in lock.get("package", [])}
    assert "trybuild" not in names, (
        "rust/Cargo.lock still resolves `trybuild`, so some manifest or "
        "dependency brings the nested scratch build back (ADR-027)"
    )


def test_no_rust_source_starts_a_nested_build() -> None:
    """A test that spawns Cargo charges a host-shared queue to its own timer."""
    offenders = [
        f"{_relative(path)}: {pattern.pattern}"
        for path in _workspace_files("*.rs")
        for pattern in NESTED_BUILD_PATTERNS
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"these sources start a nested Cargo or compiler: {offenders}. Its "
        f"queue wait on the shared compiler pool is charged to one test's "
        f"allowance, which no timeout can bound (ADR-027)"
    )


def test_every_compile_fail_doctest_is_pinned_and_controlled() -> None:
    """Each negative case names its error and sits beside a positive control."""
    defects = {
        _relative(path): found
        for path in _workspace_files("*.rs")
        if (found := compile_fail_defects(path.read_text(encoding="utf-8")))
    }
    assert not defects, (
        f"`compile_fail` doctests must name the error they expect and share "
        f"their doc comment with a compiling positive control that imports "
        f"everything they do, or they can pass for an unrelated failure on a "
        f"toolchain whose rustdoc ignores the error code: {defects}"
    )


def test_the_compile_fail_inventory_is_pinned() -> None:
    """A deleted compile-fail contract must fail here, not vanish."""
    counts = {
        _relative(path): count
        for path in _workspace_files("*.rs")
        if (count := compile_fail_count(path.read_text(encoding="utf-8")))
    }
    assert counts == EXPECTED_COMPILE_FAIL_DOCTESTS, (
        f"the workspace holds {counts} `compile_fail` doctests, not the "
        f"reviewed {EXPECTED_COMPILE_FAIL_DOCTESTS}; update the inventory "
        f"only alongside the contract being added or retired"
    )


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        pytest.param('[dev-dependencies]\ntrybuild = "1"\n', True, id="dev"),
        pytest.param(
            "[target.'cfg(windows)'.dev-dependencies]\ntrybuild = \"1\"\n",
            True,
            id="target-scoped",
        ),
        pytest.param(
            '[workspace.dependencies]\ntrybuild = "1"\n', True, id="workspace"
        ),
        pytest.param('[dependencies]\nrstest = "1"\n', False, id="absent"),
    ],
)
def test_the_manifest_scanner_reads_every_dependency_table(
    *, manifest: str, expected: bool
) -> None:
    """A dependency hidden in a scoped table is still found."""
    assert ("trybuild" in _dependency_names(tomllib.loads(manifest))) is expected, (
        "every dependency table a manifest can declare must be scanned"
    )
