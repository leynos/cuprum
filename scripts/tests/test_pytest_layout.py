"""Tests for resolving a pytest layout into the files each process will run.

The two arms of issue #607's first experiment are only comparable if they select
the same tests, so selection is the property these tests defend. Every case
drives a real temporary tree: a selection rule proved against a dictionary would
not tell us whether `Path.glob` and the Make recipe agree about what a pattern
names.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts.pytest_layout import (
    GroupError,
    Layout,
    build_layouts,
    expand,
    is_glob,
    parse_groups,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


def _tree(root: pth.Path, *names: str) -> pth.Path:
    """Create each named file under ``root`` and return the root."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def test_a_literal_entry_selects_only_an_existing_file(tmp_path: pth.Path) -> None:
    """Use a plain path as written, and contribute nothing when it is absent."""
    _tree(tmp_path, "tests/here.py")

    assert expand(("tests/here.py", "tests/absent.py"), tmp_path) == (
        "tests/here.py",
    ), "Use a plain path as written, and contribute nothing when it is absent"


def test_a_glob_entry_expands_in_sorted_order(tmp_path: pth.Path) -> None:
    """Expand sorted, so two layouts and two filesystems agree on order."""
    _tree(tmp_path, "tests/test_c.py", "tests/test_a.py", "tests/test_b.py")

    assert expand(("tests/test_*.py",), tmp_path) == (
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
    ), "Expand sorted, so two layouts and two filesystems agree on order"


def test_a_character_class_glob_is_honoured(tmp_path: pth.Path) -> None:
    """Treat the alphabetical behaviour batches as globs, not literal names.

    `tests/behaviour/test_[a-h]*.py` is one of the three selector entries whose
    provenance this experiment traced to a resource workaround. Reading it as a
    literal path would select nothing and quietly drop 26 modules.
    """
    _tree(
        tmp_path,
        "behaviour/test_alpha.py",
        "behaviour/test_indigo.py",
        "behaviour/test_zulu.py",
    )

    assert expand(("behaviour/test_[a-h]*.py",), tmp_path) == (
        "behaviour/test_alpha.py",
    ), "Treat the alphabetical behaviour batches as globs, not literal names"


def test_a_glob_matching_nothing_contributes_nothing(tmp_path: pth.Path) -> None:
    """Skip an unmatched entry, as the Make loop's `[ -e ]` test does."""
    assert not expand(("tests/test_*.py",), tmp_path), (
        "Skip an unmatched entry, as the Make loop's `[ -e ]` test does"
    )


def test_the_same_file_named_twice_in_one_group_is_refused(
    tmp_path: pth.Path,
) -> None:
    """Refuse a group that would run one test twice.

    The shell loop would pass the file twice and pytest would run it twice, so
    a layout that did the same would report a longer suite than the baseline
    while selecting the same tests.
    """
    _tree(tmp_path, "tests/test_a.py")

    with pytest.raises(GroupError, match="already selects"):
        expand(("tests/test_a.py", "tests/test_*.py"), tmp_path)


def test_a_group_is_deduplicated_across_its_own_entries(
    tmp_path: pth.Path,
) -> None:
    """Refuse rather than deduplicate, so the ambiguity is visible."""
    _tree(tmp_path, "tests/test_a.py", "tests/test_b.py")

    with pytest.raises(GroupError) as raised:
        expand(("tests/test_*.py", "tests/test_b.py"), tmp_path)
    assert raised.value.entry == "tests/test_b.py", (
        "Refuse rather than deduplicate, so the ambiguity is visible"
    )


def test_is_glob_recognizes_every_metacharacter() -> None:
    """Classify star, question mark and bracket as globs, and dots as literals."""
    assert is_glob("tests/test_*.py"), (
        "Classify star, question mark and bracket as globs, and dots as literals"
    )
    assert is_glob("tests/test_?.py"), (
        "Classify star, question mark and bracket as globs, and dots as literals"
    )
    assert is_glob("tests/test_[abc].py"), (
        "Classify star, question mark and bracket as globs, and dots as literals"
    )
    assert not is_glob("tests/test_a.py"), (
        "Classify star, question mark and bracket as globs, and dots as literals"
    )


def test_a_layout_expands_each_group_independently(tmp_path: pth.Path) -> None:
    """Keep group boundaries, so the number of processes stays observable."""
    _tree(tmp_path, "a/test_one.py", "b/test_two.py")
    layout = Layout("demo", (("a/test_*.py",), ("b/test_*.py",)))

    assert layout.expanded(tmp_path) == (("a/test_one.py",), ("b/test_two.py",)), (
        "Keep group boundaries, so the number of processes stays observable"
    )


def test_an_empty_group_is_reported_not_dropped(tmp_path: pth.Path) -> None:
    """Report which groups selected nothing, so a lost selector is visible."""
    _tree(tmp_path, "a/test_one.py")
    layout = Layout("demo", (("a/test_*.py",), ("missing/test_*.py",)))

    assert layout.empty_groups(tmp_path) == (1,), (
        "Report which groups selected nothing, so a lost selector is visible"
    )


def test_a_layout_reports_its_files_in_group_order(tmp_path: pth.Path) -> None:
    """Preserve declaration order across groups for a readable record."""
    _tree(tmp_path, "a/test_one.py", "b/test_two.py")
    layout = Layout("demo", (("b/test_*.py",), ("a/test_*.py",)))

    assert layout.files(tmp_path) == ("b/test_two.py", "a/test_one.py"), (
        "Preserve declaration order across groups for a readable record"
    )


def test_parse_groups_splits_on_pipe_and_plus() -> None:
    """Read the Makefile's rendering of a layout."""
    assert parse_groups("a+b|c") == (("a", "b"), ("c",)), (
        "Read the Makefile's rendering of a layout"
    )


def test_parse_groups_keeps_a_blank_group() -> None:
    """Keep a blank group as empty, so a caller reports it rather than loses it."""
    assert parse_groups("a||c") == (("a",), (), ("c",)), (
        "Keep a blank group as empty, so a caller reports it rather than loses it"
    )


def test_parse_groups_refuses_to_invent_groups_from_nothing() -> None:
    """Yield one empty group for an empty specification.

    A caller that lost its selector then runs one empty group and fails loudly,
    rather than iterating over no groups and reporting success.
    """
    assert parse_groups("") == ((),), "Yield one empty group for an empty specification"


def test_baseline_is_always_available() -> None:
    """Keep the rollback layout present even when no candidate exists."""
    layouts = build_layouts((("a",),))

    assert set(layouts) == {"baseline"}, (
        "Keep the rollback layout present even when no candidate exists"
    )
    assert layouts["baseline"].name == "baseline", (
        "Keep the rollback layout present even when no candidate exists"
    )


def test_a_consolidated_layout_is_selectable_only_when_supplied() -> None:
    """Add the candidate on request, and never by default."""
    layouts = build_layouts((("a",), ("b",)), (("a", "b"),))

    assert set(layouts) == {"baseline", "consolidated"}, (
        "Add the candidate on request, and never by default"
    )
    assert layouts["consolidated"].groups == (("a", "b"),), (
        "Add the candidate on request, and never by default"
    )
