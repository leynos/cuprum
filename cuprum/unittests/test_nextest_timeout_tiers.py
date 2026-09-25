"""Contract for nextest's own timeout tiers.

The two inner tiers of a coverage run live in ``rust/.config/nextest.toml``
and are nextest's to enforce: the per-test allowance, which bounds one
test's termination, and the whole-run budget above it. Both are read from
the Cargo workspace rather than the repository root, because nextest
resolves its repository configuration there and searches no parent
directory, and Cuprum keeps no root ``Cargo.toml``.

The tiers outside nextest -- the shared coverage action's cargo watchdog
and the job's own ``timeout-minutes`` -- belong to
``test_timeout_ordering_contract``, which is the only module spanning both
halves. The two assertions that compare a nextest tier against the
watchdog stay there for that reason, and this module holds the ones that
read nextest's configuration alone.

Every value here is asserted by value rather than only as a ratio, because
a comparison is satisfied by any pair that orders correctly: a profile
narrowed towards the largest healthy test would pass the ordering
assertions while terminating tests that are working.

See the coverage timeout tiers in
``docs/coverage-timeout-tiers.md``, and the canonical wording in
`leynos/shared-actions`' `generate-coverage` README.
"""

from __future__ import annotations

from cuprum.unittests._timeout_lane_support import (
    EXPECTED_GLOBAL_TIMEOUT_SECONDS,
    EXPECTED_NEXTEST_MIN_VERSION,
    EXPECTED_PER_TEST_ALLOWANCE_SECONDS,
    NEXTEST_CONFIG,
    _allowance_of,
    _default_nextest_profile,
    _slow_timeout_of,
    declared_minimum_nextest_version,
    global_timeout_seconds,
    largest_per_test_allowance_seconds,
    nextest_config_path,
)
from tests.helpers.docs import repo_root


def test_the_nextest_config_sits_where_nextest_looks_for_it() -> None:
    """Nextest reads its config from the workspace root, not the repository root.

    Nextest resolves repository configuration from
    `<workspace>/.config/nextest.toml` and searches no parent directory, so a
    copy at the repository root is never read. Cuprum keeps no root
    `Cargo.toml`, making `rust/` the workspace; a config written one level up
    would leave both inner tiers inert while every value assertion below still
    passed, because those read the file rather than the run.

    Proved by mutation: moving the file to the repository root and running
    `cargo nextest list --manifest-path rust/Cargo.toml` exits 0 and reports
    no parse error, while an invalid value at the workspace root exits 96.
    """
    misplaced = repo_root() / ".config" / "nextest.toml"
    assert not misplaced.is_file(), (
        "a nextest configuration at the repository root is never read; nextest "
        f"resolves {NEXTEST_CONFIG} from the Cargo workspace root and searches "
        "no parent directory, so the tiers it declares would be inert beneath "
        "a watchdog sized for neither"
    )
    assert nextest_config_path().is_file(), (
        f"expected the nextest configuration at {NEXTEST_CONFIG}, the path "
        "nextest resolves from the Cargo workspace root"
    )


def test_the_nextest_tiers_are_explicitly_set() -> None:
    """The default profile declares both inner timeout tiers in full.

    Every other assertion in this module compares the tiers with each
    other, and a comparison is satisfied by any pair that orders
    correctly. Narrowing the profile to ``period = "1s"`` with
    ``terminate-after = 1`` keeps ``global-timeout`` the larger of the two
    while killing healthy tests, which is the outcome the per-test tier
    exists to prevent, and a ``global-timeout`` of two seconds bounds the
    whole run below a single test's allowance. The issue's contract states
    the per-test allowance as a bound rather than a measurement -- large
    enough that no healthy test reaches it -- so the values are pinned
    here, and the ordering assertions below are what the pinned values are
    then held to.
    """
    profile = _default_nextest_profile()
    slow_timeout = _slow_timeout_of(profile)
    for key in ("period", "terminate-after"):
        assert key in slow_timeout, (
            f"{NEXTEST_CONFIG} must set [profile.default].slow-timeout.{key}"
        )
    assert "global-timeout" in profile, (
        f"{NEXTEST_CONFIG} must set [profile.default].global-timeout"
    )
    allowance = _allowance_of(slow_timeout)
    assert allowance == EXPECTED_PER_TEST_ALLOWANCE_SECONDS, (
        f"{NEXTEST_CONFIG}'s per-test allowance is {allowance} s, not the "
        f"{EXPECTED_PER_TEST_ALLOWANCE_SECONDS} s this repository sizes for; "
        f"the ordering assertions are satisfied by any pair that merely orders "
        f"correctly, so a profile narrowed towards the largest healthy test "
        f"would still pass them while terminating tests that are working"
    )
    whole_run = global_timeout_seconds()
    assert whole_run == EXPECTED_GLOBAL_TIMEOUT_SECONDS, (
        f"{NEXTEST_CONFIG}'s global-timeout is {whole_run} s, not the "
        f"{EXPECTED_GLOBAL_TIMEOUT_SECONDS} s this repository sizes for; a "
        f"value below a single test's allowance would pass the containment "
        f"assertion while bounding the run shorter than the tests it runs"
    )


def test_the_nextest_global_timeout_contains_the_per_test_allowance() -> None:
    """A whole-run budget must outlast a test's termination budget."""
    assert global_timeout_seconds() > largest_per_test_allowance_seconds(), (
        f"{NEXTEST_CONFIG}'s global-timeout must exceed its largest per-test "
        "allowance (slow-timeout.period multiplied by terminate-after)"
    )


def test_the_nextest_slow_timeout_terminates_hung_tests() -> None:
    """Slow warnings must eventually kill the test that caused them."""
    assert "terminate-after" in _slow_timeout_of(_default_nextest_profile()), (
        f"{NEXTEST_CONFIG} must set "
        "[profile.default].slow-timeout.terminate-after so a hung test is "
        "killed rather than reported slow indefinitely"
    )


def test_the_nextest_config_declares_the_version_that_understands_it() -> None:
    """The floor is what stops an older release discarding a tier silently.

    Every other assertion in this module reads the configuration file rather
    than the run, so on a release predating ``global-timeout`` they all pass
    while the whole-run budget is a key nextest warns about and ignores. The
    declaration turns that into a refusal to start, and it is the only
    assertion here whose subject is the tool rather than the file.

    The floor is asserted by value against the constant, which the Makefile
    duplicates as ``NEXTEST_MIN_VERSION``; a test in
    ``test_toolchain_pins`` compares the two, so raising one alone fails.

    Proved by mutation: removing the declaration fails the reader's guard,
    and raising the declared version above the installed one makes
    `cargo nextest list` exit 92 rather than run.
    """
    declared = declared_minimum_nextest_version()
    assert declared == EXPECTED_NEXTEST_MIN_VERSION, (
        f"{NEXTEST_CONFIG} declares nextest-version={declared!r}, not the "
        f"{EXPECTED_NEXTEST_MIN_VERSION!r} this repository sizes for; a floor "
        f"below the release that first understood global-timeout would let "
        f"that tier be discarded while every other assertion here passed"
    )
