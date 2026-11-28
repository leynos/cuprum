"""Execute a pytest layout and record what each process cost.

The Makefile's `test-python` recipe has always expressed the suite as a shell
loop over `PYTEST_TARGETS`: one pytest process per entry. Issue #607 asks
whether that many processes are necessary, and answering it needs a runner that
can execute a *different* grouping without changing what is selected, what
environment each process inherits, or how a failure propagates.

Two subcommands do the work.

``wrap`` runs one already-resolved pytest invocation and records what it cost.
The Makefile keeps its own loop and puts this in front of it, so the control arm
is the real recipe with an observer attached rather than a rewrite of it.

``run`` executes a named layout resolved by `scripts/pytest_layout.py`.
``baseline`` is the current grouping and exists so a comparison needs no branch
switch; a consolidated layout exists only while the experiment lasts.

The phases a run produces are computed by :func:`phases`, which is pure, and
executed by :func:`run_layout`. Keeping the plan separate from its execution is
what lets a contract assert that the serial and parallel phases are disjoint
without starting a process.
"""

from __future__ import annotations

import os
import pathlib as pth
import sys
import time
import typing as typ

from scripts.pytest_child_process import spawn
from scripts.pytest_layout import GroupError, Layout, build_layouts, parse_groups
from scripts.pytest_phases import (
    BASE_FLAGS,
    PYTEST,
    LayoutError,
    Options,
    Phase,
    phases,
)
from scripts.pytest_run_evidence import Envelope, Invocation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "LayoutError",
    "Options",
    "Phase",
    "main",
    "phases",
    "run_layout",
    "wrap",
)

#: Exit status for a refused command line. It matches the shell convention for
#: a usage error and cannot be confused with a test failure.
EXIT_USAGE = 2


def _unknown_flag_message(flag: str) -> str:
    """Return the refusal text for an argument the driver does not know."""
    return f"unrecognized argument {flag!r}"


def _missing_value_message(flag: str) -> str:
    """Return the refusal text for a flag given without its value."""
    return f"{flag} needs a value"


def _unknown_layout_message(name: str, known: cabc.Sequence[str]) -> str:
    """Return the refusal text for a layout name the environment omits."""
    return f"layout {name!r} is not available; known: {sorted(known)}"


def _evidence_dir() -> pth.Path | None:
    """Return the evidence directory named by the environment, if any."""
    configured = os.environ.get("CUPRUM_PYTEST_EVIDENCE_DIR", "").strip()
    return pth.Path(configured) if configured else None


def _record(envelope: Envelope, phase: Phase) -> int:
    """Run one phase, record it in the envelope, and return its status."""
    log_path = (
        envelope.evidence_dir
        / f"group-{envelope.invocation_id}-{len(envelope.invocations)}.log"
        if envelope.evidence_dir
        else None
    )
    invocation = Invocation(argv=phase.argv(), started=time.monotonic())
    outcome = spawn(invocation.argv, log_path)
    invocation.finished = time.monotonic()
    invocation.exit_status = outcome.status
    invocation.signal_number = outcome.signal_number
    invocation.incomplete = outcome.interrupted
    invocation.stdout_path = log_path
    envelope.add(invocation, files=phase.files)
    envelope.invocations[-1]["phase"] = phase.name
    return outcome.status


def wrap(argv: cabc.Sequence[str]) -> int:
    """Run one pytest invocation, record its cost, and return its status.

    Parameters
    ----------
    argv : Sequence of str
        Arguments for ``uv run pytest``, exactly as the recipe would pass them.

    Returns
    -------
    int
        The child's exit status, or ``128 + signal`` when a signal killed it,
        which is what a shell reports and what CI expects to see.
    """
    evidence_dir = _evidence_dir()
    envelope = Envelope("wrap", evidence_dir)
    full = (*PYTEST, *BASE_FLAGS, *argv)
    invocation = Invocation(argv=full, started=time.monotonic())
    log_path = (
        evidence_dir / f"child-{envelope.invocation_id}.log" if evidence_dir else None
    )
    outcome = spawn(full, log_path)
    invocation.finished = time.monotonic()
    invocation.exit_status = outcome.status
    invocation.signal_number = outcome.signal_number
    invocation.incomplete = outcome.interrupted
    invocation.stdout_path = log_path
    envelope.add(invocation, files=[name for name in argv if name.endswith(".py")])
    envelope.persist(envelope.build(outcome.status, incomplete=outcome.interrupted))
    return outcome.status


def run_layout(layout: Layout, options: Options | None = None) -> int:
    """Run every phase of a layout and return the first failing status.

    Parameters
    ----------
    layout : Layout
        The grouping to execute.
    options : Options or None
        The run's settings. Defaults to the serial baseline options.

    Returns
    -------
    int
        Zero when every phase passed, otherwise the first nonzero status. The
        run stops at the first failure, which is what the shell loop's
        ``|| exit $?`` does, so a later phase cannot mask an earlier one.
    """
    settings = Options() if options is None else options
    planned = phases(layout, settings)
    envelope = Envelope("run", _evidence_dir())
    envelope.extra = {
        "layout": layout.name,
        "workers": settings.workers,
        "distribution": settings.distribution,
        "parallel_workers": settings.parallel_workers if settings.parallel else 0,
    }
    status = 0
    for phase in planned:
        status = _record(envelope, phase)
        if status != 0:
            break
    envelope.persist(envelope.build(status, incomplete=False))
    return status


def _parse_options(arguments: cabc.Sequence[str]) -> Options:
    """Parse the driver's flags into options.

    Parameters
    ----------
    arguments : Sequence of str
        Arguments after the subcommand.

    Returns
    -------
    Options
        The parsed settings.

    Raises
    ------
    LayoutError
        If an argument is unrecognized or a valued flag has no value.
    """
    values: dict[str, str] = {}
    index = 0
    known = {"--layout", "--workers", "--dist", "--parallel"}
    while index < len(arguments):
        flag = arguments[index]
        if flag not in known:
            raise LayoutError(_unknown_flag_message(flag))
        if index + 1 >= len(arguments):
            raise LayoutError(_missing_value_message(flag))
        values[flag] = arguments[index + 1]
        index += 2
    parallel = tuple(part for part in values.get("--parallel", "").split("+") if part)
    return Options(
        layout=values.get("--layout", "baseline"),
        workers=int(values.get("--workers", "0")),
        distribution=values.get("--dist"),
        parallel=parallel,
    )


def _selected_layout(options: Options) -> Layout:
    """Return the layout the environment and options name.

    Parameters
    ----------
    options : Options
        The parsed settings.

    Returns
    -------
    Layout
        The requested layout.

    Raises
    ------
    LayoutError
        If the name is unknown, so a typo cannot silently run the baseline and
        be reported as the candidate.
    """
    baseline = parse_groups(os.environ.get("CUPRUM_PYTEST_LAYOUT_BASELINE", ""))
    consolidated_text = os.environ.get("CUPRUM_PYTEST_LAYOUT_GROUPS", "")
    consolidated = (
        parse_groups(consolidated_text) if consolidated_text.strip() else None
    )
    layouts = build_layouts(baseline, consolidated)
    layout = layouts.get(options.layout)
    if layout is None:
        raise LayoutError(_unknown_layout_message(options.layout, tuple(layouts)))
    return layout


def _run_from_environment(arguments: cabc.Sequence[str]) -> int:
    """Run the layout the environment names, applying the parsed flags."""
    try:
        options = _parse_options(arguments)
        return run_layout(_selected_layout(options), options)
    except (GroupError, LayoutError, ValueError) as error:
        print(f"pytest batches: {error}", file=sys.stderr)
        return EXIT_USAGE


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the driver's command line.

    Parameters
    ----------
    argv : Sequence of str or None
        Arguments after the program name. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        The status the process should exit with.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: pytest_batches.py {wrap|run} ...", file=sys.stderr)
        return EXIT_USAGE
    command, rest = arguments[0], arguments[1:]
    if command == "wrap":
        return wrap(rest)
    if command == "run":
        return _run_from_environment(rest)
    print(f"unknown command {command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - exercised as a script
    sys.exit(main())
