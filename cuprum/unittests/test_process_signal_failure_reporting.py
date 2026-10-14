"""What teardown reports when a signal cannot be delivered.

A teardown signal has two ways to fail to arrive, and the stop helpers cannot
tell them apart: both reach them as the same ``ProcessLookupError``. The
distinction is not cosmetic. That error is the race both signal routes already
tolerate, so passing it over silently is correct. A wider ``OSError`` is a
target this run believes is still there and could not signal, and passing
*that* over silently conflates two different outcomes — a teardown that found
nothing left to do, and a teardown that failed. Neither is signalled by the
stop helpers' own return, so the record is the only thing that separates them.

What the record does *not* do is change any caller's arithmetic. The fail-fast
path counts teardown outcomes by ``waiter_completed``, so a refusal has always
contributed zero there — the same as absence. Reporting adds diagnosis, not a
corrected tally.

These tests pin the reporting that separates the two. The teardown is the real
one, driven through the same entry the identity tests use; only the kernel call
is scripted, and it is scripted to fail for a named signal so both call sites
of the reporting wrapper are exercised rather than only the first. The stop
helpers report the delivery facts they observed and not the refusal — only the
handle, which can read the child's own status, can weigh one against the other.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import os
import signal
import sys

import pytest

from cuprum import _group_teardown
from cuprum.unittests._process_signal_identity_support import (
    _teardown,
    _TeardownTarget,
)

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# Resolved from the module rather than spelled out, so a rename of the module
# cannot leave these tests watching a logger nothing writes to. It is the
# reporting module, not ``_process_signal``: the decision to signal belongs to
# the latter, but every record these tests read is written by
# ``_group_teardown.report_group_teardown``, and binding the logger to the
# deciding module would leave them capturing nothing while still passing the
# assertions that only require *no* record.
_SIGNAL_LOGGER = _group_teardown.__name__


@dc.dataclass(frozen=True, slots=True)
class _ExpectedOutcome:
    """The three facts a refused teardown must report for one case.

    They travel together because they are one outcome, not three independent
    expectations: which signals reached the child, whether the escalation was
    tried, and whether any reaping wait returned. Carrying them as a record
    keeps the parametrisation table readable and stops the case list from
    growing an argument per fact.
    """

    delivered: bool
    escalated: bool
    waiter_completed: bool


class _RefusingKernel:
    """A ``killpg`` that answers probes and refuses one named signal.

    The existence probe has to keep working: it is what tells the teardown the
    group is worth signalling at all, so a double that refused it would stop
    the run before the signal under test was ever reached. Delivery is recorded
    for every other signal, so a test can assert what the teardown did *and*
    what it could not do.
    """

    def __init__(
        self,
        refuses: signal.Signals | None,
        fault: OSError | None,
        *,
        exits_on_sigterm: bool,
    ) -> None:
        """Record the refusal and the state of the group behind it.

        ``fault`` is ``None`` for the control case, which refuses nothing: there
        is no fault to raise, and inventing one would only be reachable if the
        test were already wrong.
        """
        self.refuses = refuses
        self.fault = fault
        self.target = _TeardownTarget(
            is_group_live=True,
            has_exited=False,
            exits_on_sigterm=exits_on_sigterm,
        )

    def killpg(self, pgid: int, sig: int) -> None:
        """Answer probes, refuse *refuses*, and deliver anything else."""
        if self.refuses is not None and sig == self.refuses:
            assert self.fault is not None, (
                "a refusal needs a fault to raise; the control case must not "
                "name a signal to refuse"
            )
            raise self.fault
        self.target.record_killpg(pgid, sig)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the records these tests care about, in the order they arrived."""
    return [record for record in caplog.records if record.name == _SIGNAL_LOGGER]


def test_a_group_that_went_before_the_signal_is_passed_over_silently(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The benign race stays quiet, because nothing was left un-signalled.

    The target going between the probe and the signal is the one failure the
    teardown already tolerates by design: it is the probe's veto arriving a
    moment late, and the outcome it reports — nothing left to do — is exactly
    what happened. Recording it would make every ordinary teardown of a
    fast-exiting group noisy, which is the failure mode this test exists to
    prevent.
    """
    kernel = _RefusingKernel(
        signal.SIGTERM,
        ProcessLookupError(1, "No such process"),
        exits_on_sigterm=False,
    )
    monkeypatch.setattr(os, "killpg", kernel.killpg)

    with caplog.at_level(logging.WARNING, logger=_SIGNAL_LOGGER):
        handled = asyncio.run(_teardown(kernel.target, owns_group=True))

    assert handled.delivered is False, "a target that went settles nothing"
    assert _records(caplog) == [], (
        "the tolerated race must not be reported as a failure, or every "
        f"teardown of a fast-exiting group would be noise; got {caplog.messages}"
    )


@pytest.mark.parametrize(
    ("refused", "expected"),
    [
        (signal.SIGTERM, _ExpectedOutcome(False, False, False)),
        (signal.SIGKILL, _ExpectedOutcome(True, True, False)),
    ],
    ids=["the-graceful-signal", "the-escalation"],
)
def test_a_signal_the_kernel_refuses_is_recorded_and_reported(
    refused: signal.Signals,
    expected: _ExpectedOutcome,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused signal is recorded, and the outcome still says what happened.

    The child ignores ``SIGTERM``, so refusing the graceful signal and refusing
    the escalation are separate reaches into the code under test: the first is
    refused before the grace period begins, the second only once the grace has
    expired and the escalation has been considered.

    Both have to be recorded, and neither may be flattened onto the other. The
    two refusals leave the child in genuinely different states and the outcome
    carries that difference: a refused graceful signal means nothing ever
    reached the child, while a refused escalation means the child was asked to
    leave, ignored it, and outlived the window. This is why ``delivered`` alone
    is not the assertion — it is ``True`` for the second refusal, and a shared
    expectation would have to call that either a completed teardown or a
    failed one. Reporting must not *replace* the outcome either, or a caller
    branching on it would read a refused graceful signal as an escalated
    ending.

    The record must name the signal, because the two states it distinguishes
    are very different: the first leaves a process that was never asked to
    leave, the second leaves one that was asked and did not.
    """
    kernel = _RefusingKernel(
        refused,
        OSError(1, "Operation not permitted"),
        exits_on_sigterm=False,
    )
    monkeypatch.setattr(os, "killpg", kernel.killpg)

    with caplog.at_level(logging.WARNING, logger=_SIGNAL_LOGGER):
        handled = asyncio.run(_teardown(kernel.target, owns_group=True))

    assert handled.delivered is expected.delivered, (
        "the graceful signal is the only one that decides whether the child "
        f"was ever reached; got {handled!r}"
    )
    assert handled.escalated is expected.escalated, (
        "only a refusal of the escalation can leave the grace window closed "
        f"with the child still running; got {handled!r}"
    )
    assert handled.waiter_completed is expected.waiter_completed, (
        "a refused signal never reaches a reaping wait on either route, so no "
        f"wait can be reported as returned; got {handled!r}"
    )
    refused_records = [
        record
        for record in _records(caplog)
        if "cuprum.teardown_signal_failed" in record.getMessage()
    ]
    assert len(refused_records) == 1, (
        "a refused signal must be recorded exactly once, or a reader tallying "
        f"failed teardowns cannot count them; got {caplog.messages}"
    )
    assert f"signal={int(refused)}" in refused_records[0].getMessage(), (
        "the record must name the signal the kernel refused, so a refused "
        "graceful signal is distinguishable from a refused escalation: "
        f"{refused_records[0].getMessage()!r}"
    )


def test_a_refused_direct_signal_is_recorded_like_a_refused_group_signal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The direct route reports through the same wrapper as the group route.

    The other cases here script ``os.killpg`` under ``owns_group=True``, so they
    reach only the group route. The wrapper is shared by both, and the direct
    route is not a lesser caller: a run that inherits its group is the default,
    so a refusal there is the commoner of the two and must not be silent. This
    drives the same teardown with ``owns_group=False`` and refuses
    ``process.terminate``, which is the ``direct`` callable the graceful signal
    passes in.

    The refusal is deliberately *not* a ``ProcessLookupError``. On the direct
    route that error is suppressed inside ``Popen.send_signal`` (bpo-40550)
    before it can reach this wrapper, so scripting it here would test a state
    the real route cannot produce.

    The refusal alone would not prove which route was taken, since a group
    signal can fail the same way. Scripting ``os.killpg`` as well is what makes
    the route itself observable: the group name the double would be signalled
    under is a real pid, so an implementation that took the group route for an
    inheriting run would reach the host kernel rather than fail here.
    """

    def refuse() -> None:
        """Record that this route was taken, then refuse it as a kernel would."""
        attempted.append("terminate")
        raise OSError(1, "Operation not permitted")

    attempted: list[str] = []
    target = _TeardownTarget(
        is_group_live=True,
        has_exited=False,
        exits_on_sigterm=False,
    )
    monkeypatch.setattr(target, "terminate", refuse)
    # The group route must be reachable enough to be *detected*, but must never
    # reach the real kernel: this test's pid is a plausible group name, so an
    # implementation that wrongly took the group route would signal whatever
    # holds it. Recording the attempt is what the assertions below read.
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    with caplog.at_level(logging.WARNING, logger=_SIGNAL_LOGGER):
        handled = asyncio.run(_teardown(target, owns_group=False))

    assert handled.delivered is False, (
        "a direct signal that could not be delivered is not a completed teardown"
    )
    assert attempted == ["terminate"], (
        "the direct callable is the route an inheriting run takes, and the "
        f"graceful phase reaches it exactly once; got {attempted}"
    )
    # Group ownership, not mechanism, decides the route: recording through the
    # real ``killpg`` name would satisfy the assertion above too, so this is
    # what separates the two routes.
    assert target.group_signals == [], (
        "a run that does not own the group must not signal it, or an "
        f"inheriting run would reach processes it never spawned; got "
        f"{target.group_signals}"
    )
    refused_records = [
        record
        for record in _records(caplog)
        if "cuprum.teardown_signal_failed" in record.getMessage()
    ]
    assert len(refused_records) == 1, (
        "the shared wrapper must report a refused direct signal too, or the "
        f"default policy's failures go unrecorded; got {caplog.messages}"
    )
    assert "signal=15" in refused_records[0].getMessage(), (
        "the record must name the graceful signal this route refused: "
        f"{refused_records[0].getMessage()!r}"
    )


def test_a_teardown_whose_signals_arrive_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Control: an ordinary teardown stays silent at warning level.

    Without this, a recording that fired on every teardown would satisfy the
    tests above while making the log useless. The child accepts ``SIGTERM``, so
    the run settles on the grace phase and the escalation is never considered —
    the same shape as a refused graceful signal, differing only in whether the
    signal arrived.
    """
    kernel = _RefusingKernel(None, None, exits_on_sigterm=True)
    monkeypatch.setattr(os, "killpg", kernel.killpg)

    with caplog.at_level(logging.WARNING, logger=_SIGNAL_LOGGER):
        handled = asyncio.run(_teardown(kernel.target, owns_group=True))

    assert handled.delivered is True, "the child accepts the signal and the run settles"
    assert _records(caplog) == [], (
        "a teardown whose signals arrive must record nothing at warning level; "
        f"got {caplog.messages}"
    )
