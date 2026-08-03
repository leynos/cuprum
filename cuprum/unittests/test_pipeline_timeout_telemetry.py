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
        assert fields["cuprum_operation"] == "wait"
        assert fields["cuprum_timeout_s"] == pytest.approx(0.2)
        assert fields["cuprum_timeout_mode"] == "elapsed_deadline"
        assert fields["cuprum_error_type"] == "TimeoutError"


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
