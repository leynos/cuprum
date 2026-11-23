"""Contract tests for the per-module process-boundary classification.

Issue #607 asks whether the default suite's 31 pytest processes are all
necessary. Answering it needs a recorded distinction between the batches that
exist because a module really builds something and the batches that exist
because splitting once kept a constrained machine responsive. That distinction
is data in `tests/helpers/suite_classification.py`; these tests hold the data
to the claims it makes, so a stale or invented entry fails rather than quietly
protecting nothing.

The tests read the same selector the suite reads. A classified module that no
longer belongs to the default suite is a classification that guards nothing.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.suite_classification import (
    COMPILER_INVOKING_MODULES,
    FRESH_PROCESS_MODULES,
    require_classification_is_well_formed,
    require_modules_exist,
)
from tests.helpers.suite_selection import selected_paths, variable_expansion

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Everything the default suite collects, as repository-relative paths.
DEFAULT_SUITE: frozenset[str] = frozenset(
    str(path) for path in selected_paths(variable_expansion("PYTEST_TARGETS"))
)


def _classified() -> cabc.Iterator[tuple[str, str, str]]:
    """Yield each ``(mapping name, module, reason)`` triple."""
    for name, mapping in (
        ("COMPILER_INVOKING_MODULES", COMPILER_INVOKING_MODULES),
        ("FRESH_PROCESS_MODULES", FRESH_PROCESS_MODULES),
    ):
        for module, reason in mapping.items():
            yield name, module, reason


def test_the_classification_is_consistent() -> None:
    """Require both mappings to be non-empty and mutually consistent."""
    require_classification_is_well_formed(
        COMPILER_INVOKING_MODULES,
        FRESH_PROCESS_MODULES,
    )


def test_every_classified_module_exists() -> None:
    """Require each classified path to name a real file.

    A rename leaves a classification describing nothing, and a guard that
    consults it would stop protecting the module it was written for.
    """
    require_modules_exist(module for _, module, _ in _classified())


def test_every_classified_module_is_in_the_default_suite() -> None:
    """Require each classified module to be collected by `PYTEST_TARGETS`.

    Classifying a module the default suite never runs would add a boundary to
    a process this experiment cannot affect, and would let a reader believe
    the classification covered a module it does not.
    """
    for name, module, _reason in _classified():
        assert module in DEFAULT_SUITE, (
            f"{name} classifies {module}, which PYTEST_TARGETS does not "
            "collect. Either add the module to the selector or delete the entry."
        )


def test_the_default_suite_population_is_not_empty() -> None:
    """Refuse a selector that resolved to nothing.

    Every assertion above is vacuously true over an empty population, so the
    resolver is reported as the failure rather than the modules it failed to
    find.
    """
    assert DEFAULT_SUITE, (
        "PYTEST_TARGETS resolved to no files, so every classification guard "
        "would pass without reading anything"
    )


@pytest.mark.parametrize(
    ("compiler", "fresh"),
    [
        pytest.param({}, {}, id="both-classes-empty"),
        pytest.param({"tests/nope.py": "x"}, {}, id="fresh-class-empty"),
        pytest.param({"tests/nope.py": " "}, {"tests/nope.py": "x"}, id="blank-reason"),
        pytest.param(
            {"tests/nope.py": "x"},
            {"tests/other.py": "x"},
            id="compiler-module-not-in-fresh-class",
        ),
    ],
)
def test_the_consistency_check_rejects_a_broken_classification(
    compiler: dict[str, str],
    fresh: dict[str, str],
) -> None:
    """Prove the consistency check can fail, so its pass means something.

    Each case is one way the real data could rot: an empty class, a reason
    that says nothing, and a compiler module that was not also marked as
    requiring its own process. Without these the green run over the real data
    would be indistinguishable from a check that never rejects anything.
    """
    with pytest.raises(AssertionError):
        require_classification_is_well_formed(compiler, fresh)


def test_the_existence_check_rejects_a_missing_module() -> None:
    """Prove the existence check fails for a path that is not on disk."""
    with pytest.raises(AssertionError, match="no such file exists"):
        require_modules_exist(["tests/test_this_module_does_not_exist.py"])
