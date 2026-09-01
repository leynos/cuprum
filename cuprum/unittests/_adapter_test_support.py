"""Shared test support for telemetry adapter unit tests."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import sys
import threading
import typing as typ
from pathlib import Path

import pytest

from cuprum import sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import ScopeConfig, scoped
from cuprum.events import ExecEvent, ExecPhase, new_exec_id
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecHook, ExecId


def _python_builder(
    *, project_name: str = "adapter-tests"
) -> tuple[cabc.Callable[..., sh.SafeCmd], ProgramCatalogue]:
    """Build a Python command builder and catalogue for adapter tests."""
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name=project_name,
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    return sh.make(python_program, catalogue=catalogue), catalogue


#: Field defaults shared by every event built with :func:`_make_exec_event`.
#: ``exec_id`` is deliberately left ``None`` here; the factory mints a fresh
#: token per call so the constant cannot leak one correlation id across events.
_DEFAULT_EXEC_EVENT: typ.Final = ExecEvent(
    phase="start",
    program=Program("cat"),
    argv=("cat",),
    cwd=None,
    env=None,
    pid=None,
    timestamp=0.0,
    line=None,
    exit_code=None,
    duration_s=None,
    tags={},
)


def _make_exec_event(
    *,
    phase: ExecPhase,
    overrides: cabc.Mapping[str, object] | None = None,
) -> ExecEvent:
    """Build an ExecEvent with sensible test defaults.

    Parameters
    ----------
    phase : ExecPhase
        Lifecycle phase recorded on the event.
    overrides : cabc.Mapping[str, object] or None, optional
        Field values replacing the shared defaults. Unknown field names raise
        :class:`TypeError` from :func:`dataclasses.replace`.

    Returns
    -------
    ExecEvent
        An event built from :data:`_DEFAULT_EXEC_EVENT`.

    Notes
    -----
    Each event carries a fresh ``exec_id`` by default, mirroring real
    executions. Tests that span several lifecycle phases of one execution must
    thread a shared ``exec_id`` override; pass ``{"exec_id": None}`` to build a
    legacy, uncorrelated event.
    """
    changes: dict[str, object] = {
        "phase": phase,
        "exec_id": new_exec_id(),
        "tags": {},
    }
    if overrides is not None:
        changes.update(overrides)
    return dc.replace(_DEFAULT_EXEC_EVENT, **changes)


def _cat_overrides(exec_id: ExecId, pid: int = 4321) -> dict[str, object]:
    """Return the identifying overrides for a traced ``cat`` execution.

    Span-lifecycle tests care about which execution an event belongs to, not
    what it ran, so they share one program and argv and vary only the
    correlation token and pid.

    Parameters
    ----------
    exec_id : ExecId
        Correlation token minted for the execution.
    pid : int, optional
        Process identifier reported by the event.

    Returns
    -------
    dict[str, object]
        Overrides naming the program, argv, pid, and correlation token.
    """
    return {"program": "cat", "argv": ("cat",), "pid": pid, "exec_id": exec_id}


def _run_observed_python(
    hook: ExecHook,
    *args: str,
    project_name: str = "adapter-tests",
) -> None:
    """Run a Python command under ``sh.observe`` with a scoped allowlist.

    Parameters
    ----------
    hook : ExecHook
        Observe hook registered for the duration of the run.
    *args : str
        Arguments appended to the Python interpreter invocation.
    project_name : str, optional
        Project name recorded on the catalogue used for the run.
    """
    builder, catalogue = _python_builder(project_name=project_name)
    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        builder(*args).run_sync()


class Traced(typ.NamedTuple):
    """An in-memory tracer and the hook recording spans into it."""

    tracer: InMemoryTracer
    hook: TracingHook


@pytest.fixture
def tracing_hook() -> Traced:
    """Provide an in-memory tracer paired with a hook writing into it.

    Returns
    -------
    Traced
        The tracer and the ``TracingHook`` bound to it.
    """
    tracer = InMemoryTracer()
    return Traced(tracer, TracingHook(tracer))


class Metered(typ.NamedTuple):
    """An in-memory metrics store and the hook recording into it."""

    metrics: InMemoryMetrics
    hook: MetricsHook


@pytest.fixture
def metrics_hook() -> Metered:
    """Provide an in-memory metrics store paired with its hook.

    Returns
    -------
    Metered
        The metrics store and the ``MetricsHook`` bound to it.
    """
    metrics = InMemoryMetrics()
    return Metered(metrics, MetricsHook(metrics))


class _CollectingHandler(logging.Handler):
    """Handler retaining every record it is given."""

    def __init__(self) -> None:
        """Start with no records collected."""
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Retain ``record`` for inspection."""
        self.records.append(record)


class LoggerCapture(typ.NamedTuple):
    """A logger configured to capture everything, plus its handler."""

    logger: logging.Logger
    handler: _CollectingHandler

    @property
    def records(self) -> list[logging.LogRecord]:
        """The records captured so far.

        Returns
        -------
        list[logging.LogRecord]
            Every record the logger has emitted, in order.
        """
        return self.handler.records


@contextlib.contextmanager
def capturing_logger(name: str) -> cabc.Iterator[LoggerCapture]:
    """Capture every record emitted through the named logger.

    The logger is detached from its ancestors and set to ``DEBUG`` — the hooks
    check ``isEnabledFor``, so inheriting the root ``WARNING`` level would drop
    records — and its prior configuration is restored on exit so no handler
    outlives the test that installed it.

    Parameters
    ----------
    name : str
        Name of the logger to capture.

    Yields
    ------
    LoggerCapture
        The configured logger and the handler retaining its records.
    """
    logger = logging.getLogger(name)
    previous_handlers = logger.handlers[:]
    previous_propagate = logger.propagate
    previous_level = logger.level
    handler = _CollectingHandler()
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield LoggerCapture(logger, handler)
    finally:
        logger.removeHandler(handler)
        logger.handlers[:] = previous_handlers
        logger.propagate = previous_propagate
        logger.setLevel(previous_level)


class _LabelRecordingCollector:
    """Record labelled metrics calls for adapter assertions."""

    def __init__(self, *, record_histograms: bool = True) -> None:
        """Initialize the recorder with optional histogram capture."""
        self.calls: list[tuple[str, float, dict[str, str]]] = []
        self.labels: list[dict[str, str]] = []
        self._record_histograms = record_histograms

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a counter increment and its labels."""
        recorded_labels = dict(labels)
        self.calls.append((name, value, recorded_labels))
        self.labels.append(recorded_labels)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Optionally record a histogram observation and its labels."""
        if self._record_histograms:
            self.calls.append((name, value, dict(labels)))


def _spawn_worker_threads(
    target: cabc.Callable[[], None],
    *,
    workers: int,
    name_prefix: str,
) -> list[threading.Thread]:
    """Build named worker threads for adapter concurrency tests."""
    return [
        threading.Thread(
            target=target,
            name=f"{name_prefix}{index}",
            daemon=True,
        )
        for index in range(workers)
    ]


def _join_workers_or_raise(
    threads: list[threading.Thread],
    *,
    timeout_s: float,
) -> None:
    """Run worker threads and fail if any do not finish."""
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout_s)
    alive_threads = [thread for thread in threads if thread.is_alive()]
    if alive_threads:
        msg = f"{len(alive_threads)} worker thread(s) did not finish"
        raise TimeoutError(msg)


def _run_in_threads(target: cabc.Callable[[], None], *, workers: int = 4) -> None:
    """Run a target callable in a fixed number of threads."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    join_timeout_s = 5.0
    thread_name_prefix = f"adapter-test-worker-{id(errors)}-"

    def run_target() -> None:
        """Run the target and preserve failures for the main thread."""
        try:
            target()
        except BaseException as exc:  # ruff: ignore[blind-except] - surface worker failures.
            with errors_lock:
                errors.append(exc)

    threads = _spawn_worker_threads(
        run_target,
        workers=workers,
        name_prefix=thread_name_prefix,
    )
    _join_workers_or_raise(threads, timeout_s=join_timeout_s)
    if errors:
        raise errors[0]
