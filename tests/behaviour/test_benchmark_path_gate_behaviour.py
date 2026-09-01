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
    """Describe a CI event and its changed paths.

    Attributes
    ----------
    name : str
        Event name supplied to the workflow gate.
    changed_paths : tuple[str, ...]
        Paths changed by the event.
    """

    name: str
    changed_paths: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class Decision:
    """Record the filter result and benchmark gate decision.

    Attributes
    ----------
    bench : bool
        Whether the filter found a performance-relevant changed path.
    benchmark_runs : bool
        Whether the benchmark job is admitted for the event.
    """

    bench: bool
    benchmark_runs: bool


@scenario(
    "../features/benchmark_path_gate.feature",
    "A documentation-only pull request skips the benchmark",
)
def test_documentation_only_pull_request_skips_the_benchmark() -> None:
    """Verify that a documentation-only pull request skips the benchmark."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A pull request touching the Rust extension benchmarks",
)
def test_pull_request_touching_the_rust_extension_benchmarks() -> None:
    """Verify that a pull request touching the Rust extension benchmarks."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A dependency bump benchmarks even without a source change",
)
def test_dependency_bump_benchmarks() -> None:
    """Verify that a dependency bump triggers the benchmark."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A mixed pull request benchmarks",
)
def test_mixed_pull_request_benchmarks() -> None:
    """Verify that a mixed pull request triggers the benchmark."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A push to main benchmarks whatever it touched",
)
def test_push_to_main_benchmarks_whatever_it_touched() -> None:
    """Verify that a push to main triggers the benchmark for any paths."""


@scenario(
    "../features/benchmark_path_gate.feature",
    "A pull request changing nothing skips the benchmark",
)
def test_pull_request_changing_nothing_skips_the_benchmark() -> None:
    """Verify that an empty pull request skips the benchmark."""


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
    """Build an event for a pull request changing the stated paths.

    Parameters
    ----------
    paths : str
        Feature-language path list, joined with ``and``.

    Returns
    -------
    Event
        The parsed pull-request event.
    """
    return Event(name="pull_request", changed_paths=_split_paths(paths))


@given(
    parsers.parse("a push to main changing {paths}"),
    target_fixture="event",
)
def given_a_push_to_main(paths: str) -> Event:
    """Build an event for a push to main changing the stated paths.

    Parameters
    ----------
    paths : str
        Feature-language path list, joined with ``and``.

    Returns
    -------
    Event
        The parsed push event.
    """
    return Event(name="push", changed_paths=_split_paths(paths))


# -- When steps ----------------------------------------------------------------


@when("the workflow classifies the changed paths", target_fixture="decision")
def when_the_workflow_classifies(
    event: Event, filter_path_patterns: frozenset[str]
) -> Decision:
    """Apply the declared filter and gate to an event.

    Parameters
    ----------
    event : Event
        Event to classify using the workflow model.
    filter_path_patterns : frozenset[str]
        Performance-relevant paths declared by the workflow fixture.

    Returns
    -------
    Decision
        Filter verdict and benchmark admission for the event.
    """
    bench = bench_output(event.changed_paths, filter_path_patterns)
    return Decision(
        bench=bench,
        benchmark_runs=benchmark_runs(
            event_name=event.name, bench=bench, detector_succeeded=True
        ),
    )


# -- Then steps ----------------------------------------------------------------


@then("a performance-relevant change is detected")
def then_a_relevant_change_is_detected(decision: Decision, event: Event) -> None:
    """Assert that the filter matched at least one changed path.

    Parameters
    ----------
    decision : Decision
        Filter decision produced by the classification step.
    event : Event
        Event whose paths were classified.
    """
    assert decision.bench, (
        f"the filter must match {list(event.changed_paths)}; a change that can "
        "move measured throughput has to reach the benchmark"
    )


@then("no performance-relevant change is detected")
def then_no_relevant_change_is_detected(decision: Decision, event: Event) -> None:
    """Assert that the filter matched none of the changed paths.

    Parameters
    ----------
    decision : Decision
        Filter decision produced by the classification step.
    event : Event
        Event whose paths were classified.
    """
    assert not decision.bench, (
        f"the filter must not match {list(event.changed_paths)}; paying for a "
        "benchmark that cannot move is the cost this gate exists to avoid"
    )


@then("the benchmark job runs")
def then_the_benchmark_job_runs(decision: Decision, event: Event) -> None:
    """Assert that the benchmark gate admits the event.

    Parameters
    ----------
    decision : Decision
        Benchmark admission decision produced by the classification step.
    event : Event
        Event whose paths were classified.
    """
    assert decision.benchmark_runs, (
        f"a {event.name!r} event changing {list(event.changed_paths)} must run "
        "benchmark-ratchet"
    )


@then("the benchmark job is skipped")
def then_the_benchmark_job_is_skipped(decision: Decision, event: Event) -> None:
    """Assert that the benchmark gate rejects the event.

    Parameters
    ----------
    decision : Decision
        Benchmark admission decision produced by the classification step.
    event : Event
        Event whose paths were classified.
    """
    assert not decision.benchmark_runs, (
        f"a {event.name!r} event changing {list(event.changed_paths)} must skip "
        "benchmark-ratchet"
    )
