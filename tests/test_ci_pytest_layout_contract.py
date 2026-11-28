"""Contract tests for the opt-in pytest layout runner.

Issue #607 compares pytest *groupings*, so the one thing a layout must never do
is change what runs. The baseline layout is the Make recipe written as data, and
these tests hold it to that claim: it must select exactly what `test-python`
selects, it must keep every compiler-invoking module out of a merged group, and
it must leave the default target untouched.

The tests read the Makefile the way `make` does, through `tests/helpers`, rather
than pasting the expected file list. A pasted list would agree with the driver
on the day it was written and disagree silently afterwards.
"""

from __future__ import annotations

import os
import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - run the real recipe.
import sys
import typing as typ

import pytest

from scripts.pytest_layout import Layout, Selection, expand_group, parse_groups
from tests.helpers.makefile import variable_expansion
from tests.helpers.suite_classification import (
    COMPILER_INVOKING_MODULES,
    FRESH_PROCESS_MODULES,
    require_merge_candidates_are_declared,
)
from tests.helpers.suite_selection import (
    SCENARIO_SELECTOR,
    SELECTOR,
    selected_paths,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The layout the experiment compares against, and the candidate's name. A
#: mismatch here would let a typo in the workflow run the baseline while
#: reporting it as the candidate.
BASELINE = "baseline"
CONSOLIDATED = "consolidated"

#: The target that must keep its original recipe. The layout route is opt-in,
#: so nothing a contributor runs by default may change.
DEFAULT_TARGET = "test-python"
OPT_IN_TARGET = "test-python-layout"


def _baseline_layout(root: pth.Path) -> Layout:
    """Return the baseline layout the Makefile renders."""
    rendered = os.environ.get("CUPRUM_PYTEST_LAYOUT_BASELINE") or _make_value(
        "PYTEST_LAYOUT_BASELINE"
    )
    return Layout(BASELINE, parse_groups(rendered))


def _make_value(name: str) -> str:
    """Return one Make variable's expanded value, rendered by `make` itself."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [  # ruff: ignore[start-process-with-partial-path] - make is on PATH.
            "make",
            "--no-print-directory",
            "-s",
            "--eval",
            f"show: ; @printf '%s' '$({name})'",
            "show",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_default_target_still_uses_the_shell_loop() -> None:
    """Keep `test-python` unchanged until an adoption decision says otherwise.

    The layout route exists so a candidate can be measured on the same commit
    as its control. Making it the default before the evidence exists would
    change every contributor's run on the strength of an unmeasured hypothesis.
    """
    assert _recipe_mentions("for pattern in"), (
        "test-python must keep its shell loop until the experiment is adopted"
    )


def _recipe_mentions(needle: str) -> bool:
    """Return whether the default target's recipe mentions ``needle``."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [  # ruff: ignore[start-process-with-partial-path] - make is on PATH.
            "make",
            "-n",
            "--no-print-directory",
            DEFAULT_TARGET,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return needle in completed.stdout


def test_the_baseline_layout_selects_exactly_what_the_selector_selects() -> None:
    """Hold the baseline to the selector's own file list, once each.

    This is the fidelity claim the whole comparison rests on. A baseline that
    selected a file twice would report a slower run and a longer suite at the
    same time, and neither difference would be the change under test.
    """
    layout = _baseline_layout(pth.Path.cwd())
    selected = selected_paths(variable_expansion(SELECTOR))
    expected = sorted(str(path) for path in selected)

    files = list(layout.files())

    assert sorted(files) == expected, (
        "the baseline layout must select the same files as PYTEST_TARGETS"
    )
    assert len(files) == len(set(files)), (
        "the baseline layout must select each file exactly once; "
        f"repeated: {_repeats(files)}"
    )


def _repeats(names: cabc.Sequence[str]) -> list[str]:
    """Return the names that appear more than once."""
    seen: set[str] = set()
    repeated: list[str] = []
    for name in names:
        if name in seen:
            repeated.append(name)
        seen.add(name)
    return repeated


def test_the_baseline_layout_has_one_group_per_individual_target() -> None:
    """Keep a process per selector entry, plus the one grouped command.

    Counting processes is the measurement, so the baseline must have the same
    number the recipe starts; otherwise the comparison has a different control.
    """
    layout = _baseline_layout(pth.Path.cwd())
    individual = _make_value("PYTEST_INDIVIDUAL_TARGETS").split()
    grouped = _make_value("PYTEST_GROUPED_TARGETS").split()

    assert len(layout.groups) == len(individual) + (1 if grouped else 0), (
        f"expected {len(individual)} individual groups plus the grouped command, "
        f"got {len(layout.groups)}"
    )


def test_no_group_names_a_file_twice() -> None:
    """Refuse an overlapping group, which would run one test twice."""
    layout = _baseline_layout(pth.Path.cwd())
    for index, group in enumerate(layout.expanded()):
        assert len(group) == len(set(group)), (
            f"group {index} selects a file twice: {_repeats(group)}"
        )


def test_the_scenario_suite_stays_out_of_every_layout() -> None:
    """Keep container-bound scenarios out of a job that has no runtime.

    `ACT_SCENARIO_TARGETS` is deliberately outside `PYTEST_TARGETS`; a layout
    that reached them would make the default suite require a container.
    """
    layout = _baseline_layout(pth.Path.cwd())
    scenarios = {
        str(path) for path in selected_paths(variable_expansion(SCENARIO_SELECTOR))
    }

    assert not scenarios.intersection(layout.files()), (
        "a layout must not collect the act scenario suite"
    )


def test_compiler_modules_are_never_merged_into_a_multi_entry_group() -> None:
    """Keep each build-invoking module out of a merged candidate group.

    A compiler-invoking module's session fixtures build artefacts whose purpose
    is a real build, so merging it would let one batch reuse another's build
    and quietly stop testing a cold build.
    """
    merged = _consolidated_selections()
    for selection in merged:
        overlap = set(selection.files).intersection(COMPILER_INVOKING_MODULES)
        assert not overlap or len(selection.files) == 1, (
            "a compiler-invoking module is merged with others: "
            f"{sorted(overlap)} in a group of {len(selection.files)} files"
        )


def test_fresh_process_modules_are_never_merged() -> None:
    """Hold the stricter class to the same rule, with its own message."""
    merged = _consolidated_selections()
    for selection in merged:
        overlap = set(selection.files).intersection(FRESH_PROCESS_MODULES)
        assert not overlap or len(selection.files) == 1, (
            "a fresh-process module is merged with others: "
            f"{sorted(overlap)} in a group of {len(selection.files)} files"
        )


def _consolidated_selections() -> tuple[Selection, ...]:
    """Return the declared candidate groups, expanded to the files they select.

    The check must run on expanded files, not on the literal entries. A merged
    entry such as `cuprum/unittests/test_*.py` would never equal a classified
    path, so comparing entries would miss a compiler module hidden inside a
    glob.

    Returns
    -------
    tuple of Selection
        One per declared candidate group, or empty when no candidate exists.
    """
    rendered = os.environ.get("CUPRUM_PYTEST_LAYOUT_GROUPS") or _make_value(
        "PYTEST_LAYOUT_GROUPS"
    )
    if not rendered.strip():
        return ()
    return tuple(expand_group(group) for group in parse_groups(rendered))


def test_every_merged_group_is_explicitly_cleared() -> None:
    """Require each merged file to be declared, not merely unclassified.

    Absence from the compiler classification is an unknown boundary, so a
    merged group may contain only modules a reviewer has cleared.
    """
    for selection in _consolidated_selections():
        if len(selection.files) > 1:
            require_merge_candidates_are_declared(selection.files)


def test_the_merge_candidate_rule_rejects_an_undeclared_module() -> None:
    """Prove the rule rejects, so an empty allow-list is a real barrier."""
    with pytest.raises(AssertionError, match="audited boundary"):
        require_merge_candidates_are_declared([min(FRESH_PROCESS_MODULES)])


def test_the_merge_guard_rejects_a_glob_that_hides_a_compiler_module() -> None:
    """Prove the merge guard can fail on a hidden module.

    Its pass over an empty manifest would otherwise mean nothing.

    With no candidate declared the guards above iterate nothing. This case
    drives the same expansion with a group that hides a classified module
    inside a glob, which is exactly the shape the literal-entry comparison
    would have missed.
    """
    compiler = min(COMPILER_INVOKING_MODULES)
    directory = compiler.rsplit("/", 1)[0]
    hidden = expand_group((f"{directory}/test_*.py",))

    overlap = set(hidden.files).intersection(COMPILER_INVOKING_MODULES)

    assert overlap, (
        f"{directory}/test_*.py must expand to include the classified module "
        f"{compiler}, so the guard is exercised rather than vacuous"
    )


def test_a_failing_driver_reaches_make_with_a_nonzero_status() -> None:
    """Execute the opt-in recipe with a failing runner and observe the status.

    Printing the command proves the recipe names the driver; it does not prove
    Make sees a failure. This runs the rendered recipe body with a stub that
    exits 7 and asserts the status survives, which is the property a green CI
    candidate depends on.
    """
    stub = _stub_runner()
    makefile = _opt_in_makefile(stub)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [  # ruff: ignore[start-process-with-partial-path] - make is on PATH.
            "make",
            "--no-print-directory",
            "-f",
            str(makefile),
            OPT_IN_TARGET,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(pth.Path.cwd()),
    )

    assert completed.returncode != 0, (
        "a failing driver must fail the Make run, got a zero status"
    )
    assert "Error 7" in completed.stderr, (
        "Make must report the driver's own status, got: "
        f"{completed.stderr.strip()[-200:]}"
    )


def _opt_in_makefile(stub: str) -> pth.Path:
    """Write a Makefile whose opt-in target runs the real recipe.

    The recipe text is the one make renders for the real target, so this
    executes the shipping command under Make rather than a copy of it.

    Parameters
    ----------
    stub : str
        Path to the failing runner the recipe should invoke.

    Returns
    -------
    pathlib.Path
        A temporary Makefile defining only the opt-in target.
    """
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [  # ruff: ignore[start-process-with-partial-path] - make is on PATH.
            "make",
            "--no-print-directory",
            "-n",
            OPT_IN_TARGET,
            f"PYTEST_LAYOUT_RUNNER={stub}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [
        line
        for line in completed.stdout.replace("\\\n", " ").splitlines()
        if line.strip()
    ]
    assert lines, f"the rendered recipe must not be empty, got: {completed.stdout}"
    directory = pth.Path(_stub_directory())
    makefile = directory / "Makefile"
    recipe = "".join(f"\t{line}\n" for line in lines)
    makefile.write_text(f"{OPT_IN_TARGET}:\n{recipe}", encoding="utf-8")
    assert "stub_runner.py" in makefile.read_text(encoding="utf-8"), (
        "the generated Makefile must invoke the stub runner"
    )
    return makefile


def _stub_runner() -> str:
    """Return a stub runner path that exits nonzero.

    The stub exists so the black-box test can prove the recipe propagates a
    status without running the suite. It is written outside the repository so
    it cannot enter a format gate.

    Returns
    -------
    str
        Path to a script that prints a marker and exits 7.
    """
    stub = pth.Path(_stub_directory()) / "stub_runner.py"
    stub.write_text(
        "import sys\n\nprint('stub runner invoked')\nsys.exit(7)\n",
        encoding="utf-8",
    )
    return str(stub)


def _stub_directory() -> str:
    """Return a scratch directory for black-box stubs.

    Returns
    -------
    str
        A fresh directory outside the repository, so a stub cannot enter a
        format gate.
    """
    import tempfile

    return tempfile.mkdtemp(prefix="cuprum-layout-stub-")


def test_the_stub_runner_really_exits_nonzero() -> None:
    """Prove the stub used above fails, so the propagation test is not vacuous."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [sys.executable, _stub_runner()],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 7, (
        f"the stub must exit 7 for the propagation test to mean anything, "
        f"got {completed.returncode}"
    )
