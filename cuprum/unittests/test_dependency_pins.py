"""Tests for the pytest upper bound and its documented rationale.

The `dev` dependency group constrains pytest below 9.1 while `pytest-bdd`
8.1.0 registers fixtures through the ``nodeid``/``baseid`` API that pytest 9.1
deprecates, so an unconstrained environment resolves a pytest that emits
`PytestRemovedIn10Warning` from inside the behavioural suite. The constraint is
a *temporary* pin with an expiry condition, which makes it easy to drop by
accident or to widen silently and reintroduce the warning.

These tests bind the constraint to its rationale and to the sites that
document it, so a change to one without the others fails here rather than in
CI:

- the pyproject constraint must still exclude the deprecating release;
- the constraint's comment block must still name the tracking issue;
- the developers' guide must document the same bound and the same link.

`docs/developers-guide.md` records the policy under "Development dependency
pins". The assertions check the bound and the tracking link — the load-bearing
parts — rather than restating the surrounding prose.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from tests.helpers import extract_markdown_subsection
from tests.helpers.docs import repo_root

_DEVELOPERS_GUIDE = "docs/developers-guide.md"
_PIN_SECTION_HEADING = "Development dependency pins"

#: The release that turns pytest-bdd's fixture registration into a warning.
_DEPRECATING_RELEASE = "9.1"

_TRACKING_ISSUE = "https://github.com/pytest-dev/pytest-bdd/issues/823"

#: The warning class the constraint exists to keep out of the suite.
_WARNING_NAME = "PytestRemovedIn10Warning"

_CONSTRAINT_RE = re.compile(r"^pytest(?P<specifier>[<>=!~].*)$")


def _pyproject_text() -> str:
    """Read pyproject.toml as text, to see the constraint's comment block."""
    return (repo_root() / "pyproject.toml").read_text(encoding="utf-8")


def _pytest_constraint() -> str:
    """Read the pytest development-dependency constraint from pyproject.toml.

    Returns
    -------
    str
        The constraint after the package name, for example ``<9.1``.
    """
    pyproject = tomllib.loads(_pyproject_text())
    dev = pyproject.get("dependency-groups", {}).get("dev")
    assert isinstance(dev, list), "pyproject.toml must declare a dev group"
    constraints = [
        match.group("specifier")
        for dependency in dev
        if (match := _CONSTRAINT_RE.match(dependency)) is not None
    ]
    assert len(constraints) == 1, (
        f"expected exactly one pytest constraint in the dev group, "
        f"found {constraints!r}"
    )
    return constraints[0]


def _constraint_comment_block() -> str:
    """Read the comment lines immediately above the pytest constraint.

    Returns
    -------
    str
        The contiguous ``#`` comment block that introduces the constraint.
    """
    lines = _pyproject_text().splitlines()
    index = next(
        position
        for position, line in enumerate(lines)
        if _CONSTRAINT_RE.match(line.strip().strip('",'))
    )
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.lstrip().startswith("#"):
            break
        block.append(line)
    assert block, "the pytest constraint must carry an inline rationale comment"
    return "\n".join(reversed(block))


def _pin_section() -> str:
    """Extract the developers' guide "Development dependency pins" section."""
    text = (repo_root() / _DEVELOPERS_GUIDE).read_text(encoding="utf-8")
    return extract_markdown_subsection(text, heading=_PIN_SECTION_HEADING, level=2)


class TestPytestDependencyPins:
    """The pytest pin, its rationale, and the sites that document them."""

    def test_pytest_constraint_excludes_the_deprecating_release(self) -> None:
        """The dev group must constrain pytest below the release that warns."""
        constraint = _pytest_constraint()
        assert f"<{_DEPRECATING_RELEASE}" in constraint, (
            f"the pytest constraint {constraint!r} no longer excludes "
            f"{_DEPRECATING_RELEASE}, which pytest-bdd 8.1.0 cannot use without "
            f"raising {_WARNING_NAME}"
        )

    def test_pytest_constraint_carries_its_rationale(self) -> None:
        """The constraint's comment block must name the tracking issue."""
        block = _constraint_comment_block()
        assert _TRACKING_ISSUE in block, (
            f"the pytest constraint's comment block must name {_TRACKING_ISSUE} "
            f"so the pin can be lifted when the fix ships; found {block!r}"
        )
        assert _WARNING_NAME in block, (
            f"the comment block must name {_WARNING_NAME}, the warning the "
            f"constraint suppresses; found {block!r}"
        )

    @pytest.mark.parametrize(
        "term",
        [
            f"pytest<{_DEPRECATING_RELEASE}",
            _TRACKING_ISSUE,
            _WARNING_NAME,
            "pytest-bdd",
        ],
    )
    def test_developers_guide_documents_the_pin(self, term: str) -> None:
        """The guide must document the pin, its cause, and how to lift it."""
        section = _pin_section()
        assert term in section, (
            f"{_DEVELOPERS_GUIDE}'s {_PIN_SECTION_HEADING!r} section must "
            f"mention {term!r}"
        )
