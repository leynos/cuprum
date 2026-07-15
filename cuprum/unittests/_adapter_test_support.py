"""Shared test support for telemetry adapter unit tests."""

from __future__ import annotations

import dataclasses as dc
import sys
import threading
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.events import ExecEvent, ExecPhase
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def _make_exec_event(
    *,
    phase: ExecPhase,
    overrides: cabc.Mapping[str, object] | None = None,
) -> ExecEvent:
    """Build an ExecEvent with sensible test defaults."""
    values: dict[str, object] = {
        "phase": phase,
        "program": "cat",
        "argv": ("cat",),
        "cwd": None,
        "env": None,
        "pid": None,
        "timestamp": 0.0,
        "line": None,
        "exit_code": None,
        "duration_s": None,
        "tags": {},
        "note": None,
        "byte_count": None,
    }
    if overrides is not None:
        unknown = set(overrides) - {field.name for field in dc.fields(ExecEvent)}
        if unknown:
            msg = f"unknown ExecEvent override fields: {sorted(unknown)!r}"
            raise KeyError(msg)
        values.update(overrides)
    return ExecEvent(
        phase=typ.cast("ExecPhase", values["phase"]),
        program=Program(typ.cast("str", values["program"])),
        argv=typ.cast("tuple[str, ...]", values["argv"]),
        cwd=typ.cast("Path | None", values["cwd"]),
        env=typ.cast("cabc.Mapping[str, str] | None", values["env"]),
        pid=typ.cast("int | None", values["pid"]),
        timestamp=typ.cast("float", values["timestamp"]),
        line=typ.cast("str | None", values["line"]),
        exit_code=typ.cast("int | None", values["exit_code"]),
        duration_s=typ.cast("float | None", values["duration_s"]),
        tags=typ.cast("cabc.Mapping[str, object]", values["tags"]),
        note=typ.cast("str | None", values["note"]),
        byte_count=typ.cast("int | None", values["byte_count"]),
    )


class _LabelRecordingCollector:
    """Record labelled metrics calls for adapter assertions."""

    def __init__(self, *, record_histograms: bool = True) -> None:
        """Initialise the recorder with optional histogram capture."""
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
        except BaseException as exc:  # noqa: BLE001 - surface worker failures.
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
