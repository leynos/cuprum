"""A pipeline deadline expiry must be as observable as a single-command one.

A pipeline enforces its deadline once for the whole run rather than per stage,
so it never reaches the single-command reporting call in
``_wait_for_exit_code_within_timeout``. These tests pin the pipeline path onto
the same three channels a single-command expiry uses — the ``timeout`` observe
event, the ``cuprum.timeout`` log record, and the metrics counter — so the
user-facing contract that *every* expiry is reported holds for pipelines too.

The single-command counterparts live in ``test_subprocess_timeout_observe`` and
``test_subprocess_timeout_logging``.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

from cuprum import Program, ScopeConfig, TimeoutExpired, scoped, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

_TIMEOUT_LOGGER = "cuprum.timeout"
_STAGE_COUNT = 2


def _sleeping_pipeline() -> tuple[sh.Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline whose first stage outlives any test deadline."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = python("-c", "import time; time.sleep(30)") | python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )
    return pipeline, frozenset([python_program])


def _run_until_timeout(
    timeout: float,
    events: list[ExecEvent],
    metrics: InMemoryMetrics,
) -> None:
    """Run a pipeline that must time out, collecting events and metrics."""
    pipeline, allowlist = _sleeping_pipeline()

    def collect(ev: ExecEvent) -> None:
        """Record every observe event the run emits."""
        events.append(ev)

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        sh.observe(collect),
        sh.observe(MetricsHook(metrics)),
        pytest.raises(TimeoutExpired),
    ):
        pipeline.run_sync(timeout=timeout, output=RunOutputOptions(capture=False))


@pytest.mark.parametrize(
    ("timeout", "expected_mode"),
    [(0.2, "elapsed_deadline"), (0, "non_positive_immediate")],
)
def test_pipeline_timeout_emits_timeout_events(
    timeout: float, expected_mode: str
) -> None:
    """A pipeline expiry emits one ``timeout`` event per stage, tagged by mode.

    Pipeline lifecycle events are per stage, so the timeout event is too: each
    stage reports its own pid. ``timeout_mode`` distinguishes an elapsed
    deadline from the immediate expiry a non-positive timeout takes, exactly as
    it does for a single command.
    """
    events: list[ExecEvent] = []
    _run_until_timeout(timeout, events, InMemoryMetrics())

    timeout_events = [ev for ev in events if ev.phase == "timeout"]
    assert len(timeout_events) == _STAGE_COUNT, (
        f"a {_STAGE_COUNT}-stage pipeline must report one timeout event per "
        f"stage, got {[(ev.phase, ev.pid) for ev in events]}"
    )
    for ev in timeout_events:
        assert ev.operation == "wait", (
            f"the timeout event must name the wait operation, got {ev.operation!r}"
        )
        assert ev.error_type == "TimeoutError", (
            f"the timeout event must name TimeoutError, got {ev.error_type!r}"
        )
        assert ev.timeout_s == timeout, (
            f"the timeout event must carry the configured timeout {timeout!r}, "
            f"got {ev.timeout_s!r}"
        )
        assert ev.timeout_mode == expected_mode, (
            f"a timeout of {timeout!r} must report mode {expected_mode!r}, got "
            f"{ev.timeout_mode!r}"
        )
        assert ev.pid is not None, "each stage's timeout event must carry its pid"

    assert {ev.pid for ev in timeout_events} == {
        ev.pid for ev in events if ev.phase == "start"
    }, "the timeout events must cover exactly the stages that started"


def test_pipeline_timeout_writes_log_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pipeline expiry reaches the ``cuprum.timeout`` logger.

    This is the channel an operator sees with no observe hook registered, so a
    pipeline expiry must not be silent on it.
    """
    events: list[ExecEvent] = []
    with caplog.at_level(logging.WARNING, logger=_TIMEOUT_LOGGER):
        _run_until_timeout(0.2, events, InMemoryMetrics())

    records = [rec for rec in caplog.records if rec.name == _TIMEOUT_LOGGER]
    assert len(records) == _STAGE_COUNT, (
        f"a {_STAGE_COUNT}-stage pipeline must log one expiry record per stage, "
        f"got {[rec.getMessage() for rec in records]}"
    )
    for rec in records:
        fields = vars(rec)
        assert fields["cuprum_operation"] == "wait", (
            "the expiry record must name the wait operation, got "
            f"{fields['cuprum_operation']!r}"
        )
        assert fields["cuprum_timeout_s"] == pytest.approx(0.2), (
            "the expiry record must carry the configured timeout, got "
            f"{fields['cuprum_timeout_s']!r}"
        )
        assert fields["cuprum_timeout_mode"] == "elapsed_deadline", (
            "a positive deadline that ran out must be reported as an elapsed "
            f"deadline, got {fields['cuprum_timeout_mode']!r}"
        )
        assert fields["cuprum_error_type"] == "TimeoutError", (
            "the expiry record must name the raised error class, got "
            f"{fields['cuprum_error_type']!r}"
        )


def test_pipeline_timeout_increments_metrics_counter() -> None:
    """A pipeline expiry increments ``cuprum_timeouts_total``.

    Without this the counter would under-report, reading as though pipelines
    never time out.
    """
    metrics = InMemoryMetrics()
    _run_until_timeout(0.2, [], metrics)

    assert metrics.counters.get("cuprum_timeouts_total") == _STAGE_COUNT, (
        f"each timed-out stage must increment the counter, got {metrics.counters!r}"
    )


def test_pipeline_timeout_expired_still_reaches_the_caller() -> None:
    """Telemetry must not displace the ``TimeoutExpired`` a caller catches.

    Reporting runs while ``TimeoutExpired`` is already propagating, so a failure
    there would replace the exception the caller is waiting for. ``pytest.raises``
    inside the helper asserts the type; this pins the payload as well.
    """
    pipeline, allowlist = _sleeping_pipeline()
    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        pytest.raises(TimeoutExpired) as exc_info,
    ):
        pipeline.run_sync(timeout=0.2, output=RunOutputOptions(capture=False))

    assert exc_info.value.timeout == pytest.approx(0.2), (
        "the raised TimeoutExpired must still carry the configured timeout"
    )


def test_pipeline_timeout_emits_terminal_exit_events() -> None:
    """Every reaped stage still reports a terminal ``exit`` event.

    The success path emits these while assembling stage results, which a
    timeout never reaches. Without an explicit emission the stage would report
    a ``timeout`` and then go silent — and, because ``TracingHook`` ends a span
    only on ``exit``, its span would stay open for the tracer's lifetime.
    """
    events: list[ExecEvent] = []
    _run_until_timeout(0.2, events, InMemoryMetrics())

    exits = [ev for ev in events if ev.phase == "exit"]
    assert len(exits) == _STAGE_COUNT, (
        f"each of the {_STAGE_COUNT} stages must report a terminal exit event, "
        f"got {[(ev.phase, ev.pid) for ev in events]}"
    )
    assert {ev.pid for ev in exits} == {
        ev.pid for ev in events if ev.phase == "start"
    }, "the exit events must cover exactly the stages that started"

    # Per stage, not globally: `phases.index` reports only the *first*
    # occurrence in the whole stream, so with two stages it would compare one
    # stage's timeout against the other's exit and accept an interleaving in
    # which a stage reported its exit before its own timeout.
    by_pid: dict[int | None, list[str]] = {}
    for event in events:
        by_pid.setdefault(event.pid, []).append(event.phase)

    timed_out = [(pid, phases) for pid, phases in by_pid.items() if "timeout" in phases]
    assert timed_out, f"at least one stage must report a timeout, got {by_pid}"
    for pid, phases in timed_out:
        assert "exit" in phases, (
            f"stage pid={pid} reported a timeout but no exit, got {phases}"
        )
        assert phases.index("timeout") < phases.index("exit"), (
            f"stage pid={pid} must report its timeout before its own exit, got {phases}"
        )


def test_exit_hook_failure_cannot_mask_the_timeout() -> None:
    """A hook raising on ``exit`` must not displace ``TimeoutExpired``.

    The terminal ``exit`` events are emitted from inside the runner's
    ``except TimeoutExpired`` handler. ``_StageObservation.emit`` re-raises a
    synchronous observe-hook failure, so without a per-stage guard the first
    stage's failing hook would both replace the ``TimeoutExpired`` the caller
    is owed and abandon the remaining stages — stranding the open spans the
    exit events exist to close.
    """
    pipeline, allowlist = _sleeping_pipeline()
    seen: list[ExecEvent] = []
    tracer = InMemoryTracer()

    def failing_on_exit(ev: ExecEvent) -> None:
        """Record every event, then fail on each ``exit``."""
        seen.append(ev)
        if ev.phase == "exit":
            msg = "exit hook boom"
            raise RuntimeError(msg)

    # The tracing hook is registered first deliberately. ``_emit_exec_event``
    # abandons the hook chain at the first failure, so a hook ordered ahead of
    # the tracer would stop it seeing the event at all — cross-hook isolation
    # the library does not offer and this test is not about. What is under test
    # is per-*stage* isolation: stage two must still emit after stage one's
    # hook raised.
    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        sh.observe(TracingHook(tracer)),
        sh.observe(failing_on_exit),
        pytest.raises(TimeoutExpired) as exc_info,
    ):
        pipeline.run_sync(timeout=0.2, output=RunOutputOptions(capture=False))

    assert exc_info.value.timeout == pytest.approx(0.2), (
        "the hook failure must not replace the TimeoutExpired the caller is owed"
    )
    exits = [ev for ev in seen if ev.phase == "exit"]
    assert len(exits) == _STAGE_COUNT, (
        f"every stage must still emit exit after an earlier stage's hook "
        f"failed, got {[(ev.phase, ev.pid) for ev in seen]}"
    )
    open_spans = [span.name for span in tracer.spans if not span.ended]
    assert tracer.spans, "the tracing hook must have opened a span per stage"
    assert not open_spans, (
        "a failing hook on one stage must not strand another stage's span, "
        f"got open spans {open_spans}"
    )


def test_pipeline_timeout_closes_tracing_spans() -> None:
    """A timed-out pipeline leaves no span open in the tracer.

    This is what the terminal ``exit`` event buys: ancillary phases record a
    span event and deliberately leave the span open, so only ``exit`` can end
    it. A pipeline that timed out previously stranded one span per stage.
    """
    tracer = InMemoryTracer()
    pipeline, allowlist = _sleeping_pipeline()

    with (
        scoped(ScopeConfig(allowlist=allowlist)),
        sh.observe(TracingHook(tracer)),
        pytest.raises(TimeoutExpired),
    ):
        pipeline.run_sync(timeout=0.2, output=RunOutputOptions(capture=False))

    assert len(tracer.spans) == _STAGE_COUNT, (
        f"expected one span per stage, got {len(tracer.spans)}"
    )
    unfinished = [span for span in tracer.spans if not span.ended]
    assert not unfinished, (
        f"a timed-out pipeline must end every span it opened, {len(unfinished)} "
        "were left open"
    )
