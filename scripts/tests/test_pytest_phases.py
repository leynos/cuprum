"""Tests for planning a layout run's phases.

Issue #607's second stage is allowed to run *only* a proven-independent subset
under two workers, and only in a phase that never overlaps the serial one.
Those are properties of the plan, not of a subprocess, so they are asserted here
without starting one. The tests use a real temporary tree so the expansion each
phase sees is the expansion a run would see.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts.pytest_layout import Layout
from scripts.pytest_phases import (
    MAX_PARALLEL_WORKERS,
    LayoutError,
    Options,
    Phase,
    phases,
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


def _layout(tmp_path: pth.Path, *groups: tuple[str, ...]) -> Layout:
    """Return a layout of ``groups`` resolved against ``tmp_path``."""
    return Layout("demo", groups)


def test_a_serial_run_keeps_one_phase_per_nonempty_group(
    tmp_path: pth.Path,
) -> None:
    """Give every non-empty group its own serial process, as the recipe does."""
    _tree(tmp_path, "a/test_one.py", "b/test_two.py")
    layout = _layout(tmp_path, ("a/test_*.py",), ("b/test_*.py",))

    planned = phases(layout, Options(), tmp_path)

    assert [phase.files for phase in planned] == [
        ("a/test_one.py",),
        ("b/test_two.py",),
    ], "Give every non-empty group its own serial process, as the recipe does"
    assert all(phase.workers == 0 for phase in planned), (
        "the serial default must stay at zero workers"
    )


def test_an_empty_group_runs_no_process(tmp_path: pth.Path) -> None:
    """Skip a group that selected nothing rather than starting a bare pytest."""
    _tree(tmp_path, "a/test_one.py")
    layout = _layout(tmp_path, ("a/test_*.py",), ("missing/test_*.py",))

    planned = phases(layout, Options(), tmp_path)

    assert len(planned) == 1, "an empty group must not become a process"


def test_the_parallel_phase_comes_last(tmp_path: pth.Path) -> None:
    """Never overlap the phases: the parallel phase runs after every serial one."""
    _tree(tmp_path, "a/test_one.py", "b/test_two.py", "ind/test_three.py")
    layout = _layout(tmp_path, ("a/test_*.py",), ("b/test_*.py",))

    planned = phases(layout, Options(parallel=("ind/test_three.py",)), tmp_path)

    assert [phase.name for phase in planned] == ["serial", "serial", "parallel"], (
        "Never overlap the phases: the parallel phase runs after every serial one"
    )
    assert planned[-1].workers == MAX_PARALLEL_WORKERS, (
        "Never overlap the phases: the parallel phase runs after every serial one"
    )
    assert planned[-1].distribution == "load", (
        "Never overlap the phases: the parallel phase runs after every serial one"
    )


def test_the_parallel_manifest_is_empty_by_default(tmp_path: pth.Path) -> None:
    """Keep every test serial unless a manifest names it.

    This is the serial-by-default guarantee: a newly added module that nobody
    classified stays in the serial set instead of reaching a worker.
    """
    _tree(tmp_path, "a/test_one.py")
    layout = _layout(tmp_path, ("a/test_*.py",))

    planned = phases(layout, Options(), tmp_path)

    assert all(phase.name == "serial" for phase in planned), (
        "Keep every test serial unless a manifest names it"
    )


def test_a_file_in_both_phases_is_refused(tmp_path: pth.Path) -> None:
    """Refuse a manifest that would run one test twice."""
    _tree(tmp_path, "a/test_one.py")
    layout = _layout(tmp_path, ("a/test_*.py",))

    with pytest.raises(LayoutError, match="both the serial and parallel"):
        phases(layout, Options(parallel=("a/test_one.py",)), tmp_path)


def test_more_than_two_parallel_workers_is_refused(tmp_path: pth.Path) -> None:
    """Refuse a worker count past the audited bound, which `-n auto` would exceed."""
    _tree(tmp_path, "a/test_one.py")
    layout = _layout(tmp_path, ("a/test_*.py",))

    with pytest.raises(LayoutError, match="at most 2"):
        phases(
            layout,
            Options(
                parallel=("a/test_one.py",), parallel_workers=MAX_PARALLEL_WORKERS + 1
            ),
        )


def test_a_phase_argv_places_workers_before_files() -> None:
    """Build an argv whose flags cannot be mistaken for a test path."""
    phase = Phase(
        files=("tests/test_a.py", "tests/test_b.py"),
        workers=0,
        distribution=None,
        name="serial",
    )

    argv = phase.argv()

    assert argv[:5] == ("uv", "run", "pytest", "-v", "-n"), (
        "the command must be `uv run pytest` with verbose output and an "
        f"explicit worker flag, got {argv[:5]!r}"
    )
    assert argv[5] == "0", f"the worker count must follow -n, got {argv[5]!r}"
    assert argv[6:] == ("tests/test_a.py", "tests/test_b.py"), (
        f"every file must follow the flags, got {argv[6:]!r}"
    )


def test_a_phase_argv_carries_the_distribution_strategy() -> None:
    """Pass `--dist` only when a strategy was chosen."""
    phase = Phase(
        files=("tests/test_a.py",),
        workers=2,
        distribution="loadscope",
        name="parallel",
    )

    argv = phase.argv()

    assert argv[argv.index("--dist") + 1] == "loadscope", (
        "Pass `--dist` only when a strategy was chosen"
    )
    assert "-n" in argv, "the argv must carry an explicit worker count"
    assert argv[argv.index("-n") + 1] == "2", (
        "Pass `--dist` only when a strategy was chosen"
    )


def test_no_phase_ever_asks_for_automatic_workers(tmp_path: pth.Path) -> None:
    """Refuse `auto`, which would fan out on whatever the host reports."""
    _tree(tmp_path, "a/test_one.py")
    layout = _layout(tmp_path, ("a/test_*.py",))

    for phase in phases(layout, Options(), tmp_path):
        assert "auto" not in phase.argv(), "an automatic worker count is not auditable"
