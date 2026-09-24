"""Dependabot directory globs and the composite actions they must reach.

Dependabot does not descend from ``/`` into ``.github/actions``, so the GitHub
Actions stanza has to name the composite actions with a directory glob. These
helpers find every composite action in the repository and decide whether a
glob covers a directory the way Dependabot does: ``*`` stays within one path
segment and ``**`` spans segments.

Only ``tests/test_ci_dependabot_config.py`` and this module's own tests use
these helpers; they read the repository and never parse workflows.
"""

from __future__ import annotations

import re

from tests.helpers.ci_workflows import ROOT

#: The glob covering every composite action one level below ``.github/actions``.
COMPOSITE_ACTIONS = "/.github/actions/*"

#: File names that make a directory a composite action.
ACTION_MANIFESTS = frozenset({"action.yml", "action.yaml"})


def composite_action_directories() -> tuple[str, ...]:
    """Return every composite action directory, at any depth, in Dependabot form.

    Returns
    -------
    tuple[str, ...]
        Sorted directories such as ``/.github/actions/cache-keys``.
    """
    return tuple(
        sorted(
            "/" + manifest.parent.relative_to(ROOT).as_posix()
            for manifest in (ROOT / ".github" / "actions").rglob("action.y*ml")
            if manifest.name in ACTION_MANIFESTS
        )
    )


def directory_glob_matches(glob: str, directory: str) -> bool:
    """Return whether a Dependabot directory glob covers ``directory``.

    Returns
    -------
    bool
        ``True`` when ``glob`` matches, with ``*`` confined to one segment and
        ``**`` spanning segments.

    Examples
    --------
    >>> directory_glob_matches("/.github/actions/*", "/.github/actions/lint")
    True
    >>> directory_glob_matches("/.github/actions/*", "/.github/actions/a/b")
    False
    """
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", glob)
    )
    return re.fullmatch(regex, directory) is not None
