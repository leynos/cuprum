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

See "Test timeouts: the tiers this repository sets" in
``docs/developers-guide.md``.
"""

from __future__ import annotations

import typing as typ

from cuprum.unittests._timeout_lane_support import (
    NEXTEST_CONFIG,
    _allowance_of,
    _default_nextest_profile,
    _override_slow_timeouts,
    _slow_timeout_of,
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

COMPILE_TEST_SOURCES: typ.Final[tuple[str, ...]] = (
    "rust/cuprum-rust/tests/compile_tests.rs",
    "rust/cuprum-streams/tests/compile_tests.rs",
)


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
    the profile's, and changing its filter away from `compile_tests` each
    fail this test.
    """
    profile = _default_nextest_profile()
    base = _allowance_of(_slow_timeout_of(profile))
    overrides = _override_slow_timeouts(profile)
    assert overrides, (
        f"{NEXTEST_CONFIG} grants every test the profile's allowance, but the "
        f"`trybuild` UI tests are bounded by a compilation and were measured "
        f"at 277 s against 62 s in CI under `make test`'s dev-fast flags; a "
        f"healthy test reaching the allowance means it is no longer a bound"
    )
    widest = max(_allowance_of(override) for override in overrides)
    assert widest > base, (
        f"{NEXTEST_CONFIG}'s widest override grants {widest} s, no more than "
        f"the profile's {base} s, so it does not widen the tier the `trybuild` "
        f"tests need widened"
    )
    filters = [
        str(override.get("filter"))
        for override in typ.cast("list[dict[str, object]]", profile["overrides"])
        if "slow-timeout" in override
    ]
    assert COMPILE_TEST_FILTER in filters, (
        f"none of {NEXTEST_CONFIG}'s {filters} is {COMPILE_TEST_FILTER!r}, so "
        f"the widened tier is granted to whatever those filters select instead "
        f"of to the `trybuild` binaries this tier exists for"
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
