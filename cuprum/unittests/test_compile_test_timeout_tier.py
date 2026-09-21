"""Contract for the allowance the `trybuild` UI tests run under.

Every other Rust test in this repository is bounded by work it does; these
two are bounded by a compilation. `trybuild` builds a scratch crate for
each UI case, and the scratch workspace is a target directory of its own,
so neither nextest's warm build nor the coverage lane's cache can serve
it. They also inherit `RUSTFLAGS` from the `cargo` that invoked them,
which makes `make test`'s dev-fast flags (`--jobs 1`, `-C
codegen-units=1`, `CARGO_BUILD_JOBS=1`) several times slower than the
coverage lane's plain `-D warnings`.

The profile's per-test allowance is sized for tests that do work, so these
two need an override. This module holds the override to that, and holds
the filter that carries it to the binaries that need it: an override whose
filter matches nothing is inert, and an inert override still reads as
present to every assertion that only checks the value is there.

See the coverage timeout tiers in
``docs/coverage-timeout-tiers.md``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Cargo argv, pinned manifest.
import typing as typ
from pathlib import Path

from cuprum.unittests._timeout_lane_support import (
    NEXTEST_CONFIG,
    _allowance_of,
    _default_nextest_profile,
    _slow_timeout_of,
    _slow_timeout_overrides,
)
from tests.helpers.docs import repo_root

#: The nextest filter that must carry the widened compile-test tier, and
#: the binaries it has to match.
#:
#: `trybuild` compiles a scratch crate per UI case, so its tests are
#: bounded by a build rather than by test work. They live in a binary
#: named ``compile_tests`` in each crate, which is what the filter
#: selects. The sources are pinned alongside the filter because a filter
#: that matches nothing is inert: renaming either file would leave the
#: override in place, still asserting its own presence, while the tests it
#: was written for fell back to the allowance that killed one of them.
COMPILE_TEST_FILTER: typ.Final[str] = "binary(compile_tests)"

#: The Cargo binary name the filter selects, and the manifest that
#: resolves it. `binary()` matches a *target name*, not a source path, so
#: a file existing at the pinned path does not show the filter reaches it:
#: a rename in the crate manifest would move the target name while leaving
#: the file where it was.
COMPILE_TEST_TARGET_NAME: typ.Final[str] = "compile_tests"

#: The workspace manifest whose targets the resolution reads.
WORKSPACE_MANIFEST: typ.Final[Path] = Path("rust") / "Cargo.toml"

#: The allowance that override must grant, in seconds.
#:
#: Pinned by value rather than only asserted to exceed the profile,
#: because exceeding the profile is not the requirement -- exceeding a
#: cold build is. An override set to the profile's 300 s plus one second
#: satisfies every relation this module checks while still terminating a
#: healthy compile: the largest figure measured for these tests is
#: 277.049 s, which a 301 s tier does not clear with the margin the gate
#: runs actually need. The tier is ten 60 s periods.
EXPECTED_COMPILE_TEST_ALLOWANCE_SECONDS: typ.Final[int] = 10 * 60

COMPILE_TEST_SOURCES: typ.Final[tuple[str, ...]] = (
    "rust/cuprum-rust/tests/compile_tests.rs",
    "rust/cuprum-streams/tests/compile_tests.rs",
)


def _resolved_test_targets() -> dict[str, set[str]]:
    """Return Cargo's test-target names, keyed by source path.

    Returns
    -------
    dict[str, set[str]]
        One entry per test source in the workspace, mapping the source
        path — relative to the repository root, POSIX-separated — to the
        set of target names Cargo builds from it.

    Requires `cargo` on PATH, and Cargo to resolve the manifest; the
    assertion and the `check=True` below state both failures in full.

    A set per source rather than a single name, because one source can
    carry more than one target: a `[[test]]` table may name a target while
    pointing at a path Cargo still autodiscovers under its file stem, and
    Cargo then builds both. A caller that kept one name per path would be
    handed whichever of the two Cargo happened to list last.

    Resolves through Cargo rather than reading the manifests, because the
    target name is what `binary()` selects and Cargo is what defines it: a
    `[[test]]` table may set `name` and `path` independently, and Cargo
    derives a name from the file stem when they are absent. Reading the
    toml would reimplement that derivation and could disagree with it.
    `--no-deps` keeps this offline and registry-free, so it resolves the
    same way on a host that has never fetched a dependency.
    """
    cargo = shutil.which("cargo")
    assert cargo is not None, (
        "the compile-test tier is carried by a nextest `binary()` filter, so "
        "resolving what that filter selects needs Cargo on PATH"
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv, no shell needed.
        [
            cargo,
            "metadata",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            str(repo_root() / WORKSPACE_MANIFEST),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    metadata = typ.cast("dict[str, object]", json.loads(completed.stdout))
    packages = metadata.get("packages")
    assert isinstance(packages, list), (
        "`cargo metadata` must report the workspace packages"
    )
    resolved: dict[str, set[str]] = {}
    for package in packages:
        assert isinstance(package, dict), (
            "`cargo metadata` must report each package as an object"
        )
        for target in package.get("targets", []):
            if target.get("kind") != ["test"]:
                continue
            source = Path(target["src_path"])
            key = source.relative_to(repo_root()).as_posix()
            resolved.setdefault(key, set()).add(str(target["name"]))
    return resolved


def test_the_trybuild_tests_carry_their_own_allowance() -> None:
    """The compile-driven tests need more than the profile's allowance.

    `trybuild` compiles a scratch crate for each UI case, so these tests
    are bounded by a build rather than by test work, and they inherit
    `RUSTFLAGS` from the `cargo` that invoked them. `make test` passes
    `--jobs 1`, `-C codegen-units=1` and `CARGO_BUILD_JOBS=1`, and the
    scratch workspace is a separate target directory that nextest's own
    warm build cannot serve, so the build is several times slower there
    than in the coverage lane's plain `-D warnings`.

    Without an override these tests fall to the profile's allowance, which
    this repository has already seen kill one of them while it was still
    compiling a dependency and was therefore healthy: the gate run of
    2026-09-19 terminated `compile_time_ui` at 300 s, against 277 s
    measured for the same test on an unloaded machine. A tier a healthy
    test reaches is not a bound, so the override is asserted to exist, to
    carry the expected filter, and to exceed the profile's allowance.

    Proved by mutation: removing the override, reducing its multiplier to
    the profile's, changing its filter away from `compile_tests`, and
    renaming a target in its crate manifest each fail this test. The last
    is the one the file-existence check cannot see, which is why the
    sources are resolved through Cargo instead.
    """
    profile = _default_nextest_profile()
    base = _allowance_of(_slow_timeout_of(profile))
    matching = [
        override
        for override in _slow_timeout_overrides(profile)
        if str(override.get("filter")) == COMPILE_TEST_FILTER
    ]
    assert matching, (
        f"no override in {NEXTEST_CONFIG} carries filter "
        f"{COMPILE_TEST_FILTER!r}, so the widened tier is granted to whatever "
        f"the declared filters select instead of to the `trybuild` binaries "
        f"this tier exists for; they fall back to the profile's {base} s, "
        f"which this repository has already seen kill one of them while it was "
        f"still compiling a dependency and was therefore healthy"
    )
    widest = max(_allowance_of(_slow_timeout_of(override)) for override in matching)
    assert widest > base, (
        f"the override carrying filter {COMPILE_TEST_FILTER!r} grants {widest} "
        f"s, no more than the profile's {base} s, so it does not widen the tier "
        f"the `trybuild` tests need widened: the gate run of 2026-09-19 "
        f"terminated `compile_time_ui` at 300 s against 277 s measured for the "
        f"same test on an unloaded machine, so a healthy test reaches it"
    )
    assert widest == EXPECTED_COMPILE_TEST_ALLOWANCE_SECONDS, (
        f"the override carrying filter {COMPILE_TEST_FILTER!r} grants {widest} "
        f"s, not the {EXPECTED_COMPILE_TEST_ALLOWANCE_SECONDS} s sized for it. "
        f"Exceeding the profile is not the requirement; exceeding the cold "
        f"build is. The largest figure measured for these tests is 277.049 s, "
        f"and an override set just above the profile's 300 s would still leave "
        f"a healthy compile terminating"
    )
    missing = [
        source
        for source in COMPILE_TEST_SOURCES
        if not (repo_root() / source).is_file()
    ]
    assert not missing, (
        f"{COMPILE_TEST_FILTER!r} selects a binary built from these sources, "
        f"but {missing} no longer exist; a filter matching nothing leaves the "
        f"override inert while it still reads as present, and the tests it was "
        f"written for fall back to the allowance that killed one of them"
    )
    # Existing is not the same as selected. `binary()` matches a target
    # *name*, so the sources are resolved through Cargo and each must be a
    # source that builds a test target named `compile_tests`; a rename in a
    # crate manifest would leave the files in place and the filter matching
    # nothing. A source resolving to no name at all is refused for the same
    # reason, naming the source rather than reporting the mismatch as a
    # rename.
    targets = _resolved_test_targets()
    resolving = {source: targets.get(source) for source in COMPILE_TEST_SOURCES}
    expected = {source: {COMPILE_TEST_TARGET_NAME} for source in COMPILE_TEST_SOURCES}
    assert resolving == expected, (
        f"{COMPILE_TEST_FILTER!r} selects a test target named "
        f"{COMPILE_TEST_TARGET_NAME!r}, but Cargo resolves the pinned sources "
        f"to {resolving}; a target renamed away from that leaves the override "
        f"inert while it still reads as present, and the tests it was written "
        f"for fall back to the allowance that killed one of them"
    )
