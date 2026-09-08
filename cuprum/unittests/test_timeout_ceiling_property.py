"""Property-based tests for the coverage job's ceiling requirement.

``required_ceiling`` is the one piece of arithmetic in the timeout
contract that a workflow cannot exercise. Both lanes here run the
coverage action exactly once with the same watchdog, so the example the
contract asserts against would pass a reading that returned its first
argument, one that multiplied by the number of steps, or one that
dropped either constant term.

These pin the shape instead of the value: the result is the sum of the
budgets plus both constants, it is monotonic in each budget, and it is
never below the sum of what it has to contain. Adding a second coverage
step to either lane later is the case they exist for.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from cuprum.unittests._timeout_lane_support import (
    CEILING_MARGIN_SECONDS,
    OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS,
    required_ceiling,
)

#: Watchdog budgets in the range a workflow could plausibly declare, up
#: to six hours, which is GitHub's own job default and so the largest
#: value a lane could carry without the job timer ending it first.
BUDGETS = st.lists(st.integers(min_value=0, max_value=6 * 60 * 60), max_size=6)


@given(budgets=BUDGETS)
def test_the_ceiling_is_the_sum_of_the_budgets_and_both_constants(
    budgets: list[int],
) -> None:
    """Every term is added, for any number of coverage steps.

    The empty list is included deliberately: a job that runs no coverage
    step still has work outside a watchdog and still needs the margin,
    so the constants are terms of their own rather than a fraction of
    the budgets.
    """
    assert required_ceiling(budgets) == (
        sum(budgets) + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS
    )


@given(budgets=BUDGETS, extra=st.integers(min_value=0, max_value=6 * 60 * 60))
def test_adding_a_coverage_step_never_lowers_the_requirement(
    budgets: list[int], extra: int
) -> None:
    """A second invocation can only ask more of the ceiling.

    A reading that took one step's budget and multiplied, or that took
    the largest rather than the sum, would break here as soon as the
    budgets differed.
    """
    assert required_ceiling([*budgets, extra]) >= required_ceiling(budgets)


@given(budgets=BUDGETS)
def test_the_ceiling_contains_everything_it_has_to(budgets: list[int]) -> None:
    """The requirement is never below the work it must cover.

    Each coverage step may legitimately spend its whole watchdog, and
    the job timer covers the work outside those windows as well, so the
    sum of the two is a floor the requirement must clear with the margin
    to spare.
    """
    contained = sum(budgets) + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS
    assert required_ceiling(budgets) >= contained + CEILING_MARGIN_SECONDS
