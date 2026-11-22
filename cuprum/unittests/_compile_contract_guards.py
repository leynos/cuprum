"""Pure guards against the retired ``compile_tests`` test harness.

The offender logic lives here, apart from the tests that assert on the live
repository, so each rule can be exercised with synthetic manifests, paths and
nextest profiles rather than only against the current tree.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: The nextest test target that used to carry the ``trybuild`` UI cases.
RETIRED_TEST_TARGET: typ.Final[str] = "compile_tests"


def declared_test_targets(manifest: dict[str, object]) -> set[str]:
    """Return the names of the ``[[test]]`` targets a manifest declares."""
    targets = manifest.get("test", [])
    if not isinstance(targets, list):
        return set()
    return {
        str(target["name"])
        for target in targets
        if isinstance(target, dict) and isinstance(target.get("name"), str)
    }


def nextest_override_filters(config: dict[str, object]) -> list[str]:
    """Return the filter of every override in every nextest profile."""
    # A named profile such as [profile.ci] can repeat the default's override,
    # so scanning only the default would leave a per-binary allowance
    # reachable under another profile name.
    profiles = config.get("profile", {})
    if not isinstance(profiles, dict):
        return []
    filters: list[str] = []
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        overrides = profile.get("overrides", [])
        if not isinstance(overrides, list):
            continue
        filters.extend(
            str(override["filter"])
            for override in overrides
            if isinstance(override, dict) and "filter" in override
        )
    return filters


def is_integration_test_entry_point(path: Path) -> bool:
    """Return whether ``path`` is a Cargo integration-test entry point."""
    # Cargo infers a compile_tests target from either tests/compile_tests.rs or
    # tests/compile_tests/main.rs at the default autotests setting, so both must
    # be refused even though neither declares a [[test]] table.
    if path.parent.name == "tests":
        return path.name == f"{RETIRED_TEST_TARGET}.rs"
    return (
        path.name == "main.rs"
        and path.parent.name == RETIRED_TEST_TARGET
        and path.parent.parent.name == "tests"
    )


def retired_target_offenders(
    manifests: cabc.Iterable[tuple[Path, dict[str, object]]],
    rust_files: cabc.Iterable[Path],
) -> list[Path]:
    """Return the manifests and entry points that restore the retired target."""
    offenders = [
        path
        for path, manifest in manifests
        if RETIRED_TEST_TARGET in declared_test_targets(manifest)
    ]
    offenders.extend(
        path for path in rust_files if is_integration_test_entry_point(path)
    )
    return offenders


def retired_override_offenders(config: dict[str, object]) -> list[str]:
    """Return the nextest override filters that name the retired target."""
    return [
        override
        for override in nextest_override_filters(config)
        if RETIRED_TEST_TARGET in override
    ]
