"""Stress-test support for Linux-native pipeline stream hand-offs."""

from __future__ import annotations

import dataclasses as dc
import pathlib
import threading
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, TimeoutExpired, scoped, sh

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._backend import StreamBackend
    from cuprum.program import Program
    from cuprum.sh import Pipeline


_REPEATED_NATIVE_PIPELINE_ATTEMPTS = 16
# CI runs this native-thread hand-off stress test on two vCPUs, so its budget
# accounts for scheduler contention rather than typical desktop latency.
_REPEATED_NATIVE_PIPELINE_ATTEMPT_TIMEOUT_S = 5.0
_REPEATED_NATIVE_PIPELINE_TIMEOUT_S = 30.0
_REPEATED_NATIVE_PIPELINE_FD_TOLERANCE = 4


def _open_fd_count() -> int:
    """Count the descriptors currently open in this process."""
    return sum(1 for _ in pathlib.Path("/proc/self/fd").iterdir())


@dc.dataclass(frozen=True, slots=True)
class _NativePipelineHandOff:
    """Native pipeline hand-off configuration for one stress-test run."""

    active_backend: StreamBackend
    make_pipeline: cabc.Callable[[str], tuple[Pipeline, frozenset[Program]]]

    def run_attempt(
        self,
        attempt: int,
        timeout_s: float,
        fd_delta: int,
        thread_delta: int,
    ) -> None:
        """Run one native hand-off and retain diagnostic context on failure."""
        pipeline, allowlist = self.make_pipeline(
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        )
        attempt_started_at = time.monotonic()
        with scoped(ScopeConfig(allowlist=allowlist)):
            try:
                result = pipeline.run_sync(timeout=timeout_s)
            except TimeoutExpired as error:
                attempt_elapsed_s = time.monotonic() - attempt_started_at
                pytest.fail(
                    "AUTO native pipeline hand-off exceeded its local deadline "
                    f"(attempt={attempt}, backend={self.active_backend.value}, "
                    f"attempt_elapsed_s={attempt_elapsed_s:.3f}, "
                    f"fd_delta={fd_delta}, thread_delta={thread_delta}, "
                    f"task={pipeline!r}, error={error!r})",
                )
        if result.stdout != "HELLO":
            pytest.fail(
                f"attempt {attempt} with backend={self.active_backend.value} lost "
                "pipeline output",
            )
        if not result.ok:
            pytest.fail(
                f"attempt {attempt} with backend={self.active_backend.value} had a "
                "non-zero stage",
            )


_DEEP_NATIVE_PIPELINE_HOPS = 6
_DEEP_NATIVE_PIPELINE_BYTES = 4 * 1024 * 1024
_DEEP_NATIVE_PIPELINE_TIMEOUT_S = 20.0


def assert_deep_native_pipeline_completes(
    active_backend: StreamBackend,
    allowlist: frozenset[Program],
    *,
    python_builder: cabc.Callable[..., sh.SafeCmd],
    cat_builder: cabc.Callable[..., sh.SafeCmd],
) -> None:
    """Pump a payload through more concurrent native hops than the idle pool.

    Every hop of a pipeline blocks while the pipe below it is full, and only a
    later hop can drain that pipe. A worker pool that made a submission wait
    for a free worker therefore deadlocked this shape as soon as the payload
    outgrew the capacity the hops share, so the chain runs deeper than the
    idle retention limit on a payload far larger than a single pipe buffer.
    """
    payload = (
        "import sys; "
        f"sys.stdout.buffer.write(b'x' * {_DEEP_NATIVE_PIPELINE_BYTES}); "
        "sys.stdout.flush()"
    )
    commands = [python_builder("-c", payload)]
    commands.extend(cat_builder() for _ in range(_DEEP_NATIVE_PIPELINE_HOPS))
    pipeline = sh.Pipeline(tuple(commands))

    started_at = time.monotonic()
    try:
        with scoped(ScopeConfig(allowlist=allowlist)):
            result = pipeline.run_sync(timeout=_DEEP_NATIVE_PIPELINE_TIMEOUT_S)
    except TimeoutExpired as error:
        pytest.fail(
            "a native pipeline deeper than the idle worker pool must still "
            f"complete (hops={_DEEP_NATIVE_PIPELINE_HOPS}, "
            f"bytes={_DEEP_NATIVE_PIPELINE_BYTES}, "
            f"backend={active_backend.value}, "
            f"elapsed_s={time.monotonic() - started_at:.3f}, "
            f"error={error!r})",
        )
    stdout_text = result.stdout
    if stdout_text is None:
        pytest.fail(
            "the final stage of a capturing pipeline must return its stdout, "
            f"found {result.stdout!r}",
        )
    if len(stdout_text) != _DEEP_NATIVE_PIPELINE_BYTES:
        pytest.fail(
            "every byte must survive a deep native hand-off, found "
            f"{len(stdout_text)} of {_DEEP_NATIVE_PIPELINE_BYTES}",
        )
    if not result.ok:
        pytest.fail(
            "all stages of a deep native pipeline must exit successfully, "
            f"found failure_index={result.failure_index!r} with stage "
            f"exit codes "
            f"{[stage.exit_code for stage in result.stages]!r}",
        )


def assert_repeated_native_pipeline_hand_off(
    active_backend: StreamBackend,
    make_pipeline: cabc.Callable[[str], tuple[Pipeline, frozenset[Program]]],
) -> None:
    """Exercise repeated native hand-offs within bounded time and descriptor use."""
    hand_off = _NativePipelineHandOff(active_backend, make_pipeline)
    started_at = time.monotonic()
    initial_fd_count = _open_fd_count()
    initial_thread_count = threading.active_count()
    for attempt in range(_REPEATED_NATIVE_PIPELINE_ATTEMPTS):
        fd_count = _open_fd_count()
        fd_delta = fd_count - initial_fd_count
        if fd_delta > _REPEATED_NATIVE_PIPELINE_FD_TOLERANCE:
            pytest.fail(
                "native pipeline hand-off leaked descriptors across attempts "
                f"(attempt={attempt}, initial_fd_count={initial_fd_count}, "
                f"fd_count={fd_count}, fd_delta={fd_delta})",
            )
        elapsed_s = time.monotonic() - started_at
        remaining_s = _REPEATED_NATIVE_PIPELINE_TIMEOUT_S - elapsed_s
        if remaining_s <= 0:
            pytest.fail(
                "AUTO native pipeline hand-off exceeded its aggregate deadline "
                f"before attempt {attempt} (elapsed_s={elapsed_s:.3f}, "
                f"fd_delta={fd_delta}, "
                "thread_delta="
                f"{threading.active_count() - initial_thread_count})",
            )
        hand_off.run_attempt(
            attempt,
            min(_REPEATED_NATIVE_PIPELINE_ATTEMPT_TIMEOUT_S, remaining_s),
            fd_delta,
            threading.active_count() - initial_thread_count,
        )
        elapsed_s = time.monotonic() - started_at
        if elapsed_s > _REPEATED_NATIVE_PIPELINE_TIMEOUT_S:
            pytest.fail(
                "AUTO native pipeline hand-off exceeded its aggregate deadline "
                f"(attempt={attempt}, elapsed_s={elapsed_s:.3f}, "
                f"fd_delta={fd_delta}, "
                "thread_delta="
                f"{threading.active_count() - initial_thread_count})",
            )
