"""Resolve a named pytest layout into the files each process will run.

Issue #607 compares two ways of grouping the default suite: the current one
process per selector entry, and a consolidated one with fewer processes. Both
must select exactly the same files, so both are expressed as a *layout* - a list
of groups, each a list of selector entries - and resolved here.

This module answers only "which files does this group name?". Running them is
`scripts/pytest_batches.py`. The split keeps the selection rules testable
without a subprocess, and keeps a claim about the selector from being tangled
with a claim about process cost.

The rules reproduce the Make recipe's shell loop deliberately, because a
comparison whose two arms select different tests measures nothing:

- an entry containing a glob metacharacter is expanded by sorted globbing;
- a plain path is used as written, and contributes nothing when absent;
- an entry that selects nothing contributes nothing, and the group is reported
  as empty rather than silently dropped;
- ordering is sorted, so two runs and two layouts agree.

One rule is stricter than the shell loop. The loop would pass the same file
twice if two entries named it, and pytest would run it twice. Refusing that is
a correctness improvement for a layout, and it cannot change what the baseline
selects because the baseline's entries are disjoint by construction.
"""

from __future__ import annotations

import pathlib as pth
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("GroupError", "Layout", "build_layouts", "expand", "parse_groups")

#: The selector entries are relative to the repository root, which is where the
#: Make recipe runs. Resolution defaults to the working directory so a caller
#: standing anywhere in the tree behaves as `make` would.
DEFAULT_ROOT = pth.Path.cwd()

#: Characters that make an entry a glob rather than a literal path. `[` is
#: included because the alphabetical behaviour batches rely on it.
_GLOB_CHARACTERS = frozenset("*?[")

#: Prefix marking an entry as a *deselection* rather than a selection. No path
#: in this repository begins with a hyphen, so the two cannot be confused, and
#: the Make recipe's `--deselect` becomes part of the layout it belongs to
#: instead of a flag the driver would have to apply to every command.
DESELECT_PREFIX = "-"


class GroupError(ValueError):
    """A group that cannot be run as written.

    Attributes
    ----------
    entry : str
        The offending entry.
    reason : str
        Why the entry is unusable, phrased for the person who wrote it.
    """

    def __init__(self, entry: str, reason: str) -> None:
        """Record the entry and the reason it was refused."""
        super().__init__(f"{entry!r}: {reason}")
        self.entry = entry
        self.reason = reason


def is_glob(entry: str) -> bool:
    """Return whether an entry is a shell glob rather than a literal path."""
    return bool(_GLOB_CHARACTERS.intersection(entry))


def expand(
    entries: cabc.Sequence[str], root: pth.Path | None = None
) -> tuple[str, ...]:
    """Expand selector entries to repository-relative files.

    Parameters
    ----------
    entries : Sequence of str
        Selector entries, as `PYTEST_TARGETS` spells them.
    root : pathlib.Path or None
        Directory the entries are relative to. Defaults to the working
        directory, matching where the Make recipe runs.

    Returns
    -------
    tuple of str
        Selected files, relative to ``root``, sorted and deduplicated.

    Raises
    ------
    GroupError
        If two entries in one group select the same file. The shell loop would
        run that file twice; a layout must not, so the ambiguity is reported
        rather than resolved by guessing.
    """
    base = DEFAULT_ROOT if root is None else root
    selected: dict[str, str] = {}
    deselected: set[str] = set()
    for entry in entries:
        if entry.startswith(DESELECT_PREFIX):
            deselected.update(_expand_one(entry[len(DESELECT_PREFIX) :], base))
            continue
        for relative in _expand_one(entry, base):
            if relative in selected:
                raise GroupError(
                    entry,
                    f"selects {relative}, which {selected[relative]!r} already "
                    "selects; one group may name a file only once",
                )
            selected[relative] = entry
    return tuple(sorted(set(selected) - deselected))


def _expand_one(entry: str, base: pth.Path) -> tuple[str, ...]:
    """Expand one entry, with or without metacharacters, to relative paths."""
    if is_glob(entry):
        paths = [path for path in sorted(base.glob(entry)) if path.is_file()]
    else:
        candidate = base / entry
        paths = [candidate] if candidate.is_file() else []
    return tuple(path.relative_to(base).as_posix() for path in paths)


class Layout:
    """A named grouping of the suite into pytest processes.

    Parameters
    ----------
    name : str
        The layout's name, as a caller selects it.
    groups : Sequence of Sequence of str
        Each group is a list of selector entries.
    """

    def __init__(self, name: str, groups: cabc.Sequence[cabc.Sequence[str]]) -> None:
        """Hold the layout's name and its groups, in declaration order."""
        self.name = name
        self.groups = tuple(tuple(group) for group in groups)

    def expanded(self, root: pth.Path | None = None) -> tuple[tuple[str, ...], ...]:
        """Expand every group to the files it selects.

        Parameters
        ----------
        root : pathlib.Path or None
            Directory the entries are relative to.

        Returns
        -------
        tuple of tuple of str
            One tuple per declared group. A group that selects nothing appears
            as an empty tuple, so a caller can report it rather than lose it.
        """
        return tuple(expand(group, root) for group in self.groups)

    def files(self, root: pth.Path | None = None) -> tuple[str, ...]:
        """Return every file the layout selects, in group order.

        Parameters
        ----------
        root : pathlib.Path or None
            Directory the entries are relative to.

        Returns
        -------
        tuple of str
            Files in the order the groups name them, with repeats preserved so
            a duplicate between groups stays visible to a fidelity check.
        """
        return tuple(name for group in self.expanded(root) for name in group)

    def empty_groups(self, root: pth.Path | None = None) -> tuple[int, ...]:
        """Return the indices of groups that selected no file.

        Parameters
        ----------
        root : pathlib.Path or None
            Directory the entries are relative to.

        Returns
        -------
        tuple of int
            Zero-based indices, so a caller can name the group it skipped.
        """
        return tuple(
            index for index, group in enumerate(self.expanded(root)) if not group
        )


def parse_groups(text: str) -> tuple[tuple[str, ...], ...]:
    """Parse a ``|``-joined, ``+``-joined group specification.

    The Makefile owns the selector, so it renders the layout as one string
    rather than the driver re-reading and re-interpreting the Makefile. `|`
    separates groups and `+` separates entries within a group; both were chosen
    because neither appears in a path in this repository.

    Parameters
    ----------
    text : str
        The specification.

    Returns
    -------
    tuple of tuple of str
        One tuple of entries per group, in declaration order. An empty
        specification yields one empty group rather than no groups, so a caller
        that lost its selector fails loudly instead of running nothing quietly.
    """
    return tuple(
        tuple(entry for entry in group.split("+") if entry.strip())
        for group in text.split("|")
    )


def build_layouts(
    baseline: cabc.Sequence[cabc.Sequence[str]],
    consolidated: cabc.Sequence[cabc.Sequence[str]] | None = None,
) -> dict[str, Layout]:
    """Return the selectable layouts, by name.

    Parameters
    ----------
    baseline : Sequence of Sequence of str
        The groups that reproduce the Make recipe's own loop.
    consolidated : Sequence of Sequence of str or None
        The candidate grouping, or ``None`` when no candidate exists.

    Returns
    -------
    dict
        Layout names mapped to layouts. ``baseline`` is always present, so a
        rollback is one variable and never depends on the experiment's wiring.
    """
    layouts = {"baseline": Layout("baseline", baseline)}
    if consolidated is not None:
        layouts["consolidated"] = Layout("consolidated", consolidated)
    return layouts
