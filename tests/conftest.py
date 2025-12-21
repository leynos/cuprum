"""Pytest configuration for the behavioural test suite.

Pytest does not collect Gherkin ``.feature`` files by default. Our behavioural
tests are defined in Python and *reference* these feature files via pytest-bdd,
but callers (and CI scripts) may still select the ``.feature`` file directly on
the command line. This collector makes such selections work without changing
pytest-bdd semantics.
"""

from __future__ import annotations

import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from pathlib import Path


class FeatureFile(pytest.File):
    """Collect a ``.feature`` file as a lightweight validation test."""

    def collect(self) -> list[pytest.Item]:
        """Return a single item that checks the feature file is readable."""
        return [FeatureFileItem.from_parent(self, name=self.path.name)]


class FeatureFileItem(pytest.Item):
    """A minimal test item for ``.feature`` file selections."""

    def runtest(self) -> None:
        """Ensure the feature file looks like a Gherkin feature."""
        content = self.path.read_text(encoding="utf-8")
        if "Feature:" not in content:
            msg = f"{self.path} does not look like a Gherkin feature file."
            raise AssertionError(msg)


def pytest_collect_file(
    file_path: Path,
    parent: pytest.Collector,
) -> FeatureFile | None:
    """Collect ``.feature`` files so they can be selected on the CLI."""
    if file_path.suffix != ".feature":
        return None
    return FeatureFile.from_parent(parent, path=file_path)
