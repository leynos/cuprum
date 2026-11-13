"""Contract tests for the spelling gate and the lint target's sequencing.

These checks used to live beside the Skylos Makefile contract. The Skylos and
duplication gates are now installed commands tested in df12-python-lints, but
the spelling-builder pin and the lint target's leaf sequencing are Cuprum's own
policy, so they stay here.
"""

from __future__ import annotations

import re

from tests.helpers.docs import repo_root

_TYPOS_BUILDER_REPOSITORY = (
    "git+https://github.com/leynos/typos-config-builder.git"
    "@$(TYPOS_CONFIG_BUILDER_VERSION)"
)
_VERSION_PIN = re.compile(r"^TYPOS_CONFIG_BUILDER_VERSION \?= v0\.1\.3$", re.MULTILINE)
_BUILDER = re.compile(
    r"^TYPOS_CONFIG_BUILDER = (.*?)\n(?!\s)", re.MULTILINE | re.DOTALL
)
_SPELLING_RECIPE = re.compile(
    r"^spelling:.*\n\t\$\(TYPOS_CONFIG_BUILDER\) gate --repository \. --scope all$",
    re.MULTILINE,
)
_LINT_RULE = re.compile(r"^lint:([^\n#]*)(?:##.*)?\n(\t.*\n)?", re.MULTILINE)


def _makefile() -> str:
    """Return the Makefile text.

    Returns
    -------
    str
        The repository Makefile.
    """
    return (repo_root() / "Makefile").read_text(encoding="utf-8")


def test_spelling_gate_uses_the_pinned_config_builder() -> None:
    """The spelling gate must use the pinned policy builder on Python 3.14."""
    text = _makefile()
    assert _VERSION_PIN.search(text), (
        "Spelling-builder version contract must retain the pinned release"
    )
    builder = _BUILDER.search(text)
    assert builder is not None, "the Makefile must define TYPOS_CONFIG_BUILDER"
    command = " ".join(builder.group(1).replace("\\\n", " ").split())
    assert "--python 3.14" in command, (
        "Spelling-builder command contract must retain the Python 3.14 CLI"
    )
    assert _TYPOS_BUILDER_REPOSITORY in command, (
        "Spelling-builder command contract must retain the pinned source"
    )
    assert _SPELLING_RECIPE.search(text), (
        "Spelling gate contract must invoke the builder for the full repository"
    )


def test_lint_sequences_its_leaves_as_prerequisites() -> None:
    """``lint`` must run its leaves as prerequisites in a single Make process."""
    match = _LINT_RULE.search(_makefile())
    assert match is not None, "the Makefile must define the lint target"
    assert match.group(1).split() == [
        "python-lint",
        "rust-lint",
        "github-actions-lint",
    ], "lint must sequence its leaves as prerequisites"
    assert match.group(2) is None, (
        "lint must delegate to its leaves as prerequisites, not recursive sub-makes"
    )
