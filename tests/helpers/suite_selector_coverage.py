"""Validate the split selector route used by the Python suite target."""

from __future__ import annotations

from tests.helpers.ci_documents import require
from tests.helpers.grouped_recipe_flow import require_grouped_selector_drives_pytest
from tests.helpers.makefile_references import variables_referencing
from tests.helpers.recipe_flow import (
    require_selector_drives_pytest,
    require_selector_is_consumed,
)


def require_test_python_selector_coverage(recipe: str, selector: str) -> None:
    """Require the individual and grouped branches to cover one selector.

    The individual alias filters out grouped modules. Lineage alone cannot
    prove coverage, so this checks that the individual loop and grouped
    intersection both reach pytest through their respective branches.

    Parameters
    ----------
    recipe : str
        Recipe text returned by :func:`tests.helpers.makefile.recipe_of`.
    selector : str
        Source Make variable whose selected modules must reach pytest.

    Raises
    ------
    AssertionError
        If either branch has no lineage from the selector or fails to pass its
        selected arguments to pytest.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require helpers.
    aliases = variables_referencing(selector)
    required_aliases = {
        "PYTEST_INDIVIDUAL_TARGETS",
        "PYTEST_SELECTED_TARGETS",
    }
    require(
        condition=required_aliases <= aliases,
        message=(
            f"the `test-python` route must derive its individual and grouped "
            f"branches from {selector}"
        ),
    )
    require_selector_drives_pytest(
        recipe,
        selector="PYTEST_INDIVIDUAL_TARGETS",
        pytest_variable="PYTEST",
    )
    require_selector_is_consumed(recipe, selector="PYTEST_GROUPED_DESELECT")
    require_grouped_selector_drives_pytest(
        recipe,
        grouped_targets="PYTEST_GROUPED_TARGETS",
        selected_targets="PYTEST_SELECTED_TARGETS",
        pytest_variable="PYTEST",
    )
