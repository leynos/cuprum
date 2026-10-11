"""Shared fixtures and child programs for the standard-stream redirection suites.

Two suites assert explicit ``StdioTarget`` bindings, sharing one subject: the
target-semantics suite asks *where* a redirected stream lands, and the ownership
suite asks *who* may close it afterwards. Both need the same two axes, because
neither is about the axis itself — how the command is executed (``run()`` or
``run_sync()``) and which backend spawns it (the ``wait4`` reaper or asyncio's
transport).

The child programs are here for the same reason. Each is a one-line snippet
whose whole meaning is "write this much to that stream", and a case that is
about *ownership* should not have to restate what the child writes in order to
have something to own.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import typing as typ

import pytest

from cuprum import _wait4_process
from cuprum.sh import RunOutputOptions, StdioTarget
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import subprocess
    from pathlib import Path

    from cuprum._wait4_process import DirectProcessConfig
    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


@dc.dataclass(slots=True)
class _BackendTraversal:
    """Which spawn routes were reached, and which route the case selected.

    ``reached`` is held live rather than copied. The spawn entry is called
    after the fixture returns, so a snapshot taken at setup would always be
    empty and every case would pass its own assertion for the wrong reason.

    Shared by both backend fixtures: the ownership suites assert that the
    route they ran is the route they selected, and the failure suites assert
    additionally that only that route was reached at all.
    """

    requested: str
    reached: list[str]


_WRITE_STDOUT = "import sys; sys.stdout.write('out\\n'); sys.stdout.flush()"
_WRITE_STDERR = "import sys; sys.stderr.write('err\\n'); sys.stderr.flush()"
_WRITE_BOTH = f"{_WRITE_STDOUT}; {_WRITE_STDERR}"
_WRITE_THEN_FAIL = f"{_WRITE_BOTH}; raise SystemExit(3)"
_NEVER_EXITS = "import time; time.sleep(30)"

# The child reports the contents of the descriptor it was handed on fd 2 onto
# its own stdout, which is where the line stream can see it. A report sent back
# on stderr would travel through the very descriptor under test.
_READ_BACK_STDERR = (
    "import os, sys; os.lseek(2, 0, 0); sys.stdout.write(os.read(2, 4096).decode())"
)

# Only POSIX descriptor semantics are asserted: on Windows a "borrowed
# descriptor" is a CRT file descriptor, whose inheritance and reuse rules are a
# different contract that this change does not claim to implement.
_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="asserts POSIX descriptor inheritance and ownership",
)


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Parameters
    ----------
    request : pytest.FixtureRequest
        The injected fixture request whose ``param`` selects the asynchronous
        or synchronous execution strategy returned here.

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture(params=["wait4", "asyncio"], ids=["wait4", "asyncio"])
def spawn_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> _BackendTraversal:
    """Select the backend a direct command is spawned through, and record it.

    A direct command has two spawn routes, chosen inside the backend by
    ``wait4_resource_measurement_available``: the ``wait4`` reaper, which is
    the only one that can return the child's own resource usage, and
    asyncio's ``create_subprocess_exec`` transport. The choice is made
    *below* the stdio mapping these suites test, and the two routes receive
    the identical three stdio values, so this axis establishes that the
    ownership and flush rules hold on both rather than on the one a Linux
    machine happens to prefer.

    Each arm now *selects* its route rather than leaving one implicit. The
    ``wait4`` arm used to patch nothing, so on a platform without ``os.wait4``
    this arm ran the asyncio transport while being labelled ``wait4`` — an
    ownership case reported as evidence about a route it never took. That arm
    now skips with a platform reason instead, and both arms record the route
    whose entry point was actually reached, so a case can assert the route it
    ran rather than the label it was given.

    The fixture returns that record rather than nothing. A case that ignores
    the return value is unaffected; the ownership suites assert on it, because
    a label is not traversal.

    Returns
    -------
    _BackendTraversal
        The route this case selected and the routes whose spawn entry was
        actually reached, handed out live so a post-run read sees them.
    """
    reached: list[str] = []
    if request.param == "wait4":
        if not _wait4_process.wait4_resource_measurement_available():
            pytest.skip("os.wait4 and waitstatus_to_exitcode are unavailable here")
        monkeypatch.setattr(
            _wait4_process, "_spawn_popen", _recording_entry(reached, "wait4")
        )
    else:
        # ``spawn_direct_process`` reads this at call time, so patching it
        # selects the fallback for this test alone. Patching the dispatch
        # itself would replace the branch under test. The wait4 entry is
        # trapped as well, so this arm cannot silently run the route it names
        # as *not* selected.
        monkeypatch.setattr(
            _wait4_process, "wait4_resource_measurement_available", lambda: False
        )
    monkeypatch.setattr(
        _wait4_process.asyncio,
        "create_subprocess_exec",
        _recording_transport(reached, "asyncio", request.param),
    )
    return _BackendTraversal(request.param, reached)


def _recording_entry(
    reached: list[str], label: str
) -> cabc.Callable[[DirectProcessConfig], subprocess.Popen[bytes]]:
    """Wrap ``_spawn_popen`` so a wait4 spawn records the route it took.

    The record is a *side effect*, not an interception: the wrapped entry is
    still called and its child still starts. Replacing it with a recorder would
    turn every ownership case into a case about a child that never ran, which
    is a different test.

    Returns
    -------
    collections.abc.Callable[[DirectProcessConfig], subprocess.Popen[bytes]]
        The entry point, recording on the way through.
    """
    original = _wait4_process._spawn_popen

    def record(config: DirectProcessConfig) -> subprocess.Popen[bytes]:
        """Note that the wait4 route ran, then spawn as it normally would."""
        reached.append(label)
        return original(config)

    return record


def _recording_transport(
    reached: list[str], label: str, selected: str
) -> cabc.Callable[..., cabc.Coroutine[object, object, object]]:
    """Wrap ``asyncio.create_subprocess_exec`` to record and to trap.

    On the asyncio arm the record is a side effect again: the real transport
    runs. On the wait4 arm it is a *trap* — reaching it means the route the
    case claims was not the route taken, because the wait4 entry was patched
    and the dispatch did not choose it. Reporting that as a named failure is
    more useful than letting the case spawn through the wrong backend and pass.

    The wrapper takes ``object`` and forwards through a cast rather than
    mirroring the transport's own parameters. An exact mirror costs one
    parameter per keyword the transport accepts — ``program``, ``stdin``,
    ``stdout``, ``stderr``, ``env``, ``cwd`` — which is six against a project
    ceiling of five, so it cannot be written at all. Accepting the call
    loosely is the only faithful option left, and it costs nothing at runtime:
    the cast is confined to the forwarding call, so arguments still reach the
    real callee and are still checked against its real signature there. A
    wrapper that drifted from the transport's parameters would fail loudly
    rather than passing silently.

    Returns
    -------
    collections.abc.Callable[..., collections.abc.Coroutine[object, object, object]]
        The transport, recording on the way through, or refusing when it
        should not have been reached at all.
    """
    original = _wait4_process.asyncio.create_subprocess_exec
    # The cast exists only because ``kwargs`` is typed as ``object``. It is
    # applied at the call rather than widening the callee's annotation: the
    # real transport keeps its own signature, and every forwarded value is
    # still checked against it at runtime.
    transport = typ.cast(
        "cabc.Callable[..., cabc.Coroutine[object, object, object]]", original
    )

    async def record(*args: object, **kwargs: object) -> object:
        """Note the asyncio route, then run or refuse the real transport."""
        reached.append(label)
        if selected != label:
            message = (
                "the asyncio transport was reached, but this case selected "
                f"{selected!r}; the wait4 entry patch did not take effect"
            )
            raise AssertionError(message)
        return await transport(*args, **kwargs)

    return record


def _refuse_to_spawn(*_: object, **__: object) -> typ.NoReturn:
    """Fail the spawn the way a missing executable does."""
    message = "missing"
    raise FileNotFoundError(message)


@pytest.fixture
def refusing_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the spawn itself fail, the way a missing executable does.

    Patched at the backend's own spawn entry point, which is the last statement
    inside ``_spawn_subprocess``'s ``try``. Patching the executor's
    ``_spawn_subprocess`` instead would replace the whole body — including the
    ``finally`` these cases exist to observe — so the run would never reach the
    cleanup being asserted.

    Unlike the timeout and exit-code outcomes, this failure unwinds through
    that ``finally`` with an owned descriptor already recorded *and* a borrowed
    object already touched, which is why both ownership cases need it.
    """
    # ``spawn_direct_process`` is read from the module at call time, so patching
    # the module attribute selects the failure without rebinding the import.
    monkeypatch.setattr(_wait4_process, "spawn_direct_process", _refuse_to_spawn)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def _redirect_options(
    stdout: StdioTarget | None = None, stderr: StdioTarget | None = None
) -> RunOutputOptions:
    """Build options that redirect both streams, leaving capture off.

    Capture reads a parent-side pipe, so it is incompatible with a redirected
    stream and must be disabled for these runs; ``RunOutputOptions`` rejects
    the combination rather than silently choosing one.

    Returns
    -------
    RunOutputOptions
        Options naming the given targets for stdout and stderr, with capture
        off. An argument left ``None`` leaves that stream at its unset default,
        which resolves to a library-owned pipe.
    """
    return RunOutputOptions(capture=False, stdout=stdout, stderr=stderr)


@dc.dataclass(frozen=True, slots=True)
class DescriptorLog:
    """Descriptors cuprum opened for one path target, and those closed again."""

    opened: list[int]
    closed: list[int]


@pytest.fixture
def target_descriptor_log(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Callable[[Path], DescriptorLog]:
    """Track descriptor opens and closes for a named owned path.

    Both hooks are process-wide, so they also see asyncio's own pipe
    descriptors and the interpreter's file writes; narrowing to the descriptors
    opened *for this target* is what makes the comparison mean what it says. A
    leaked target descriptor still fails, since it is counted as opened and then
    never appears among the closes.

    Returns
    -------
    collections.abc.Callable[[Path], DescriptorLog]
        A starter that installs the hooks for one target path and returns the
        log they fill for it.
    """

    def track(log: Path) -> DescriptorLog:
        """Install the hooks and return the lists they fill."""
        opened: list[int] = []
        closed: list[int] = []
        real_open = os.open
        real_close = os.close

        def tracking_open(path: object, flags: int, *args: object) -> int:
            """Open the file, recording a descriptor the target owns."""
            fd = real_open(path, flags, *args)  # ty: ignore[invalid-argument-type]
            if str(path) == str(log):
                opened.append(fd)
            return fd

        def tracking_close(fd: int) -> None:
            """Close a descriptor, recording it when the target opened it."""
            if fd in opened:
                closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "close", tracking_close)
        return DescriptorLog(opened, closed)

    return track


def _assert_no_descriptor_leak(descriptors: DescriptorLog) -> None:
    """Fail unless the target was opened and every open was matched by a close.

    Reopening the file and checking it is a regular file would not show this:
    both a closed and a leaked descriptor leave the path intact. Only counting
    the closes against the opens distinguishes them.
    """
    assert descriptors.opened, "the path target must actually have been opened"
    assert sorted(descriptors.closed) == sorted(descriptors.opened), (
        "every descriptor cuprum opened for the target must be closed again; "
        f"opened={descriptors.opened} closed={descriptors.closed}"
    )
