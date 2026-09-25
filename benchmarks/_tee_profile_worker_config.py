"""Configuration and result types for the tee hot-path profiling worker.

This module owns the validated ``TeeProfileWorkerConfig`` input type, the
``TeeProfileWorkerResult`` result payload, the ``TeeMode`` literal, and the
bounds shared by configuration validation and CLI argument parsing. It has no
dependency on command construction or execution, so
``benchmarks._tee_profile_worker_command`` and
``benchmarks._tee_profile_worker_execution`` can depend on it without
introducing an import cycle with ``benchmarks.tee_profile_worker``.
"""

from __future__ import annotations

import dataclasses as dc
import pathlib as pth
import typing as typ

from cuprum._streams_pump import _READ_SIZE

if typ.TYPE_CHECKING:
    from benchmarks._tee_profile_worker_backend import BackendName
    from benchmarks.sinks import SinkKind

type TeeMode = typ.Literal["echo", "capture", "tee"]

__all__ = ["TeeMode", "TeeProfileWorkerConfig", "TeeProfileWorkerResult"]

_VALID_MODES = {"echo", "capture", "tee"}
_VALID_SINKS = {"devnull", "text_blackhole", "pty_blackhole"}
_VALID_BACKENDS = {"auto", "python", "rust"}
_MAX_REPEAT_COUNT = 1000


class TeeProfileWorkerResult(typ.TypedDict):
    """Machine-readable tee profiling worker result payload."""

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
    """Configuration for one tee profiling worker execution."""

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
