"""Record one JSON evidence file per pytest session, on request only.

Issue #607 asks whether the default suite's process boundaries earn their cost.
Answering it needs per-invocation facts - what was collected, what ran, how long
collection took, and how the process ended - that a passing test run does not
print. This module is a pytest plugin that writes one such record per session.

It is inert unless ``CUPRUM_PYTEST_EVIDENCE_DIR`` names a directory. A run that
does not set the variable behaves exactly as it did before the plugin existed:
no hook does work, no file is written, and no exit status changes. That is the
property the experiment depends on, because a measurement tool that perturbs
the thing it measures is worth nothing.

Under `pytest-xdist` the controller writes the record and counts worker
failures; workers attach no record of their own, so one pytest invocation
produces one evidence file rather than one per worker. Collection is therefore
read from the workers' reports, because the controller collects nothing itself.

A failure to write is reported once and swallowed. Evidence is a diagnostic, so
it must never turn a green suite red; a full disk failing the build would make
the instrumentation more dangerous than the problem it studies.
"""

from __future__ import annotations

import os
import pathlib as pth
import time
import typing as typ
import uuid

from scripts.atomic_write import write_json

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

__all__ = (
    "EVIDENCE_ENV",
    "SCHEMA_VERSION",
    "SessionEvidence",
    "evidence_directory",
    "record_path",
    "register",
)

#: The variable that turns the plugin on. An empty or absent value leaves the
#: suite untouched, so a caller opts in per step rather than globally.
EVIDENCE_ENV = "CUPRUM_PYTEST_EVIDENCE_DIR"

#: Bumped when a field changes meaning, so an old artefact is not silently
#: compared against a new one.
SCHEMA_VERSION = 1

#: Skip and xfail reasons come from arbitrary test code, so they are bounded
#: before they enter a record a reviewer reads.
_MAX_REASON_CHARS = 200

#: The plugin's registration name, so a caller can tell whether it is active.
PLUGIN_NAME = "cuprum-pytest-session-evidence"

#: The length of the ``(file, line, reason)`` tuple pytest uses for a skip
#: longrepr. Naming it keeps the destructuring self-describing.
_SKIP_LONGREPR_LENGTH = 3


def evidence_directory() -> pth.Path | None:
    """Return the configured evidence directory, or ``None`` when disabled.

    Returns
    -------
    pathlib.Path or None
        The directory named by :data:`EVIDENCE_ENV`, or ``None`` when the
        variable is unset or blank. A blank value is treated as absent so a
        workflow that interpolates an unset variable does not create a record
        in the working directory.
    """
    configured = os.environ.get(EVIDENCE_ENV, "").strip()
    return pth.Path(configured) if configured else None


def _truncate(reason: object) -> str:
    """Return a bounded, single-line rendering of a skip or xfail reason."""
    text = " ".join(str(reason).split())
    if len(text) <= _MAX_REASON_CHARS:
        return text
    return f"{text[: _MAX_REASON_CHARS - 3]}..."


def _reports_an_outcome(report: pytest.TestReport) -> bool:
    """Return whether this phase settles the node's outcome.

    A call phase always reports one, and so does any failure or skip, whatever
    the phase: a setup skip is the node's result even though no call ran, and
    omitting it would make a valid run look like it had missing evidence.

    Parameters
    ----------
    report : pytest.TestReport
        The report for one phase of one node.

    Returns
    -------
    bool
        Whether the report should be recorded as the node's outcome.
    """
    return report.when == "call" or report.failed or bool(report.skipped)


def _skip_reason(report: pytest.TestReport) -> str:
    """Return the bounded skip reason a skip report carries, if any."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) >= _SKIP_LONGREPR_LENGTH:
        return _truncate(longrepr[_SKIP_LONGREPR_LENGTH - 1])
    return ""


class SessionEvidence:
    """Accumulate one session's record and write it when the session ends.

    Durations are summed per phase rather than stored per node. A suite of
    several thousand nodes would otherwise produce a record too large to review
    and too large to upload, and the experiment only needs totals.

    Parameters
    ----------
    The plugin is registered as an instance, so a hook that receives only a
    report reaches this session through the registration rather than through a
    module-level global. The ``config`` argument is accepted for the
    registration signature and is not retained: every fact the record needs is
    observed by a hook, and holding the configuration alive past the session
    would outlive the objects it owns.
    """

    def __init__(self, config: pytest.Config) -> None:
        """Start a record for the session the config belongs to."""
        del config
        self.invocation_id = uuid.uuid4().hex
        self.started = time.time()
        self._monotonic_started = time.monotonic()
        self._collection_seconds: float | None = None
        self._collected: list[str] = []
        self._outcomes: dict[str, str] = {}
        self._phase_seconds: dict[str, float] = {"setup": 0.0, "call": 0.0}
        self._skip_reasons: dict[str, str] = {}
        self._xfail_reasons: dict[str, str] = {}
        self._failures: list[str] = []
        self._worker_failures: int = 0

    @property
    def collected(self) -> tuple[str, ...]:
        """The node IDs this session collected, sorted."""
        return tuple(sorted(self._collected))

    @property
    def worker_failures(self) -> int:
        """How many xdist workers went down with an error."""
        return self._worker_failures

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Record collection for a run that collects in this process.

        Parameters
        ----------
        session : pytest.Session
            The finished collection. Its item list is the authoritative
            statement of what this process will run. Under xdist the controller
            collects nothing, so the worker hook supplies the same fact.
        """
        self._collected.extend(item.nodeid for item in session.items)
        self._mark_collected()

    def pytest_xdist_node_collection_finished(
        self, node: object, ids: cabc.Sequence[str]
    ) -> None:
        """Record one worker's collected node IDs.

        Parameters
        ----------
        node : object
            The worker that finished collecting. Unused; the IDs are the fact.
        ids : Sequence of str
            The node IDs that worker collected.
        """
        del node
        self._collected.extend(ids)
        self._mark_collected()

    def pytest_testnodedown(self, node: object, error: object | None) -> None:
        """Count a worker that went down with an error.

        Parameters
        ----------
        node : object
            The worker that went down. Unused.
        error : object or None
            The worker's failure, or ``None`` for a clean shutdown. A non-None
            value is a crash the record must not lose, because a truncated run
            with no crash count reads exactly like a complete one.
        """
        del node
        if error is not None:
            self._worker_failures += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Fold one test report into the session totals.

        Parameters
        ----------
        report : pytest.TestReport
            The report pytest published for one phase of one node. Setup, call
            and teardown durations are summed separately, and a teardown
            failure is recorded as an outcome so a node that passed its call
            and then leaked is not lost.
        """
        phase = report.when or "unknown"
        self._phase_seconds[phase] = (
            self._phase_seconds.get(phase, 0.0) + report.duration
        )
        if _reports_an_outcome(report):
            self._outcomes[report.nodeid] = report.outcome
        if report.skipped:
            reason = _skip_reason(report)
            if reason:
                self._skip_reasons[report.nodeid] = reason
        if hasattr(report, "wasxfail"):
            self._xfail_reasons[report.nodeid] = _truncate(
                getattr(report, "wasxfail", "")
            )
        if report.failed:
            self._failures.append(_truncate(report.nodeid))

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Write the session record.

        Parameters
        ----------
        session : pytest.Session
            The finishing session. Unused; the record was accumulated as it ran.
        exitstatus : int
            The status pytest will exit with. It is recorded, never changed.
        """
        del session
        directory = evidence_directory()
        if directory is None:
            return
        write_record(directory, self.as_record(exitstatus))

    def _mark_collected(self) -> None:
        """Record elapsed time at the first point collection is complete."""
        if self._collection_seconds is None:
            self._collection_seconds = round(
                time.monotonic() - self._monotonic_started, 3
            )

    def as_record(self, exit_status: int) -> dict[str, object]:
        """Return the finished record.

        Parameters
        ----------
        exit_status : int
            The session's exit status, as pytest reported it.

        Returns
        -------
        dict
            A JSON-serializable record carrying :data:`SCHEMA_VERSION`.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "invocation_id": self.invocation_id,
            "started": self.started,
            "finished": time.time(),
            "wall_seconds": round(time.monotonic() - self._monotonic_started, 3),
            "collection_seconds": self._collection_seconds,
            "collected": sorted(self._collected),
            "outcomes": dict(sorted(self._outcomes.items())),
            "failures": sorted(self._failures),
            "phase_seconds": {
                phase: round(seconds, 3)
                for phase, seconds in sorted(self._phase_seconds.items())
            },
            "skip_reasons": dict(sorted(self._skip_reasons.items())),
            "xfail_reasons": dict(sorted(self._xfail_reasons.items())),
            "worker_failures": self._worker_failures,
            "exit_status": exit_status,
        }


def record_path(directory: pth.Path, invocation_id: str) -> pth.Path:
    """Return the file name a record is written to.

    Parameters
    ----------
    directory : pathlib.Path
        Destination directory.
    invocation_id : str
        The record's identifier.

    Returns
    -------
    pathlib.Path
        The path the record occupies once written.
    """
    return directory / f"pytest-evidence-{invocation_id}.json"


def write_record(
    directory: pth.Path, record: cabc.Mapping[str, object]
) -> pth.Path | None:
    """Write one record atomically into ``directory``.

    The write goes to a temporary file in the same directory and is then
    renamed, so an interrupted run leaves either the previous artefact or a
    complete new one. A failure to write is reported once and swallowed, since
    a diagnostic must not fail the suite it describes.

    Parameters
    ----------
    directory : pathlib.Path
        Destination directory, created if it does not exist.
    record : collections.abc.Mapping of str to object
        The record to serialize as JSON.

    Returns
    -------
    pathlib.Path or None
        The written path, or ``None`` when the write failed.
    """
    invocation_id = str(record.get("invocation_id", "unknown"))
    destination = record_path(directory, invocation_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_json(destination, record)
    except OSError as error:
        print(f"cuprum pytest evidence: could not write to {directory}: {error}")
        return None
    return destination


def register(config: pytest.Config) -> SessionEvidence | None:
    """Attach the evidence plugin when the environment asks for it.

    The plugin is registered as an instance rather than declared as module-level
    hooks, because a hook that receives only a report cannot otherwise reach the
    session it belongs to without a module-level global.

    Parameters
    ----------
    config : pytest.Config
        The configuration to register with.

    Returns
    -------
    SessionEvidence or None
        The registered plugin, or ``None`` when evidence collection is
        disabled or the plugin was already registered.
    """
    if evidence_directory() is None:
        return None
    if config.pluginmanager.has_plugin(PLUGIN_NAME):
        existing = config.pluginmanager.get_plugin(PLUGIN_NAME)
        return existing if isinstance(existing, SessionEvidence) else None
    plugin = SessionEvidence(config)
    config.pluginmanager.register(plugin, PLUGIN_NAME)
    return plugin


def pytest_configure(config: pytest.Config) -> None:
    """Register the evidence plugin when the environment asks for it.

    This module-level hook is what makes the plugin loadable through
    `pytest_plugins`, `PYTEST_PLUGINS` or `-p`, rather than only from a
    repository that happens to import :func:`register` itself. It does nothing
    when :data:`EVIDENCE_ENV` is unset, so declaring the module as a plugin does
    not start measuring a run that did not ask to be measured.

    Parameters
    ----------
    config : pytest.Config
        The session configuration to register with.
    """
    if evidence_directory() is None:
        return
    register(config)
