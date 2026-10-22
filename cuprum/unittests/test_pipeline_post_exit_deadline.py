"""A pipeline's post-exit drain must not outlive its configured deadline.

A pipeline stages its output gathering *after* the stage wait has finished:
``_collect_pipeline_inputs`` computes the remaining budget for the wait, then
awaits every stage's stderr consumer and the final stage's stdout consumer with
no bound of its own. A descendant that inherits those pipes and outlives its
stage therefore holds the gather open for as long as it likes, and the
configured ``timeout=`` never ends the run.

The success path is the discriminating route: the stage wait returns normally,
so no timeout handler runs and nothing has terminated the descendant that
keeps the pipes open. Three sibling cases cover the rest of the matrix, one
test each rather than a product: the wait-timeout route, which shares the same
handler; ``INHERIT``, because only an owning run may signal the stage's group
at all; and an uncaptured run, which has no consumer to bound. Each case fixes
its own policy and capture setting because they assert four different outcomes
— bounded expiry, reclamation, survival, and prompt completion — not one
outcome under four settings.

The whole module is POSIX-only. Three cases configure ``OWN_GROUP``, which the
policy rejects on Windows rather than emulating, and all four reap their
descendant with ``SIGKILL``. The module is one of ``EXTENSION_TEST_TARGETS``,
so the extension-gated CI job collects it on a Windows runner as well; the
skip below is what turns that collection into recorded skips rather than a
run of failures about a primitive the platform does not have.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import sys
import threading
import time
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
from cuprum.unittests._post_exit_teardown_support import reap_descendant
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


# Three of the four cases configure ``OWN_GROUP``, which the policy rejects on
# Windows rather than emulating, and every case reaps its descendant with
# ``SIGKILL``, which Windows does not have either. The module is scoped to the
# platform that has both. Skipping is not cosmetic — this module is one of
# ``EXTENSION_TEST_TARGETS``, which the extension-gated CI job runs on a Windows
# runner, so without the mark the job fails on a platform gap rather than on the
# code under test.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# The deadline a run is given.
DEADLINE_S = 1.0

# What an expired run may spend past its deadline before it must have returned.
# The expiry is not the end of the work: the stages are then terminated (a
# cancel grace of 0.1 s, then SIGKILL), the inter-stage pumps are reconciled,
# and a capturing settlement gives the remaining readers a 0.25 s EOF grace —
# about 1.5 s in total, measured. This leaves room for that plus spawn cost on
# a loaded runner while still failing a settlement that outlasts its budget by
# seconds, which the old 12 s harness bound could not.
SETTLE_ALLOWANCE_S = 3.0

# The outer bound the test itself applies. It only fires if the run under test
# has already failed: a bounded run returns in about a second.
_HARNESS_BUDGET_S = 12.0

# A marker written by the descendant's parent before it exits, so a capturing
# run has captured output to preserve across the expiry.
MARKER = "before-the-descendant"

type _PipelineRunFn = cabc.Callable[
    [Pipeline, _RunKwargs],
    tuple[TimeoutExpired, set[asyncio.Task[object]]],
]


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
    """Return an execution context selecting *policy* with a short cancel grace."""
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
    """Run *pipeline* under a deadline, require it to expire, and time it.

    The timing is the assertion that the settlement was *bounded* rather than
    merely finite. ``run`` already refuses to outlast its own harness bound, but
    a bound of several seconds beyond the configured deadline accepts exactly
    the regression this module exists to catch — a retreat to the old unbounded
    drain — so the settled figure is measured here instead.

    Returns
    -------
    tuple[TimeoutExpired, set[asyncio.Task[object]]]
        The expiry that ended the run, and any tasks it left pending.
    """
    kwargs: _RunKwargs = {
        "timeout": DEADLINE_S,
        "output": RunOutputOptions(capture=capture),
        "context": _context(policy),
    }
    started = time.monotonic()
    outcome = run(pipeline, kwargs)
    elapsed = time.monotonic() - started
    assert elapsed < DEADLINE_S + SETTLE_ALLOWANCE_S, (
        f"an expired run must settle within its {DEADLINE_S}s deadline plus "
        f"the {SETTLE_ALLOWANCE_S}s cleanup allowance, took {elapsed:.2f}s"
    )
    return outcome


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
    descendant: int | None = None

    try:
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            expired, leaked = _run_to_deadline(
                pipeline,
                pipeline_run_fn,
                policy=ProcessGroupPolicy.OWN_GROUP,
                capture=True,
            )
            descendant = wait_for_pid_file(
                pid_file,
                context="the success-route group reclamation",
            )

        assert expired.timeout == DEADLINE_S, (
            f"the expiry must report the configured timeout {DEADLINE_S!r}, "
            f"got {expired.timeout!r}"
        )
        assert not leaked, f"the run left pending tasks behind: {leaked!r}"
        assert not process_is_running(descendant), (
            f"an owning run must reclaim descendant pid {descendant}, "
            "even when every stage exited on its own"
        )
    finally:
        # ``None`` makes the reaper read the pid file itself, so a run that
        # failed before recording a descendant still exits through one path.
        reap_descendant(pid_file, descendant)


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
    descendant: int | None = None

    try:
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
    finally:
        reap_descendant(pid_file, descendant)


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
    descendant: int | None = None

    try:
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
        reap_descendant(pid_file, descendant)


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

    descendant: int | None = None
    try:
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

        assert [stage.exit_code for stage in result.stages] == [0, 0], (
            f"both stages must report a normal exit, got {result.stages!r}"
        )
    finally:
        # Inside the ``finally`` from the start: a run that raises, or a pid
        # file that never appears, must not leave a ``SIGTERM``-immune
        # descendant on the shared host. ``None`` makes the reaper read the pid
        # file itself, so a run that failed before recording a descendant still
        # exits through one path.
        reap_descendant(pid_file, descendant)
