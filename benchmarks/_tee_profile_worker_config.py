"""Configuration and result types for the tee hot-path profiling worker.

This module owns the validated ``TeeProfileWorkerConfig`` input type, the
``TeeProfileWorkerResult`` result payload, the ``TeeMode`` literal, and the
bounds shared by configuration validation and CLI argument parsing. It has no
dependency on command construction or execution, so
``benchmarks._tee_profile_worker_command`` and
``benchmarks._tee_profile_worker_execution`` can depend on it without
introducing an import cycle with ``benchmarks.tee_profile_worker``.
"""

# No ``from __future__ import annotations`` here: both public types are
# introspected with ``typing.get_type_hints``, so their annotations are
# evaluated eagerly and ``BackendName`` and ``SinkKind`` are runtime imports.
import dataclasses as dc
import pathlib as pth
import typing as typ

from benchmarks._tee_profile_worker_backend import BackendName
from benchmarks.sinks import SinkKind
from cuprum._streams_pump import _READ_SIZE

type TeeMode = typ.Literal["echo", "capture", "tee"]

__all__ = ["TeeMode", "TeeProfileWorkerConfig", "TeeProfileWorkerResult"]

_VALID_MODES = {"echo", "capture", "tee"}
_VALID_SINKS = {"devnull", "text_blackhole", "pty_blackhole"}
_VALID_BACKENDS = {"auto", "python", "rust"}
_MAX_REPEAT_COUNT = 1000


class TeeProfileWorkerResult(typ.TypedDict):
    """Machine-readable tee profiling worker result payload.

    The worker prints this payload as JSON on stdout, one object per run.

    Attributes
    ----------
    scenario : str
        Compact label combining mode, sink kind, callback use, stage count,
        and backend, for example ``"tee-devnull-cb-s2-rust"``.
    fixture_path : str
        Path of the fixture file the writer stage streamed.
    fixture_manifest_hash : str or None
        Hash recorded in the fixture's neighbouring ``.json`` manifest, or
        ``None`` when no manifest exists.
    stages : int
        Number of pipeline stages; ``1`` runs a single command.
    mode : TeeMode
        Output handling that was measured: ``"echo"``, ``"capture"``, or
        ``"tee"``.
    sink_kind : SinkKind
        Echo sink the run wrote to.
    with_line_callbacks : bool
        Whether a per-line observe callback was attached.
    backend : BackendName
        Stream backend requested for the run: ``"auto"``, ``"python"``, or
        ``"rust"``.
    repeat_count : int
        Number of runs the worker attempted.
    read_size : int
        Stream read size in bytes that was in effect for the runs.
    wall_time_seconds : float
        Elapsed wall-clock time for the whole repeat loop.
    lock_wait_seconds : float
        Time this thread spent waiting to acquire the backend selector.
    reentrant_rejection_count : int
        Number of rejected reentrant backend-selector acquisitions.
    status : {"ok", "failed"}
        ``"failed"`` when any run exited non-zero, which also stops the loop.
    exit_code : int
        Exit code of the last run the worker executed.
    captured_output_length : int
        Total length of captured stdout across runs; ``0`` when not captured.
    stdout_line_count : int
        Total number of stdout line events observed across runs.
    """

    scenario: str
    fixture_path: str
    fixture_manifest_hash: str | None
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    read_size: int
    wall_time_seconds: float
    lock_wait_seconds: float
    reentrant_rejection_count: int
    status: typ.Literal["ok", "failed"]
    exit_code: int
    captured_output_length: int
    stdout_line_count: int


def _validate_repeat_count(repeat_count: int) -> None:
    """Validate the bounded worker repeat count."""
    if repeat_count < 1:
        msg = f"repeat-count must be >= 1, got {repeat_count}"
        raise ValueError(msg)
    if repeat_count > _MAX_REPEAT_COUNT:
        msg = f"repeat-count must be <= {_MAX_REPEAT_COUNT}, got {repeat_count}"
        raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class TeeProfileWorkerConfig:
    """Configuration for one tee profiling worker execution.

    Construction validates every field and raises :class:`ValueError` when a
    value is out of range or unknown. ``fixture_path`` is coerced to a
    :class:`pathlib.Path` and must name an existing file.

    Attributes
    ----------
    fixture_path : pathlib.Path
        Fixture file the writer stage streams to stdout.
    stages : int
        Number of pipeline stages; ``1`` runs a single command. Must be at
        least ``1``.
    mode : TeeMode
        Output handling to measure: ``"echo"``, ``"capture"``, or ``"tee"``.
    sink_kind : SinkKind
        Echo sink to write to: ``"devnull"``, ``"text_blackhole"``, or
        ``"pty_blackhole"``.
    with_line_callbacks : bool
        Whether to attach a per-line observe callback.
    backend : BackendName
        Stream backend to select: ``"auto"``, ``"python"``, or ``"rust"``.
    repeat_count : int
        Number of runs, from ``1`` to ``1000`` inclusive.
    read_size : int, default=_READ_SIZE
        Stream read size in bytes. Must be at least ``1``.
    encoding : str, default="utf-8"
        Text encoding used to decode child output.
    errors : str, default="replace"
        Decoding error handler used with ``encoding``.
    """

    fixture_path: pth.Path
    stages: int
    mode: TeeMode
    sink_kind: SinkKind
    with_line_callbacks: bool
    backend: BackendName
    repeat_count: int
    read_size: int = _READ_SIZE
    encoding: str = "utf-8"
    errors: str = "replace"

    def __post_init__(self) -> None:
        """Validate worker configuration."""
        self._coerce_fixture_path()
        self._validate_numeric_bounds()
        self._validate_enum_fields()

    def _coerce_fixture_path(self) -> None:
        """Coerce and validate the fixture path."""
        fixture_path = pth.Path(self.fixture_path)
        object.__setattr__(self, "fixture_path", fixture_path)
        if not fixture_path.is_file():
            msg = f"fixture_path must exist and be a file: {fixture_path}"
            raise ValueError(msg)

    def _validate_numeric_bounds(self) -> None:
        """Validate numeric worker bounds."""
        if self.stages < 1:
            msg = f"stages must be >= 1, got {self.stages}"
            raise ValueError(msg)
        _validate_repeat_count(self.repeat_count)
        if self.read_size < 1:
            msg = f"read-size must be >= 1, got {self.read_size}"
            raise ValueError(msg)

    def _validate_enum_fields(self) -> None:
        """Validate enum-like worker fields."""
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}, got {self.mode!r}"
            raise ValueError(msg)
        if self.sink_kind not in _VALID_SINKS:
            msg = (
                f"sink-kind must be one of {sorted(_VALID_SINKS)}, "
                f"got {self.sink_kind!r}"
            )
            raise ValueError(msg)
        if self.backend not in _VALID_BACKENDS:
            msg = (
                f"backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.backend!r}"
            )
            raise ValueError(msg)
