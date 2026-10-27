"""Runners and fixtures for the pipeline post-exit deadline regression matrix.

Only test_pipeline_post_exit_deadline uses this harness. It owns entrypoint and
backend selection, elapsed-time measurement, and fixture construction so the
behavioural cases can state their distinct expiry and reclamation contracts.
"""

from __future__ import annotations

import asyncio
import threading
import time
import typing as typ

import pytest

from cuprum import TimeoutExpired, _rust_backend
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
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    pending_tasks,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd
    from tests.helpers.execution import _RunKwargs


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

type _PipelineRunFn = typ.Literal["run", "run_sync", "run_bytes", "run_bytes_sync"]


def _run_async(
    pipeline: Pipeline, kwargs: _RunKwargs, entrypoint: _PipelineRunFn
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run()``, returning the expiry and any tasks left pending."""

    async def run_case() -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
        """Await the run inside a loop that is still open for inspection."""
        with pytest.raises(TimeoutExpired) as exc_info:
            await asyncio.wait_for(
                getattr(pipeline, entrypoint)(**kwargs), _HARNESS_BUDGET_S
            )
        return exc_info.value, pending_tasks()

    return asyncio.run(run_case())


def _run_sync(
    pipeline: Pipeline, kwargs: _RunKwargs, entrypoint: _PipelineRunFn
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
                getattr(pipeline, entrypoint)(**kwargs)
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


@pytest.fixture(params=["run", "run_sync", "run_bytes", "run_bytes_sync"])
def pipeline_run_fn(request: pytest.FixtureRequest) -> _PipelineRunFn:
    """Provide the text and byte variants of both public pipeline runners."""
    return typ.cast("_PipelineRunFn", request.param)


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
        *descendant_then_exit_argv(
            pid_file,
            exit_code=0,
            stdout_chunks=(f"{MARKER}\n",),
        )
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
    invoke = _run_sync if run.endswith("_sync") else _run_async
    outcome = invoke(pipeline, kwargs, run)
    elapsed = time.monotonic() - started
    assert elapsed < DEADLINE_S + SETTLE_ALLOWANCE_S, (
        f"an expired run must settle within its {DEADLINE_S}s deadline plus "
        f"the {SETTLE_ALLOWANCE_S}s cleanup allowance, took {elapsed:.2f}s"
    )
    if capture:
        _assert_captured_prefix(outcome[0], run)
    return outcome


def _assert_captured_prefix(expired: TimeoutExpired, run: _PipelineRunFn) -> None:
    """Assert the deadline preserved output in the entrypoint's own mode."""
    if "bytes" in run:
        assert isinstance(expired.output, bytes), (
            f"a byte entrypoint must preserve bytes, got {expired.output!r}"
        )
        assert MARKER.encode() in expired.output, (
            f"captured bytes must survive expiry, got {expired.output!r}"
        )
        assert isinstance(expired.stderr, bytes), (
            f"a byte entrypoint must preserve stderr bytes, got {expired.stderr!r}"
        )
    else:
        assert isinstance(expired.output, str), (
            f"a text entrypoint must preserve text, got {expired.output!r}"
        )
        assert MARKER in expired.output, (
            f"captured text must survive expiry, got {expired.output!r}"
        )
