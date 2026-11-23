"""Contract that nextest's default scheduling is retained deliberately.

Issue #606 asked whether re-ordering Cuprum's expensive `trybuild` tests would
shorten the Continuous Integration (CI) critical path. Before the question
could be measured, ADR-027 moved the compile-time contracts into the ordinary
build graph and removed the `compile_tests` binaries, their `trybuild`
dependency, and the `binary(compile_tests)` nextest override. ADR-0030
therefore retains nextest's default scheduling and records why the experiment
is superseded.

This module pins the scheduling half of that decision. It refuses a top-level
`test-groups` table, a `test-threads` setting on any profile, and a `priority`
or `test-group` setting on any override, which are the mechanisms issue #606
proposed to use. It deliberately does not refuse an override that declares only
a timeout: an allowance is a timeout-tier concern, pinned by
`test_nextest_timeout_tiers.py` and `test_termination_allowance.py`, and the
filter that names a retired binary is pinned by
`test_compile_contracts_build_graph.py` under ADR-027.

A green suite cannot show that a configuration which serialises the whole run,
or restores a per-binary priority, changes nothing today. Both are latent: the
suite still passes, and the setting only matters once a test becomes slow. The
scanner is therefore driven with synthetic configurations as well as the live
one, so a scanner that reported nothing unconditionally would fail rather than
pass.

The concurrency the run actually uses is separate and caller-owned: both
coverage jobs bind `NEXTEST_TEST_THREADS` and `CARGO_BUILD_JOBS` to
`LINUX_RUNNER_VCPUS`, and `tests/test_ci_test_coverage_overlap.py` pins that
binding. This module reads only the nextest configuration.

See `docs/coverage-timeout-tiers.md` for the timeout tiers that remain, and
ADR-0030 for the evidence and the rollback condition.
"""

from __future__ import annotations

import tomllib
import typing as typ

import pytest

from cuprum.unittests import _timeout_lane_support as support

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The decision record every refusal here points at, so a change that trips one
#: has to update the decision rather than work around the assertion.
DECISION_RECORD: typ.Final[str] = (
    "docs/adrs/adr-0030-retain-default-nextest-scheduling.md"
)

#: The only profile-level key that changes the schedule. A `test-threads` here
#: serialises every test in the run, which is the accidental global
#: serialisation issue #606 warned a test group could cause.
PROFILE_SCHEDULING_KEYS: typ.Final[tuple[str, ...]] = ("test-threads",)

#: Override-level keys, which nextest accepts only inside a per-test override.
#: An override is the only place a priority or a test-group membership can be
#: declared.
OVERRIDE_SCHEDULING_KEYS: typ.Final[tuple[str, ...]] = ("priority", "test-group")


@pytest.fixture(autouse=True)
def _fresh_nextest_config() -> cabc.Iterator[None]:
    """Keep a synthetic parse from answering for the live file.

    `_nextest_config` is cached, so a configuration written by one test would
    otherwise be served to every test after it.
    """
    support._nextest_config.cache_clear()
    yield
    support._nextest_config.cache_clear()


def _profiles(config: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return the configuration's named profiles, or nothing when it has none.

    A synthetic document may declare only a top-level table, such as
    `[test-groups]`, so an absent `[profile]` is not a defect here. The live
    file is required to declare one by the caller.

    Parameters
    ----------
    config : collections.abc.Mapping[str, object]
        A parsed nextest configuration. The live reader returns a
        ``TypedDict``, so the parameter is a mapping rather than a ``dict``.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The profile tables keyed by name, or an empty mapping when the
        document declares none.
    """
    profiles = config.get("profile")
    if not isinstance(profiles, dict):
        return {}
    return profiles


def _scheduling_controls(config: cabc.Mapping[str, object]) -> list[str]:
    """Return every setting in ``config`` that changes the schedule.

    Parameters
    ----------
    config : collections.abc.Mapping[str, object]
        A parsed nextest configuration. It may be the repository's live file
        or a synthetic document written by a test.

    Returns
    -------
    list[str]
        One dotted location per control found, in a stable order. An empty
        list means the configuration leaves scheduling to nextest's defaults
        and to the caller's thread setting.
    """
    found: list[str] = ["test-groups"] if "test-groups" in config else []
    for name, profile in _profiles(config).items():
        if not isinstance(profile, dict):
            continue
        found += [
            f"profile.{name}.{key}" for key in PROFILE_SCHEDULING_KEYS if key in profile
        ]
        overrides = profile.get("overrides")
        if not isinstance(overrides, list):
            continue
        for index, override in enumerate(overrides):
            if not isinstance(override, dict):
                continue
            found += [
                f"profile.{name}.overrides[{index}].{key}"
                for key in OVERRIDE_SCHEDULING_KEYS
                if key in override
            ]
    return found


def test_the_configuration_declares_a_profile_and_no_scheduling_control() -> None:
    """Retain the default schedule instead of the arms #606 proposed.

    A priority, a one-slot group, and a per-profile thread limit are all
    scheduling controls. None has a subject at the refreshed baseline, because
    ADR-027 removed the long-running `compile_tests` binaries, and an override
    is the only place nextest accepts a priority or a test group.
    """
    config = support._nextest_config()
    assert _profiles(config), (
        f"{support.NEXTEST_CONFIG} must declare [profile]; without one nextest "
        f"runs its built-in defaults and {DECISION_RECORD} would be pinning a "
        "schedule the file does not describe"
    )
    found = _scheduling_controls(config)
    assert not found, (
        f"{support.NEXTEST_CONFIG} declares {found}; {DECISION_RECORD} retains "
        "nextest's default scheduling because the compile_tests binaries that "
        "motivated #606 were removed by ADR-027. Reopen the decision with fresh "
        "evidence before adding a priority, a test group, or a thread limit"
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            '[profile.default]\nslow-timeout = { period = "60s" }\n',
            [],
            id="default-schedule",
        ),
        pytest.param(
            '[profile.default]\nslow-timeout = { period = "60s" }\n'
            "[[profile.default.overrides]]\n"
            'filter = "binary(slow)"\n'
            'slow-timeout = { period = "60s", terminate-after = 10 }\n',
            [],
            id="timeout-only-override",
        ),
        pytest.param(
            "[test-groups.slow]\nmax-threads = 1\n",
            ["test-groups"],
            id="test-groups-table",
        ),
        pytest.param(
            "[profile.default]\ntest-threads = 1\n",
            ["profile.default.test-threads"],
            id="global-serialisation",
        ),
        pytest.param(
            '[[profile.default.overrides]]\nfilter = "binary(slow)"\npriority = 100\n',
            ["profile.default.overrides[0].priority"],
            id="priority",
        ),
        pytest.param(
            '[[profile.ci.overrides]]\nfilter = "binary(slow)"\ntest-group = "slow"\n',
            ["profile.ci.overrides[0].test-group"],
            id="named-profile-group",
        ),
    ],
)
def test_the_scheduling_scanner_reports_each_control(
    *, document: str, expected: list[str]
) -> None:
    """Each control must be seen, or the live assertion proves nothing.

    The first two rows are negative controls: a configuration that declares
    only timeouts must scan clean, so a scanner that reported any override
    would fail here rather than silently forbid a legitimate allowance. The
    `named-profile-group` row is the one a scan of `[profile.default]` alone
    gets wrong.
    """
    found = _scheduling_controls(tomllib.loads(document))
    assert found == expected, (
        f"the scanner reported {found} for a document declaring {expected}; "
        f"{DECISION_RECORD} relies on this scanner to distinguish a retained "
        "default schedule from a reintroduced one"
    )
