"""Behavioural tests for confirming a regression by measuring again.

Stated in terms of what each measurement flagged, because that is what a
maintainer reading a failed job sees: one scenario named twice is a
regression, the same scenario named once is a runner.
"""

from __future__ import annotations

import json
import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from benchmarks.confirm_regression import main as confirm_cli

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

FEATURE = "../features/benchmark_regression_confirmation.feature"
SCENARIOS = ("small-single-nocb", "medium-single-nocb", "medium-single-cb")


@scenario(FEATURE, "A regression that reproduces fails the job")
def test_a_regression_that_reproduces_fails_the_job() -> None:
    """A regression that reproduces fails the job."""


@scenario(FEATURE, "A regression that does not reproduce is treated as noise")
def test_a_regression_that_does_not_reproduce_is_noise() -> None:
    """A regression that does not reproduce is treated as noise."""


@scenario(FEATURE, "A flake that moves to another scenario does not confirm")
def test_a_flake_that_moves_scenario_does_not_confirm() -> None:
    """A flake that moves to another scenario does not confirm."""


@scenario(FEATURE, "The second measurement cannot fail a scenario the first passed")
def test_the_second_measurement_cannot_introduce_a_failure() -> None:
    """The second measurement cannot fail a scenario the first passed."""


@scenario(FEATURE, "An unusable second measurement leaves the first verdict standing")
def test_an_unusable_second_measurement_keeps_the_verdict() -> None:
    """An unusable second measurement leaves the first verdict standing."""


def _flagged(names: str) -> tuple[str, ...]:
    """Parse the scenario names a step states, if any."""
    return () if names == "nothing" else (names.strip(),)


def _report(*regressed: str) -> dict[str, object]:
    """Return a ratchet report flagging the named scenarios."""
    comparisons = [
        {
            "scenario_name": name,
            "baseline_ratio": 1.0,
            "candidate_ratio": 1.9 if name in regressed else 1.01,
            "regression_ratio": 0.9 if name in regressed else 0.01,
            "max_regression": 0.30,
            "is_regression": name in regressed,
        }
        for name in SCENARIOS
    ]
    return {
        "max_regression": 0.30,
        "passed": not regressed,
        "rust_scenarios_compared": len(comparisons),
        "worst_regression_ratio": 0.9 if regressed else 0.01,
        "comparisons": comparisons,
        "regressions": [
            comparison for comparison in comparisons if comparison["is_regression"]
        ],
    }


def _combine_with_cli(
    *,
    tmp_path: pth.Path,
    primary: dict[str, object],
    confirmation: dict[str, object],
) -> dict[str, object]:
    """Execute the confirmation CLI and return its persisted report."""
    primary_path = tmp_path / "primary.json"
    confirmation_path = tmp_path / "confirmation.json"
    output_path = tmp_path / "combined.json"
    primary_path.write_text(json.dumps(primary), encoding="utf-8")
    confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")

    exit_code = confirm_cli([
        "--primary-report",
        str(primary_path),
        "--confirmation-report",
        str(confirmation_path),
        "--output",
        str(output_path),
    ])
    verdict = typ.cast(
        "dict[str, object]", json.loads(output_path.read_text(encoding="utf-8"))
    )

    assert exit_code == int(not verdict["passed"]), (
        "the confirmation CLI exit code must match its persisted verdict"
    )
    return verdict


# -- Given steps ---------------------------------------------------------------


@given(
    parsers.parse("the first measurement flagged {names}"),
    target_fixture="primary",
)
def given_the_first_measurement(names: str) -> dict[str, object]:
    """Record what the first measurement reported."""
    return _report(*_flagged(names))


# -- When steps ----------------------------------------------------------------


@when(
    parsers.parse("a second measurement flags {names}"),
    target_fixture="verdict",
)
def when_a_second_measurement(
    primary: dict[str, object], names: str, tmp_path: pth.Path
) -> cabc.Mapping[str, object]:
    """Combine two measurements through the confirmation CLI."""
    return _combine_with_cli(
        tmp_path=tmp_path,
        primary=primary,
        confirmation=_report(*_flagged(names)),
    )


@when("a second measurement cannot be compared", target_fixture="verdict")
def when_a_second_measurement_cannot_compare(
    primary: dict[str, object],
    tmp_path: pth.Path,
) -> cabc.Mapping[str, object]:
    """Combine with a skip report through the confirmation CLI."""
    return _combine_with_cli(
        tmp_path=tmp_path,
        primary=primary,
        confirmation={
            "baseline_available": True,
            "comparison_performed": False,
            "passed": True,
            "reason": "incompatible_benchmark_profile",
        },
    )


# -- Then steps ----------------------------------------------------------------


def _names(entries: object) -> list[str]:
    """Return the scenario names of a list of comparison entries."""
    return [
        str(entry["scenario_name"])
        for entry in typ.cast("list[dict[str, object]]", entries)
    ]


@then("the ratchet passes")
def then_the_ratchet_passes(verdict: cabc.Mapping[str, object]) -> None:
    """Assert the combined verdict reports no confirmed regression."""
    assert verdict["passed"] is True, (
        f"expected a pass; confirmed {_names(verdict['confirmed_regressions'])}"
    )
    assert verdict["confirmation_status"] in {"unconfirmed", "not_required"}, (
        "a passing confirmation report must persist why no failure remained"
    )


@then(parsers.parse("the ratchet fails on {name}"))
def then_the_ratchet_fails_on(verdict: cabc.Mapping[str, object], name: str) -> None:
    """Assert the named scenario is the confirmed regression."""
    assert verdict["passed"] is False, "expected the reproduced regression to fail"
    assert _names(verdict["confirmed_regressions"]) == [name], (
        f"expected only {name!r} to reproduce; report was {verdict}"
    )


@then(parsers.parse("{name} is reported as unconfirmed"))
def then_reported_as_unconfirmed(verdict: cabc.Mapping[str, object], name: str) -> None:
    """Assert the scenario is recorded as measured once and not reproduced.

    Reported rather than discarded, so a maintainer can see that the job
    considered a regression and decided it was noise.
    """
    assert name in _names(verdict["unconfirmed_regressions"]), (
        f"expected {name!r} to remain unconfirmed; report was {verdict}"
    )
    assert verdict["confirmation_status"] == "unconfirmed", (
        "a completed non-reproducing retry must retain its bounded status"
    )


@then("confirmation is unavailable")
def then_confirmation_is_unavailable(verdict: cabc.Mapping[str, object]) -> None:
    """Assert an unusable retry persists its bounded availability status."""
    assert verdict["confirmation_status"] == "unavailable", (
        "a skipped retry must record unavailable confirmation evidence"
    )
