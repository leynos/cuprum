"""Behavioural tests for the benchmark path gate declared in `ci.yml`.

The unit contract tests pin the declarations that make up the gate — the
filter's path list, the `needs` edge, the `if:` expression. These state the
decision those declarations produce for a pull request a maintainer would
recognize: docs-only, a Rust change, a dependency bump, a mixed diff, and a
push to `main`. Both read the same workflow through `tests.helpers.workflow`,
so neither can pass against a gate the other does not see.
"""

from __future__ import annotations

import dataclasses as dc

from pytest_bdd import given, parsers, scenario, then, when

from tests.helpers.workflow import bench_output, benchmark_runs


@dc.dataclass(frozen=True, slots=True)
class Event:
    """A CI event: what triggered the run, and which paths it changed."""

    name: str
    changed_paths: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class Decision:
    """What the workflow decides for an event."""

    bench: bool
    benchmark_runs: bool


@scenario(
    "../features/benchmark_path_gate.feature",
    "A documentation-only pull request skips the benchmark",
)
def test_documentation_only_pull_request_skips_the_benchmark() -> None:
    """A documentation-only pull request skips the benchmark."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A pull request touching the Rust extension benchmarks",
)
def test_pull_request_touching_the_rust_extension_benchmarks() -> None:
    """A pull request touching the Rust extension benchmarks."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A dependency bump benchmarks even without a source change",
)
def test_dependency_bump_benchmarks() -> None:
    """A dependency bump benchmarks even without a source change."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A mixed pull request benchmarks",
)
def test_mixed_pull_request_benchmarks() -> None:
    """A mixed pull request benchmarks."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A push to main benchmarks whatever it touched",
)
def test_push_to_main_benchmarks_whatever_it_touched() -> None:
    """A push to main benchmarks whatever it touched."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A pull request changing nothing skips the benchmark",
)
def test_pull_request_changing_nothing_skips_the_benchmark() -> None:
    """A pull request changing nothing skips the benchmark."""


# -- Given steps ---------------------------------------------------------------


def _split_paths(paths: str) -> tuple[str, ...]:
    """Split the ``a and b`` path list a scenario states into a tuple."""
    if paths == "no files":
        return ()
    return tuple(path.strip() for path in paths.split(" and "))


@given(
    parsers.parse("a pull request changing {paths}"),
    target_fixture="event",
)
def given_a_pull_request(paths: str) -> Event:
    """Describe a pull request changing the stated paths."""
    return Event(name="pull_request", changed_paths=_split_paths(paths))


@given(
    parsers.parse("a push to main changing {paths}"),
    target_fixture="event",
)
def given_a_push_to_main(paths: str) -> Event:
    """Describe a push to main changing the stated paths."""
    return Event(name="push", changed_paths=_split_paths(paths))


# -- When steps ----------------------------------------------------------------


@when("the workflow classifies the changed paths", target_fixture="decision")
def when_the_workflow_classifies(event: Event) -> Decision:
    """Apply the workflow's declared filter and gate to the event."""
    bench = bench_output(event.changed_paths)
    return Decision(
        bench=bench,
        benchmark_runs=benchmark_runs(event_name=event.name, bench=bench),
    )


# -- Then steps ----------------------------------------------------------------


@then("a performance-relevant change is detected")
def then_a_relevant_change_is_detected(decision: Decision, event: Event) -> None:
    """Assert the filter matched at least one changed path."""
    assert decision.bench, (
        f"the filter must match {list(event.changed_paths)}; a change that can "
        "move measured throughput has to reach the benchmark"
    )


@then("no performance-relevant change is detected")
def then_no_relevant_change_is_detected(decision: Decision, event: Event) -> None:
    """Assert the filter matched none of the changed paths."""
    assert not decision.bench, (
        f"the filter must not match {list(event.changed_paths)}; paying for a "
        "benchmark that cannot move is the cost this gate exists to avoid"
    )


@then("the benchmark job runs")
def then_the_benchmark_job_runs(decision: Decision, event: Event) -> None:
    """Assert the gate admits this event."""
    assert decision.benchmark_runs, (
        f"a {event.name!r} event changing {list(event.changed_paths)} must run "
        "benchmark-ratchet"
    )


@then("the benchmark job is skipped")
def then_the_benchmark_job_is_skipped(decision: Decision, event: Event) -> None:
    """Assert the gate rejects this event."""
    assert not decision.benchmark_runs, (
        f"a {event.name!r} event changing {list(event.changed_paths)} must skip "
        "benchmark-ratchet"
    )
