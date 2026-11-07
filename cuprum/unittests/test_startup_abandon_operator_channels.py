"""A suppressed reclaim failure must reach every operator channel.

The reclaim never replaces the error the caller is unwinding, so a step that
fails inside it can only be seen if it is reported. That reporting has three
consumers — the ``cuprum.reclaim`` log record, the ``teardown_error`` observe
event, and the ``cuprum_teardown_errors_total`` counter — and each is fed by a
different seam. The reporter's own contract is covered elsewhere; this case
drives ``SafeCmd.run()`` so that the wiring from the run, through the reclaim,
to all three consumers is what fails when any one of them is broken.

The start hook is what fails the run, so the reclaim runs with no caller left
and a failure of its own is suppressed rather than raised. That is the whole
point of the suppression: the caller must still meet the hook's error.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_startup_abandon, scoped, sh
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.sh import RunOutputOptions
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    capturing_logger,
)
from cuprum.unittests._start_ownership_support import (
    _assert_strictly_reaped,
    _failing_start_hook,
    _python_catalogue,
    _StartHookError,
)
from cuprum.unittests._wait4_process_support import _abandon
from tests.helpers.timeouts import (
    child_argv,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("startup-operator-channels")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _teardown_counters(
    collector: _LabelRecordingCollector,
) -> list[dict[str, str]]:
    """Return the label sets recorded for the teardown-error counter."""
    return [
        labels
        for name, _value, labels in collector.calls
        if name == "cuprum_teardown_errors_total"
    ]


def test_a_failed_reclaim_step_is_reported_on_every_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """One suppressed step failure must reach the log, the event, and the metric.

    The discard is made to fail, which is the step a reset pipe fails in. The
    run is otherwise real: a child is spawned and the start hook raises, so the
    reclaim is reached by the same route production code takes. The caller must
    still meet the hook's error, because a failure this case injected is not
    the one that ended the run.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    collector = _LabelRecordingCollector()

    async def fail_discard(_process: object) -> None:
        """Fail the discard as a pipe reset would."""
        msg = "the pipe could not be read"
        await asyncio.sleep(0)
        raise OSError(msg)

    monkeypatch.setattr(
        _subprocess_startup_abandon,
        "_discard_outputs",
        fail_discard,
    )

    async def run_case() -> BaseException:
        """Run the command and return the failure the caller met."""
        with pytest.raises(_StartHookError) as caught:
            await command.run(output=RunOutputOptions())
        return typ.cast("BaseException", caught.value)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_failing_start_hook(events)),
        sh.observe(MetricsHook(collector)),
        capturing_logger("cuprum.reclaim") as capture,
    ):
        raised = asyncio.run(run_case())

    pids = started_pids(events)
    try:
        assert isinstance(raised, _StartHookError), (
            f"the hook's own error must reach the caller, got {raised!r}"
        )
        assert len(capture.records) == 1, (
            f"one suppressed step failure must produce one record, got "
            f"{capture.records!r}"
        )
        assert capture.records[0].levelno == logging.WARNING, (
            "a reclaim that could not finish a step is a warning, not noise"
        )
        assert vars(capture.records[0])["cuprum_operation"] == "output_discard", (
            "the record must attribute the failure to the step that raised"
        )
        teardown = [event for event in events if event.phase == "teardown_error"]
        assert [event.operation for event in teardown] == ["output_discard"], (
            f"the observe event must name the failed step, got {teardown!r}"
        )
        counters = _teardown_counters(collector)
        assert [labels["operation"] for labels in counters] == ["output_discard"], (
            "the counter must carry the same bounded operation as the other two "
            f"channels, got {collector.calls!r}"
        )
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a reported reclaim failure")
    finally:
        for pid in pids:
            _abandon(pid)
