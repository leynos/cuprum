"""A pipeline's post-exit drain must not outlive its configured deadline.

A pipeline stages its output gathering *after* the stage wait has finished:
``_collect_pipeline_inputs`` computes the remaining budget for the wait, then
awaits every stage's stderr consumer and the final stage's stdout consumer with
no bound of its own. A descendant that inherits those pipes and outlives its
stage therefore holds the gather open for as long as it likes, and the
configured ``timeout=`` never ends the run.

The success path is the discriminating route: the stage wait returns normally,
so no timeout handler runs and nothing has terminated the descendant that
keeps the pipes open. The wait-timeout route is parametrized beside it because
the two share one handler, and ``INHERIT`` beside ``OWN_GROUP`` because only an
owning run may signal the stage's group at all.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import os
import signal
import threading
import typing as typ

import pytest

from cuprum import ScopeConfig, TimeoutExpired, _rust_backend, scoped, sh
from cuprum._backend import (
    StreamBackend,
    _check_rust_available,
    get_stream_backend,
)
from cuprum._testing import (
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)
from cuprum.sh import ExecutionContext, Pipeline, ProcessGroupPolicy, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.execution import _RunKwargs
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pending_tasks,
    process_is_running,
    started_pids,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


# The deadline a run is given. It is comfortably below ``RUN_BUDGET_S`` so the
# elapsed assertion can tell "returned on its own deadline" apart from "the
# test harness gave up", and high enough that a stage which exits promptly is
# never mistaken for one that overran.
DEADLINE_S = 1.0
RUN_BUDGET_S = 20.0

# The outer bound the test itself applies. It only fires if the run under test
# has already failed: a bounded run returns in about a second.
_HARNESS_BUDGET_S = 12.0

# A marker written by the descendant's parent before it exits, so a capturing
# run has captured output to preserve across the expiry.
MARKER = "before-the-descendant"

_POLICIES = [ProcessGroupPolicy.OWN_GROUP, ProcessGroupPolicy.INHERIT]
_POLICY_IDS = ["own-group", "inherit"]
_CAPTURE_MODES = [True, False]
_CAPTURE_IDS = ["capture", "no-capture"]

type _PipelineRunFn = cabc.Callable[
    [Pipeline, _RunKwargs],
    tuple[TimeoutExpired, set[asyncio.Task[object]]],
]

assert RUN_BUDGET_S > DEADLINE_S, (
    f"RUN_BUDGET_S={RUN_BUDGET_S} must exceed DEADLINE_S={DEADLINE_S}"
)
assert RUN_BUDGET_S > _HARNESS_BUDGET_S, (
    f"RUN_BUDGET_S={RUN_BUDGET_S} must exceed the harness bound {_HARNESS_BUDGET_S}"
)


def _run_async(
    pipeline: Pipeline, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run()``, returning the expiry and any tasks left pending."""

    async def run_case() -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
        """Await the run inside a loop that is still open for inspection."""
        with pytest.raises(TimeoutExpired) as exc_info:
            await asyncio.wait_for(pipeline.run(**kwargs), _HARNESS_BUDGET_S)
        return exc_info.value, pending_tasks()

    return asyncio.run(run_case())


def _run_sync(
    pipeline: Pipeline, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run_sync()``; its loop is closed before control returns.

    ``run_sync`` owns its loop, so the harness bound is applied by the worker
    thread's join rather than by wrapping the coroutine: a run that never
    returns must fail this test instead of hanging the suite.

    Returns
    -------
    tuple[TimeoutExpired, set[asyncio.Task[object]]]
        The expiry that ended the run, and an always-empty task set: this
        runner cannot inspect a loop it does not own.
    """
    outcome: list[TimeoutExpired] = []
    failure: list[BaseException] = []

    def run_case() -> None:
        """Run the case on a thread so the caller can bound the wait."""
        try:
            with pytest.raises(TimeoutExpired) as exc_info:
                pipeline.run_sync(**kwargs)
            outcome.append(exc_info.value)
        except BaseException as exc:  # ruff: ignore[blind-except] - re-raised on the caller
            failure.append(exc)

    worker = threading.Thread(target=run_case, daemon=True)
    worker.start()
    worker.join(_HARNESS_BUDGET_S)
    if worker.is_alive():
        pytest.fail(
            f"run_sync() did not return within {_HARNESS_BUDGET_S}s; "
            f"the post-exit drain is unbounded"
        )
    if failure:
        raise failure[0]
    return outcome[0], set()


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def pipeline_run_fn(request: pytest.FixtureRequest) -> _PipelineRunFn:
    """Provide both public pipeline runners."""
    return _run_async if request.param == "async" else _run_sync


@pytest.fixture(autouse=True)
def reset_stream_dispatch_test_hooks() -> cabc.Iterator[None]:
    """Reset stream-dispatch test hooks before and after each test.

    The backend is resolved once and cached, so a test that changes which
    backend is selected must reset the caches on both sides of itself or leak
    its selection into its neighbours.
    """
    _reset_stream_dispatch()
    yield
    _reset_stream_dispatch()


def _reset_stream_dispatch() -> None:
    """Clear the Rust availability and backend resolution caches."""
    set_rust_availability_for_testing(is_available=None)
    reset_pump_stream_dispatch_for_testing()
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


@pytest.fixture(params=list(StreamBackend), ids=lambda backend: backend.value)
def stream_backend(request: pytest.FixtureRequest) -> StreamBackend:
    """Provide each stream backend that this host can actually run.

    A backend that is unavailable is skipped rather than parametrized away, so
    a host with the extension installed exercises both and a host without it
    reports the omission as a skip.

    Returns
    -------
    StreamBackend
        The backend this arm exercises.
    """
    backend: StreamBackend = request.param
    if backend is StreamBackend.RUST and not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")
    return backend


def _context(policy: ProcessGroupPolicy) -> ExecutionContext:
    """Return an execution context selecting *policy* and the given timeout."""
    return ExecutionContext(process_group=policy, cancel_grace=0.1)


def _pipeline(
    python: cabc.Callable[..., SafeCmd],
    pid_file: Path,
    *,
    blocks: bool,
) -> Pipeline:
    """Build a two-stage pipeline whose last stage leaves a pipe-holding child.

    The last stage announces :data:`MARKER`, spawns a ``SIGTERM``-immune
    descendant that inherits the run's pipes, and exits 0 — so its *own* exit
    is prompt while the pipes stay open. With ``blocks`` the first stage is
    still asleep when the deadline fires, which is the wait-timeout route;
    without it every stage has exited and only the post-exit drain remains,
    which is the success route.

    The first stage writes rather than reads in both cases: a stage that reads
    its stdin would exit as soon as the pipeline closes the previous stage's
    stdout, which would make the stage's own exit — not the descendant — the
    thing the run was waiting on.

    Returns
    -------
    Pipeline
        The two-stage pipeline, ready to run.
    """
    first = (
        python("-c", "import time; time.sleep(60)")
        if blocks
        else python("-c", "import sys; sys.stdout.write('head' + chr(10))")
    )
    last = python(
        *descendant_then_exit_argv(pid_file, exit_code=0, stdout_marker=MARKER)
    )
    return first | last


def _run_to_deadline(
    pipeline: Pipeline,
    run: _PipelineRunFn,
    *,
    policy: ProcessGroupPolicy,
    capture: bool,
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run *pipeline* under a deadline and require it to expire."""
    kwargs: _RunKwargs = {
        "timeout": DEADLINE_S,
        "output": RunOutputOptions(capture=capture),
        "context": _context(policy),
    }
    return run(pipeline, kwargs)


def test_success_route_drain_is_bounded_by_the_configured_deadline(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A descendant holding the pipes cannot extend a *successful* pipeline.

    No handler runs on this route: the stage wait returns normally, and the
    post-exit gather is the only thing that awaits the descendant's pipes. An
    unbounded gather therefore hangs here for as long as the descendant lives.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "success-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=False)

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        expired, leaked = _run_to_deadline(
            pipeline,
            pipeline_run_fn,
            policy=ProcessGroupPolicy.OWN_GROUP,
            capture=True,
        )

    assert expired.timeout == DEADLINE_S, (
        f"the expiry must report the configured timeout {DEADLINE_S!r}, "
        f"got {expired.timeout!r}"
    )
    assert not leaked, f"the run left pending tasks behind: {leaked!r}"


def test_wait_timeout_drain_is_bounded_and_reclaims_an_owned_group(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On the wait-timeout route the settlement is bounded and settles stages.

    ``OWN_GROUP`` is the arm that may signal the stage's group, so the
    descendant is expected to be gone when the run returns. The expiry itself
    must still report the configured deadline and preserve captured output.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "timeout-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=True)
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
    ):
        expired, leaked = _run_to_deadline(
            pipeline,
            pipeline_run_fn,
            policy=ProcessGroupPolicy.OWN_GROUP,
            capture=True,
        )
        descendant = wait_for_pid_file(
            pid_file,
            context="the pipeline deadline reclamation",
        )

    assert expired.timeout == DEADLINE_S, (
        f"the expiry must report the configured timeout {DEADLINE_S!r}, "
        f"got {expired.timeout!r}"
    )
    assert not leaked, f"the run left pending tasks behind: {leaked!r}"
    assert isinstance(expired.output, str), (
        f"a capturing expiry must report text output, got {expired.output!r}"
    )
    assert MARKER in expired.output, (
        f"captured output must survive the expiry, got {expired.output!r}"
    )
    assert not process_is_running(descendant), (
        f"an owning run must reclaim descendant pid {descendant}, "
        f"stages={started_pids(events)!r}"
    )


def test_wait_timeout_drain_is_bounded_under_an_inherited_group(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``INHERIT`` bounds the run without signalling a group it does not own.

    The descendant survives by design — the only process an inherited run may
    signal is the direct child, which has already exited — so what is asserted
    is a bounded settlement rather than a reclamation.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "inherit-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=True)

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        expired, leaked = _run_to_deadline(
            pipeline,
            pipeline_run_fn,
            policy=ProcessGroupPolicy.INHERIT,
            capture=True,
        )
        descendant = wait_for_pid_file(
            pid_file,
            context="the inherited-group deadline settlement",
        )

    try:
        assert expired.timeout == DEADLINE_S, (
            f"the expiry must report the configured timeout {DEADLINE_S!r}, "
            f"got {expired.timeout!r}"
        )
        assert not leaked, f"the run left pending tasks behind: {leaked!r}"
        assert isinstance(expired.output, str), (
            f"a capturing expiry must report text output, got {expired.output!r}"
        )
        assert MARKER in expired.output, (
            f"captured output must survive the expiry, got {expired.output!r}"
        )
    finally:
        _reap(descendant)


def _reap(pid: int) -> None:
    """Kill a descendant this test deliberately left running.

    The descendant ignores ``SIGTERM`` by construction, so only ``SIGKILL``
    ends it. The process may also have ended on its own, hence the suppressed
    lookup failure.
    """
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_uncaptured_success_route_does_not_wait_on_the_descendant(
    pipeline_run_fn: _PipelineRunFn,
    stream_backend: StreamBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pipeline that consumes no output has nothing to bound.

    Its stages get ``DEVNULL`` for the streams it does not read, so a
    descendant cannot hold the gather open and the run finishes on its own.
    The case is the no-capture counterpart of the capturing success route: if
    it ever starts blocking, the difference names the consumer rather than the
    deadline.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", stream_backend.value)
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pid_file = tmp_path / "uncaptured-descendant.pid"
    pipeline = _pipeline(python, pid_file, blocks=False)

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = asyncio.run(
            asyncio.wait_for(
                pipeline.run(
                    timeout=DEADLINE_S,
                    output=RunOutputOptions(capture=False),
                    context=_context(ProcessGroupPolicy.OWN_GROUP),
                ),
                _HARNESS_BUDGET_S,
            ),
        )
        descendant = wait_for_pid_file(
            pid_file,
            context="the uncaptured success route",
        )

    try:
        assert [stage.exit_code for stage in result.stages] == [0, 0], (
            f"both stages must report a normal exit, got {result.stages!r}"
        )
    finally:
        _reap(descendant)
