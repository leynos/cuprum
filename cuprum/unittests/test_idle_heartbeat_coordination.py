"""Idle reporting when more than one command shares the parent's event loop.

A single command's heartbeat owns one clock and one destination. Everything
interesting about the feature starts when that stops being true: a pipeline has
several children and one aggregate clock, and concurrent or nested runs have
separate clocks that must not see each other's children. These tests pin the
outward-facing contract -- final-stage stdout and every stage's stderr count as
output, inter-stage transfers do not -- and the isolation the aggregate implies.

They also carry the regression that pairs this feature with the GitHub Actions
sink adapter (#360): a keepalive is a *parent* diagnostic, so a caller that
wraps a run in a log group sees it inside that group, where it reaches neither
the capture buffer nor the activity tracker.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped
from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.idle import IdleRecorder, keepalives, pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import Pipeline, PipelineResult, SafeCmd
    from tests.helpers.catalogue import PythonCatalogue

_PIPELINE_LABEL = "pipeline output idle"
_INTERVAL = 0.2


@pytest.fixture
def python(python_catalogue_env: PythonCatalogue) -> cabc.Callable[..., SafeCmd]:
    """Provide an allowlisted builder for the current interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder whose catalogue covers the running interpreter.
    """
    return python_catalogue_env.builder


@contextlib.contextmanager
def _allowlisted(env: PythonCatalogue) -> cabc.Iterator[None]:
    """Authorize the interpreter for the pipeline about to run."""
    with scoped(ScopeConfig(allowlist=frozenset([env.program]))):
        yield


def _two_stage(
    python: cabc.Callable[..., SafeCmd],
    *,
    producer: str,
    consumer: str,
) -> Pipeline:
    """Build a two-stage pipeline from two inline programmes."""
    return python("-c", producer) | python("-c", consumer)


def test_pipeline_reports_its_own_aggregate_subject(
    python_catalogue_env: PythonCatalogue,
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """A quiet pipeline is labelled for what the parent actually observes."""
    sink = io.StringIO()
    pipeline = _two_stage(
        python,
        producer="print('out')",
        consumer="import time; time.sleep(0.45); print('done')",
    )

    with _allowlisted(python_catalogue_env):
        result = pipeline.run_sync(
            output=RunOutputOptions(idle_after=0.1),
            context=ExecutionContext(stderr_sink=sink),
        )

    lines = keepalives(sink)
    assert len(lines) >= 2, f"a quiet pipeline must be reported for lines={lines!r}"
    for line in lines:
        assert _PIPELINE_LABEL in line, (
            "the aggregate heartbeat must name the pipeline's output rather than "
            f"claiming every child is idle: {line!r}"
        )
    assert result.final.stdout == "done\n", f"capture must be unaffected: {result!r}"
    assert "[cuprum]" not in (result.final.stdout or ""), (
        "the keepalive must never enter the pipeline's captured output"
    )


def test_inter_stage_transfers_do_not_defer_the_aggregate(
    python_catalogue_env: PythonCatalogue,
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """Only the outward-facing output resets the pipeline's single clock."""
    recorder = IdleRecorder()
    # The producer talks for a full second, but every byte of it is handed to
    # the next stage rather than to the parent; the consumer stays silent until
    # the producer is done.
    producer = (
        "import time\n"
        "for _ in range(20):\n"
        "    print('tick', flush=True)\n"
        "    time.sleep(0.05)\n"
        "time.sleep(0.2)\n"
    )
    consumer = "import sys, time; sys.stdin.read(); time.sleep(0.4); print('done')"
    pipeline = _two_stage(python, producer=producer, consumer=consumer)

    with _allowlisted(python_catalogue_env):
        pipeline.run_sync(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        )

    assert recorder.seen, "a pipeline quiet on its outward streams must be reported"
    assert recorder.first_total() < 0.9, (
        "an inter-stage transfer must not defer the aggregate clock; the first "
        f"report must land while the producer is still talking: {recorder.seen!r}"
    )


def test_stage_stderr_defers_the_aggregate(
    python_catalogue_env: PythonCatalogue,
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """Any stage's stderr is outward-facing, so it resets the clock."""
    recorder = IdleRecorder()
    # The producer's stdout is inter-stage, so only its stderr can be moving
    # the deadline; the consumer's own stderr stays silent throughout.
    ticks = 4
    gap = 0.15
    producer = (
        "import sys, time\n"
        f"for _ in range({ticks}):\n"
        "    print('tick', file=sys.stderr, flush=True)\n"
        f"    time.sleep({gap})\n"
    )
    consumer = "import sys, time; sys.stdin.read(); time.sleep(0.5)"
    pipeline = _two_stage(python, producer=producer, consumer=consumer)

    with _allowlisted(python_catalogue_env):
        pipeline.run_sync(
            output=RunOutputOptions(idle_after=_INTERVAL, on_idle=recorder),
            context=ExecutionContext(stderr_sink=io.StringIO()),
        )

    assert len(recorder.seen) >= 2, (
        "the deferred quiet tail must still be reported repeatedly for "
        f"notifications={recorder.seen!r}"
    )
    # An idle age below the elapsed total is the signature of a reset: without
    # a stage's stderr counting as output no activity would ever be recorded,
    # so every report would name an idle age equal to the total. Only the
    # producer's stderr can have reset the clock here -- its stdout is
    # inter-stage and the consumer stays silent.
    #
    # Deliberately not a wall-clock threshold against the producer's tick
    # schedule: the parent's event loop stalls inside `create_subprocess_exec`
    # while it spawns the next stage, which can delay its reads of the
    # producer's stderr past the first deadline without changing what the
    # clock counted.
    total, idle = recorder.seen[-1]
    assert idle < total, (
        "a stage's stderr must defer the aggregate clock for "
        f"notifications={recorder.seen!r}"
    )


def test_a_shared_sink_keeps_the_pipeline_keepalive_on_its_own_line(
    python_catalogue_env: PythonCatalogue,
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """The final stage's stdout echo can strand the aggregate keepalive too.

    A caller may point both sinks at one object, and then the pipeline's own
    outward-facing stdout is written to the keepalive's destination. Echo is
    unbounded so each chunk lands as it arrives, leaving the cursor as the only
    guard against continuing the child's unfinished line.
    """
    sink = io.StringIO()
    pipeline = _two_stage(
        python,
        producer="print('upstream')",
        consumer=(
            "import sys, time;"
            "sys.stdout.write('partial-final');"
            "sys.stdout.flush();"
            "time.sleep(0.5);"
            "sys.stdout.write('\\n');"
            "sys.stdout.flush()"
        ),
    )

    with _allowlisted(python_catalogue_env):
        pipeline.run_sync(
            output=RunOutputOptions(
                echo=True,
                idle_after=_INTERVAL,
                max_echo_line_bytes=None,
            ),
            context=ExecutionContext(stdout_sink=sink, stderr_sink=sink),
        )

    lines = keepalives(sink)
    assert lines, f"the quiet tail must still be reported for {sink.getvalue()!r}"
    for line in lines:
        assert line.startswith("[cuprum]"), (
            "the aggregate keepalive must begin its own line rather than "
            f"continue the final stage's unfinished one: {line!r}"
        )
    assert sink.getvalue().startswith("partial-final\n"), (
        f"the stage's own bytes must be unchanged: {sink.getvalue()!r}"
    )


def test_pipeline_consumption_policy_follows_the_idle_gate() -> None:
    """The stage-stream policy counts a watchdog as a reason to drain."""
    quiet = _prepare_pipeline_config(
        output=RunOutputOptions(capture=False, echo=False),
        timeout=None,
        context=None,
    )
    watching = _prepare_pipeline_config(
        output=RunOutputOptions(capture=False, echo=False, idle_after=30.0),
        timeout=None,
        context=None,
    )

    assert (quiet.consumes_stdout, quiet.consumes_stderr) == (False, False), (
        "a pipeline without idle reporting must leave its streams unconsumed"
    )
    assert (watching.consumes_stdout, watching.consumes_stderr) == (True, True), (
        "a pipeline watching for silence must drain its stages"
    )
    assert watching.stream_config.activity is not None, (
        "the final stage's stdout must feed the aggregate clock"
    )
    assert watching.stderr_stream_config.activity is not None, (
        "every stage's stderr must feed the aggregate clock"
    )


def test_pipeline_fail_fast_leaves_no_watchdog_behind(
    python_catalogue_env: PythonCatalogue,
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """A failed pipeline settles its aggregate heartbeat like any other run."""
    sink = io.StringIO()
    pipeline = _two_stage(
        python,
        producer="import sys; sys.exit(3)",
        consumer="import sys, time; sys.stdin.read(); time.sleep(2)",
    )

    async def exercise() -> tuple[PipelineResult, list[asyncio.Task[object]]]:
        """Run a failing pipeline and survey the loop once it has settled."""
        with _allowlisted(python_catalogue_env):
            result = await pipeline.run(
                output=RunOutputOptions(idle_after=0.05),
                context=ExecutionContext(stderr_sink=sink),
            )
        return result, pending_tasks()

    result, leftover = asyncio.run(exercise())

    assert result.failure_index == 0, f"the first stage must fail: {result!r}"
    assert not leftover, f"the failed pipeline left tasks behind: {leftover!r}"
    assert "[cuprum]" not in (result.final.stdout or ""), (
        "the keepalive must not be reported as a stage's output"
    )


def test_concurrent_runs_own_separate_clocks_and_destinations(
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """One run's interval is not another run's, nor is one's sink."""
    watching_sink = io.StringIO()
    quiet_sink = io.StringIO()

    async def exercise() -> tuple[list[str], list[str], list[asyncio.Task[object]]]:
        """Run a short-interval and a long-interval command side by side."""
        # The second command outlives its own interval only if the first run's
        # watchdog is driving it; its sink staying empty is the isolation.
        short = python("-c", "import time; time.sleep(0.4)")
        patient = python("-c", "import time; time.sleep(0.3)")
        await asyncio.gather(
            short.run(
                output=RunOutputOptions(idle_after=0.05),
                context=ExecutionContext(stderr_sink=watching_sink),
            ),
            patient.run(
                output=RunOutputOptions(idle_after=1.5),
                context=ExecutionContext(stderr_sink=quiet_sink),
            ),
        )
        return keepalives(watching_sink), keepalives(quiet_sink), pending_tasks()

    watching, quiet, leftover = asyncio.run(exercise())

    assert watching, "the short-interval run must have been reported"
    assert not quiet, (
        "the long-interval run must not inherit a shorter neighbour's clock for "
        f"sink={quiet_sink.getvalue()!r}"
    )
    assert not leftover, f"the concurrent runs left tasks behind: {leftover!r}"


def _record(seen: list[tuple[float, float]]) -> cabc.Callable[[float, float], None]:
    """Return a callback appending each notification to *seen*."""

    def callback(elapsed_total: float, elapsed_idle: float) -> None:
        """Record one notification."""
        seen.append((elapsed_total, elapsed_idle))

    return callback


def test_a_run_nested_in_a_callback_has_its_own_watchdog(
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """A run started from another run's notification keeps its own clock."""
    outer_sink = io.StringIO()
    inner_sink = io.StringIO()
    outer_seen: list[tuple[float, float]] = []
    inner_seen: list[tuple[float, float]] = []
    inner: list[asyncio.Task[object]] = []

    async def inner_run() -> None:
        """Run a second, independently observed command."""
        await python("-c", "import time; time.sleep(0.3)").run(
            output=RunOutputOptions(idle_after=0.05, on_idle=_record(inner_seen)),
            context=ExecutionContext(stderr_sink=inner_sink),
        )

    def on_idle(elapsed_total: float, elapsed_idle: float) -> None:
        """Start the nested run the first time the outer one goes quiet."""
        outer_seen.append((elapsed_total, elapsed_idle))
        if not inner:
            inner.append(asyncio.ensure_future(inner_run()))

    async def exercise() -> list[asyncio.Task[object]]:
        """Run the outer command, wait for the nested one, then survey."""
        await python("-c", "import time; time.sleep(0.5)").run(
            output=RunOutputOptions(idle_after=0.08, on_idle=on_idle),
            context=ExecutionContext(stderr_sink=outer_sink),
        )
        await asyncio.gather(*inner)
        return pending_tasks()

    leftover = asyncio.run(exercise())

    assert len(outer_seen) >= 2, (
        "the outer run must go on reporting while the nested run holds the "
        f"loop: {outer_seen!r}"
    )
    assert inner_seen, "the nested run must have been reported to its own callback"
    assert not keepalives(outer_sink), (
        "a caller callback replaces the outer run's renderer for "
        f"sink={outer_sink.getvalue()!r}"
    )
    assert not keepalives(inner_sink), (
        "a caller callback replaces the nested run's renderer for "
        f"sink={inner_sink.getvalue()!r}"
    )
    assert not leftover, f"the nested run left tasks behind: {leftover!r}"


class _GroupSink:
    """A stderr sink that brackets everything written to it in a log group.

    Stands in for the GitHub Actions adapter (#360), whose contract is that the
    parent's own diagnostics land inside the group it opened for the run.
    """

    def __init__(self) -> None:
        """Open an empty group buffer."""
        self.lines: list[str] = []

    def write(self, payload: str) -> int:
        """Record the payload as it arrives, flush and all."""
        self.lines.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""

    def group_lines(self) -> list[str]:
        """Return the keepalives written inside the group.

        Returns
        -------
        list[str]
            Every recorded line the built-in renderer produced.
        """
        return [
            line
            for line in "".join(self.lines).splitlines()
            if line.startswith("[cuprum]")
        ]


def test_keepalive_stays_inside_the_callers_log_group(
    python: cabc.Callable[..., SafeCmd],
) -> None:
    """The #359/#360 regression: a diagnostic is not child output."""
    group = _GroupSink()
    command = python("-c", "import time; time.sleep(0.4); print('done')")

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(idle_after=0.05),
            context=ExecutionContext(stderr_sink=typ.cast("typ.IO[str]", group)),
        ),
    )

    lines = group.group_lines()
    # More than one line is the proof that the generated line stays outside the
    # activity tracker: a diagnostic that reset the clock would silence the run
    # it exists to keep legible.
    assert len(lines) >= 2, (
        f"the grouped keepalive must keep arriving inside the group: {group.lines!r}"
    )
    assert result.stdout == "done\n", f"capture must be unaffected: {result.stdout!r}"
    assert "[cuprum]" not in (result.stdout or ""), (
        "the grouped keepalive must not re-enter captured stdout"
    )
