"""Spawning the direct-command POSIX child this run will reap itself.

Split from ``cuprum._wait4_process`` so that module holds the handle and the
single ``wait4`` call it owns, and this one holds the spawn and the fallbacks
around it. The seam is a lifetime boundary: the handle in that module exists
from ``Popen`` until the child's status is published, while everything here
runs before that object has any pipes attached — or, for the resource
fallbacks, before and after it exists at all.

Two spawn routes are offered rather than one because the ``wait4`` route is not
available everywhere. :func:`spawn_direct_process` chooses between them on
:func:`~cuprum._rusage.wait4_resource_measurement_available`, and the resource
helpers in the sibling module answer consistently with that choice: a platform
that cannot measure a child's own usage reports the aggregate
``RUSAGE_CHILDREN`` delta instead, or nothing.

The cleanup after a failed attachment goes through the run's shared teardown
rather than a bare ``kill``. The child is created before its pipes are, so a
failure there has to dispose of a live process that may already lead a group —
a shell that forked before the attachment failed — and that disposal has to
survive a cancellation arriving on it, or the child is left un-reaped.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import typing as typ

from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._rusage import (
    ChildResourceUsage,
    _ChildRusageSnapshot,
    capture_child_rusage,
    child_rusage_delta,
    wait4_resource_measurement_available,
)
from cuprum._teardown_policy import _TeardownPolicy
from cuprum._wait4_process import _Wait4Process

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class DirectProcessConfig:
    """The direct-child spawn inputs shared by asyncio and ``wait4`` paths.

    ``start_new_session`` is carried here rather than passed to
    :func:`spawn_direct_process` as a keyword because both downstream spawn
    routes — :func:`_spawn_popen` and ``asyncio.create_subprocess_exec`` —
    accept it, and a single field keeps the two routes from drifting apart on
    the ownership policy.
    """

    argv: tuple[str, ...]
    stdin: int | None
    stdout: int | None
    stderr: int | None
    env: cabc.Mapping[str, str] | None
    cwd: str | None
    start_new_session: bool = False


def _spawn_popen(config: DirectProcessConfig) -> subprocess.Popen[bytes]:
    """Spawn the validated command synchronously before connecting its pipes."""
    # ruff: ignore[subprocess-without-shell-equals-true] - argv validated; shell=False.
    return subprocess.Popen(
        config.argv,
        stdin=config.stdin,
        stdout=config.stdout,
        stderr=config.stderr,
        env=config.env,
        cwd=config.cwd,
        start_new_session=config.start_new_session,
        shell=False,
    )


async def spawn_wait4_process(
    config: DirectProcessConfig,
) -> asyncio.subprocess.Process:
    """Spawn a direct POSIX child that owns its ``wait4`` resource usage.

    The child exists before its pipes do, so an attachment failure has to clean
    up whatever was already started. That cleanup goes through the run's shared
    teardown rather than a bare ``kill``, for two reasons the previous direct
    call could not cover: the child may lead a group that already holds
    descendants — a shell that forked before the attachment failed — which a
    signal aimed at the leader alone would leave running, and the escalation
    must survive a cancellation arriving on it, or the child is left
    un-reaped with the resource usage this path exists to measure.

    ``start_new_session`` is not merely a spawn flag here: it is the same
    ownership fact ``ProcessGroupPolicy.OWN_GROUP`` selects, so reading it is
    what keeps the spawn and its cleanup from disagreeing about whose group is
    being torn down.

    Returns
    -------
    asyncio.subprocess.Process
        The spawned child, with its pipes attached.
    """
    loop = asyncio.get_running_loop()
    process = _Wait4Process(_spawn_popen(config), loop)
    try:
        await process.connect_pipes()
    except BaseException:
        await _settle_spawned_child(
            process,
            owns_group=config.start_new_session,
        )
        raise
    return process


# A spawn that never completed has no in-flight work left to finish: the child
# was created milliseconds ago and its streams were never attached. The cleanup
# therefore escalates at once, which is what the pre-ownership ``process.kill``
# did. Only the reach of that signal and its shielding change.
_SPAWN_FAILURE_GRACE_S = 0.0


async def _settle_spawned_child(
    process: asyncio.subprocess.Process,
    *,
    owns_group: bool,
) -> None:
    """Terminate and reap a child whose pipe attachment failed.

    The shared teardown signals the child's target — the group when this run
    owns one — escalates, and awaits the direct child's exit, so reaping stays
    with this path's single ``wait4`` owner rather than being left to whatever
    collects a zombie later.
    """
    await _terminate_all_shielded(
        (process,),
        _TeardownPolicy(_SPAWN_FAILURE_GRACE_S, owns_group=owns_group),
    )


async def spawn_direct_process(
    config: DirectProcessConfig,
) -> asyncio.subprocess.Process:
    """Spawn a direct process using ``wait4`` where it can own the reap."""
    if wait4_resource_measurement_available():
        return await spawn_wait4_process(config)
    return await asyncio.create_subprocess_exec(
        *config.argv,
        stdin=config.stdin,
        stdout=config.stdout,
        stderr=config.stderr,
        env=config.env,
        cwd=config.cwd,
        start_new_session=config.start_new_session,
    )


def capture_resource_before_spawn() -> _ChildRusageSnapshot | None:
    """Capture aggregate fallback usage only when ``wait4`` is unavailable."""
    return None if wait4_resource_measurement_available() else capture_child_rusage()


def resource_usage_for(
    process: asyncio.subprocess.Process,
    before: _ChildRusageSnapshot | None,
) -> ChildResourceUsage | None:
    """Return owned ``wait4`` usage or the aggregate CPU-only fallback delta."""
    direct_usage = (
        process.resource_usage if isinstance(process, _Wait4Process) else None
    )
    return direct_usage or child_rusage_delta(before, capture_child_rusage())


__all__ = [
    "DirectProcessConfig",
    "capture_resource_before_spawn",
    "resource_usage_for",
    "spawn_direct_process",
]
