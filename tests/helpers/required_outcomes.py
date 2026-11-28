r"""Fail a curated pytest run unless every selected case passes.

The macOS lifecycle leg exists to show that behaviour which only a POSIX host
without Linux procfs can exercise really does run there. Pytest treats a skip
as success, so a leg whose cases all skipped would report the opposite of what
it was asked to prove. This plugin is loaded only by that leg, and by the
controls in ``tests/test_required_outcomes_guard.py``, so ordinary suites keep
pytest's own semantics.

The guard observes the reports pytest already produces and turns the outcomes
the leg promises never to see into named failures at session end:

- an empty selection, a collection error, or a module-level skip;
- a skipped or failed phase, an xfail, or an xpass;
- a selected case that produced no passing call phase;
- a deselection the manifest did not predict, or a predicted one that matched
  nothing;
- a required node ID that was not selected or did not pass.

Nothing here can turn a pytest failure into a success. The guard only ever adds
a failure, and ``pytest_sessionfinish`` leaves a non-zero status alone.

Examples
--------
Run pytest over a manifest with one ``--required-outcome`` per promised node::

    pytest -p tests.helpers.required_outcomes \\
        --required-outcome tests/test_example.py::test_passes \\
        --expected-deselect tests/test_example.py::test_known_skip \\
        --deselect tests/test_example.py::test_known_skip
"""

from __future__ import annotations

import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "CATEGORIES",
    "Guard",
    "pytest_addoption",
    "pytest_collection_finish",
    "pytest_collectreport",
    "pytest_configure",
    "pytest_deselected",
    "pytest_report_header",
    "pytest_runtest_logreport",
    "pytest_sessionfinish",
    "pytest_terminal_summary",
    "pytest_unconfigure",
)

#: Every way a guarded run can fall short. The names are stable so a failure
#: log can be searched for the promise that was broken.
CATEGORIES: typ.Final = (
    "empty-selection",
    "collection-error",
    "collection-skip",
    "failed",
    "skipped",
    "xfail",
    "xpass",
    "missing-passing-call",
    "unexpected-deselection",
    "stale-deselection",
    "missing-required-outcome",
    "required-outcome-not-passed",
)

_REQUIRED_OPTION = "--required-outcome"
_EXPECTED_DESELECT_OPTION = "--expected-deselect"


class Guard:
    """The outcomes one guarded pytest session has produced.

    The guard is a pure observer until :meth:`verdict` runs: it records reports
    and compares the result with the contract the manifest declared.
    """

    def __init__(
        self,
        *,
        required: frozenset[str],
        expected_deselections: frozenset[str],
    ) -> None:
        """Create a guard for one session's declared contract."""
        self.required = required
        self.expected_deselections = expected_deselections
        self.selected: tuple[str, ...] = ()
        self.deselected: set[str] = set()
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.skipped: set[str] = set()
        self.xfailed: set[str] = set()
        self.xpassed: set[str] = set()
        self.collection_errors: list[str] = []
        self.collection_skips: set[str] = set()
        self.problems: dict[str, tuple[str, ...]] = {}

    def record(self, report: pytest.TestReport) -> None:
        """Record one setup, call, or teardown report."""
        if report.failed:
            self.failed.add(report.nodeid)
        if report.skipped:
            _record_outcome(report, expected=self.xfailed, unexpected=self.skipped)
        if report.passed and report.when == "call":
            _record_outcome(report, expected=self.xpassed, unexpected=self.passed)

    def verdict(self) -> dict[str, tuple[str, ...]]:
        """Return every broken promise, keyed by its diagnostic category.

        Returns
        -------
        dict of str to tuple of str
            The categories that failed, each naming the node IDs that made it
            fail. An empty mapping means the run met its contract.
        """
        selected = set(self.selected)
        problems: dict[str, tuple[str, ...]] = {}
        if not selected:
            problems["empty-selection"] = ()
        problems.update({
            category: seen
            for category, seen in (
                ("collection-error", tuple(self.collection_errors)),
                ("collection-skip", tuple(sorted(self.collection_skips))),
                ("failed", tuple(sorted(self.failed))),
                ("skipped", tuple(sorted(self.skipped))),
                ("xfail", tuple(sorted(self.xfailed))),
                ("xpass", tuple(sorted(self.xpassed))),
                (
                    "missing-passing-call",
                    tuple(sorted(selected - self.passed)),
                ),
                (
                    "unexpected-deselection",
                    tuple(sorted(self.deselected - self.expected_deselections)),
                ),
                (
                    "stale-deselection",
                    tuple(sorted(self.expected_deselections - self.deselected)),
                ),
                (
                    "missing-required-outcome",
                    tuple(sorted(self.required - selected)),
                ),
                (
                    "required-outcome-not-passed",
                    tuple(sorted((self.required & selected) - self.passed)),
                ),
            )
            if seen
        })
        return problems


class _Registry:
    """Hold the guard for the session currently running in this process."""

    guard: Guard | None = None


_REGISTRY = _Registry()


def _was_expected_to_fail(report: pytest.TestReport) -> bool:
    """Report whether pytest marked this report as an expected failure."""
    return getattr(report, "wasxfail", None) is not None


def _record_outcome(
    report: pytest.TestReport,
    *,
    expected: set[str],
    unexpected: set[str],
) -> None:
    """Record *report* in the set its expected-failure mark selects."""
    target = expected if _was_expected_to_fail(report) else unexpected
    target.add(report.nodeid)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Declare the manifest options the guard reads."""
    group = parser.getgroup("required-outcomes")
    group.addoption(
        _REQUIRED_OPTION,
        action="append",
        default=[],
        metavar="NODE_ID",
        help=(
            "Fail the run unless NODE_ID is selected and its call phase "
            "passes. Repeat for each promised outcome."
        ),
    )
    group.addoption(
        _EXPECTED_DESELECT_OPTION,
        action="append",
        default=[],
        metavar="NODE_ID",
        help=(
            "The only node IDs this run may deselect, and each must match a "
            "collected case. Repeat for each manifest deselection."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Create the guard, refusing a run whose workers this plugin cannot see.

    Raises
    ------
    pytest.UsageError
        If xdist is distributing the run. A skip or failure in another worker
        is not observed here, so the guarded verdict would not describe the
        whole run.
    """
    if getattr(config.option, "numprocesses", None):
        message = (
            "the required-outcomes guard refuses a distributed run: a skip or "
            "failure in another worker is not observable here, so the run "
            "could report success without every required outcome being seen"
        )
        raise pytest.UsageError(message)
    _REGISTRY.guard = Guard(
        required=frozenset(config.getoption("required_outcome")),
        expected_deselections=frozenset(config.getoption("expected_deselect")),
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    """Forget the guard so a later session in this process starts clean."""
    del config
    _REGISTRY.guard = None


def pytest_report_header(config: pytest.Config) -> str | None:
    """State the declared contract at the top of the run."""
    del config
    guard = _REGISTRY.guard
    if guard is None:
        return None
    return (
        f"required outcomes: {len(guard.required)} promised, "
        f"{len(guard.expected_deselections)} expected deselection(s)"
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Record the final selection, after every deselection has been applied."""
    guard = _REGISTRY.guard
    if guard is not None:
        guard.selected = tuple(item.nodeid for item in session.items)


def pytest_deselected(items: cabc.Sequence[pytest.Item]) -> None:
    """Record items removed from the run so a surprise removal can fail it."""
    guard = _REGISTRY.guard
    if guard is not None:
        guard.deselected.update(item.nodeid for item in items)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Record a collector that failed or skipped before its cases existed.

    A module-level skip removes every case in that module from the run. If no
    required outcome or predicted deselection named one of them, the removal
    would otherwise be invisible and the leg could report success having run
    less than it promised.
    """
    guard = _REGISTRY.guard
    if guard is None:
        return
    if report.failed:
        guard.collection_errors.append(report.nodeid)
    elif report.skipped:
        guard.collection_skips.add(report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record one phase report from a selected case."""
    guard = _REGISTRY.guard
    if guard is not None:
        guard.record(report)


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int | pytest.ExitCode,
) -> None:
    """Compute the verdict and add a failure when the run met none of it."""
    del exitstatus
    guard = _REGISTRY.guard
    if guard is None:
        return
    guard.problems = guard.verdict()
    if guard.problems and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int | pytest.ExitCode,
    config: pytest.Config,
) -> None:
    """Report every broken promise by category at the end of the run."""
    del exitstatus, config
    guard = _REGISTRY.guard
    if guard is None:
        return
    terminalreporter.section("required outcomes guard", sep="=")
    if not guard.problems:
        terminalreporter.write_line(
            f"all {len(guard.selected)} selected case(s) passed; "
            f"{len(guard.expected_deselections)} expected deselection(s) matched"
        )
        return
    terminalreporter.write_line("FAILED: the guarded run broke its contract")
    for category in CATEGORIES:
        nodes = guard.problems.get(category)
        if nodes is None:
            continue
        if not nodes:
            terminalreporter.write_line(f"  {category}")
            continue
        terminalreporter.write_line(f"  {category} ({len(nodes)})")
        for node in nodes:
            terminalreporter.write_line(f"    {node}")
