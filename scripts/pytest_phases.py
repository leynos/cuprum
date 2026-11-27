"""Plan the pytest phases a layout run will execute.

A layout says how the suite is grouped; a *phase* says how it is run. Keeping
the two apart is what lets a contract assert the property issue #607 depends on
- that the serial selection and the parallel selection are disjoint, and that
every phase stays at or below the audited worker bound - without starting a
process.

The plan is pure: it reads a layout's expanded file lists and a run's settings,
and returns phases in execution order. Serial phases come first, in declaration
order, and the optional two-worker phase comes last, so the two never overlap
and two Cargo-adjacent processes never contend on one build lock.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from scripts.pytest_layout import Layout

__all__ = ("MAX_PARALLEL_WORKERS", "LayoutError", "Options", "Phase", "phases")

#: The pytest command every group runs, spelled to match the Make recipe's
#: `uv run pytest` exactly so the two arms differ only in grouping.
PYTEST = ("uv", "run", "pytest")

#: Flags the recipe passes to every invocation.
BASE_FLAGS = ("-v",)

#: The most workers the parallel phase may use. Issue #607 qualifies two
#: workers; a larger number would fan out past the billed vCPU count.
MAX_PARALLEL_WORKERS = 2


class LayoutError(ValueError):
    """A layout that cannot be run as requested.

    Parameters
    ----------
    reason : str
        Why the layout is unusable, phrased for the person who wrote it.
    """

    def __init__(self, reason: str) -> None:
        """Record the refusal reason."""
        super().__init__(reason)
        self.reason = reason


@dc.dataclass(frozen=True, slots=True)
class Phase:
    """One pytest process in a layout run.

    Attributes
    ----------
    files : tuple of str
        The files this phase runs, already selected and ordered.
    workers : int
        ``-n`` value. Zero keeps the phase in one process.
    distribution : str or None
        ``--dist`` strategy, or ``None`` to leave pytest's default alone.
    name : str
        ``serial`` or ``parallel``, recorded so a reader can tell which phase a
        timing belongs to.
    """

    files: tuple[str, ...]
    workers: int
    distribution: str | None
    name: str

    def argv(self) -> tuple[str, ...]:
        """Return the pytest argument vector for this phase.

        Returns
        -------
        tuple of str
            The full command, including ``uv run pytest``.
        """
        argv = [*PYTEST, *BASE_FLAGS, "-n", str(self.workers)]
        if self.distribution is not None:
            argv.extend(("--dist", self.distribution))
        argv.extend(self.files)
        return tuple(argv)


@dc.dataclass(frozen=True, slots=True)
class Options:
    """How a layout run was asked to behave.

    Attributes
    ----------
    layout : str
        Layout name to run. Defaults to ``baseline``, so no caller can
        consolidate the suite by accident.
    workers : int
        ``-n`` for the serial groups.
    distribution : str or None
        ``--dist`` for the serial groups.
    parallel : tuple of str
        Files for the separate two-worker phase. Empty by default, which is the
        serial-by-default manifest issue #607 asks for: a module nobody listed
        cannot reach the parallel phase.
    parallel_workers : int
        Worker count for the parallel phase, never above two.
    """

    layout: str = "baseline"
    workers: int = 0
    distribution: str | None = None
    parallel: tuple[str, ...] = ()
    parallel_workers: int = MAX_PARALLEL_WORKERS


def phases(
    layout: Layout,
    options: Options | None = None,
    root: pth.Path | None = None,
) -> tuple[Phase, ...]:
    """Return the ordered phases a layout run will execute.

    Parameters
    ----------
    layout : Layout
        The grouping to execute.
    options : Options or None
        The run's settings. Defaults to the serial baseline options.
    root : pathlib.Path or None
        Directory the layout's entries are relative to. Defaults to the working
        directory, which is where the Make recipe runs; a caller that resolves
        against another tree passes it explicitly.

    Returns
    -------
    tuple of Phase
        Serial phases first, in declaration order, then the optional parallel
        phase. They never overlap, which is what keeps two Cargo-adjacent
        processes off one build lock.

    Raises
    ------
    LayoutError
        If ``parallel_workers`` exceeds two, or a parallel file also appears in
        the serial selection. Either would run a test twice or fan out past the
        audited bound.
    """  # ruff: ignore[docstring-extraneous-exception] - LayoutError propagates from _require_compatible.
    settings = Options() if options is None else options
    expanded = layout.expanded(root)
    _require_compatible(expanded, settings)
    planned = _serial_phases(expanded, settings)
    if settings.parallel:
        planned.append(_parallel_phase(settings))
    return tuple(planned)


def _require_compatible(
    expanded: cabc.Sequence[cabc.Sequence[str]], settings: Options
) -> None:
    """Refuse a settings combination the audited bound does not allow.

    Parameters
    ----------
    expanded : Sequence of Sequence of str
        Each serial group's selected files.
    settings : Options
        The run's settings.

    Raises
    ------
    LayoutError
        If the parallel worker count exceeds the audited bound, or a parallel
        file is also in the serial selection. Either would run a test twice, or
        fan out past the count the runner is billed for.
    """
    if settings.parallel_workers > MAX_PARALLEL_WORKERS:
        raise LayoutError(_worker_bound_message(settings.parallel_workers))
    serial = {name for group in expanded for name in group}
    overlap = sorted(serial.intersection(settings.parallel))
    if overlap:
        raise LayoutError(_overlap_message(overlap))


def _serial_phases(
    expanded: cabc.Sequence[cabc.Sequence[str]], settings: Options
) -> list[Phase]:
    """Return one serial phase per non-empty group, in declaration order."""
    return [
        Phase(
            files=tuple(group),
            workers=settings.workers,
            distribution=settings.distribution,
            name="serial",
        )
        for group in expanded
        if group
    ]


def _parallel_phase(settings: Options) -> Phase:
    """Return the single two-worker phase for the declared manifest."""
    return Phase(
        files=tuple(settings.parallel),
        workers=settings.parallel_workers,
        distribution="load",
        name="parallel",
    )


def _worker_bound_message(requested: int) -> str:
    """Return the refusal text for a parallel worker count above the bound."""
    return f"parallel workers must be at most {MAX_PARALLEL_WORKERS}, got {requested}"


def _overlap_message(files: cabc.Sequence[str]) -> str:
    """Return the refusal text for files claimed by both phases."""
    return f"these files are in both the serial and parallel phases: {files}"
