"""Contract for the termination allowance the outer watchdog must cover.

``termination_allowance_seconds()`` reads nextest's ``slow-timeout``
``grace-period``, the window between nextest asking a test to stop and
killing it. The watchdog above the test tiers has to cover that window
whenever it opens, so the reader must report the largest grace period any
test can be terminated under.

The live configuration declares no grace period at all, so the module
cannot be driven by it. Every value the reader can produce today is the
60-second floor, and a reader that read only the profile's grace period
would return exactly the same 60 — meaning a future override that widened
the window would be left uncontained by the watchdog with no test
noticing. So the reader is driven here with synthetic configurations
instead, the way ``lanes_in`` is driven with a synthetic workflow, and
the configurations are written as the arithmetic they encode rather than
as the values this repository happens to declare.

An override's ``slow-timeout`` replaces the profile's table for the tests
its filter matches rather than merging into it, so an override that
declares a grace period has replaced whatever the profile declared, and
neither value can be inferred from the other. That is why the table below
carries rows where the wider grace period is on either side.

See the coverage timeout tiers in
``docs/coverage-timeout-tiers.md``.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.unittests import _timeout_lane_support as support

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: A grace period beyond anything the watchdog could contain, so a reader
#: that picks it up is unmistakable next to the floor.
OVERRIDE_GRACE_PERIOD: typ.Final[str] = "1h"

#: The floor this repository imposes, and the value every row below is
#: distinguished from.
FLOOR_SECONDS: typ.Final[int] = 60


def _config(profile: str, override: str = "") -> str:
    """Return a nextest configuration carrying the given declarations.

    Parameters
    ----------
    profile : str
        The grace-period clause for ``[profile.default].slow-timeout``,
        given as the text between the braces, or the empty string for a
        profile that configures none.
    override : str
        The grace-period clause for an override's ``slow-timeout``, or the
        empty string for a configuration that declares no override.

    Returns
    -------
    str
        A configuration document nextest could parse, declaring the
        compile-test override when one was asked for.
    """
    lines = [
        "[profile.default]",
        f'slow-timeout = {{ period = "60s", terminate-after = 5{profile} }}',
        'global-timeout = "20m"',
    ]
    if override:
        lines += [
            "[[profile.default.overrides]]",
            "filter = 'binary(compile_tests)'",
            f'slow-timeout = {{ period = "60s", terminate-after = 10{override} }}',
        ]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _clear_nextest_config_cache() -> cabc.Iterator[None]:
    """Keep each synthetic parse from being served to the next test.

    ``_nextest_config`` is cached, so a parse of one document would
    otherwise answer for every document after it, and the last synthetic
    one would answer for the live file the rest of the contract reads.
    """
    support._nextest_config.cache_clear()
    yield
    support._nextest_config.cache_clear()


@pytest.mark.parametrize(
    ("clauses", "expected"),
    [
        pytest.param(("", ""), FLOOR_SECONDS, id="neither-configured"),
        pytest.param(
            (', grace-period = "5s"', ""), FLOOR_SECONDS, id="below-the-floor"
        ),
        pytest.param((', grace-period = "90s"', ""), 90, id="profile-only"),
        pytest.param(("", ', grace-period = "1h"'), 3600, id="override-only"),
        pytest.param(
            (', grace-period = "90s"', ', grace-period = "1h"'),
            3600,
            id="override-wider",
        ),
        pytest.param(
            (', grace-period = "1h"', ', grace-period = "90s"'),
            3600,
            id="profile-wider",
        ),
    ],
)
def test_the_widest_configured_grace_period_is_the_allowance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clauses: tuple[str, str],
    expected: int,
) -> None:
    """Every grace period is read, and the widest one is reported.

    ``clauses`` is the profile's grace-period clause and the override's, in
    that order, each the text between a ``slow-timeout`` table's braces.

    The ``override-only`` row is the one that matters most: it is the row a
    profile-only reader gets wrong, and the live configuration cannot
    produce it, because the live configuration declares no grace period at
    all. Without that row the reader could drop every override and the
    contract would still pass, which is the state this test was written to
    end.

    The two ``wider`` rows exist because an override's ``slow-timeout``
    replaces the profile's rather than merging into it: neither side can be
    treated as the seat of the value, so the maximum has to be taken across
    both.

    Proved by mutation: reducing the reader to the profile's grace period
    alone fails ``override-only`` and ``override-wider``, and reporting the
    profile's value without the floor fails ``below-the-floor``.
    """
    path = tmp_path / "nextest.toml"
    path.write_text(_config(*clauses), encoding="utf-8")
    monkeypatch.setattr(support, "nextest_config_path", lambda: path)

    allowance = support.termination_allowance_seconds()
    assert allowance == expected, (
        f"the reader reported {allowance} s for a configuration declaring "
        f"the profile clause {clauses[0]!r} and the override clause "
        f"{clauses[1]!r}, not the {expected} s those clauses justify"
    )
