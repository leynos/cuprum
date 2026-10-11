"""What teardown reports when a signal cannot be delivered.

A teardown signal has two ways to fail to arrive, and the caller cannot tell
them apart: both surface as the same ``False``, meaning "there was nothing left
to do here". The distinction is not cosmetic. ``ProcessLookupError`` is the
race both signal routes already tolerate, so passing it over silently is
correct. A wider ``OSError`` is a target this run believes is still there and
could not signal, and passing *that* over silently turns a failed teardown into
an orderly one — the fail-fast path counts a stage whose termination returned
``False`` as a stage that had already settled, so the failure would be counted
and trended as a clean outcome.

These tests pin the reporting that separates the two. The teardown is the real
one, driven through the same entry the identity tests use; only the kernel call
is scripted, and it is scripted to fail for a named signal so both call sites
of the reporting wrapper are exercised rather than only the first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import pytest

from cuprum import _process_signal
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
# cannot leave these tests watching a logger nothing writes to.
_SIGNAL_LOGGER = _process_signal.__name__


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

    assert handled is False, "a target that went settles nothing"
    assert _records(caplog) == [], (
        "the tolerated race must not be reported as a failure, or every "
        f"teardown of a fast-exiting group would be noise; got {caplog.messages}"
    )


@pytest.mark.parametrize("refused", [signal.SIGTERM, signal.SIGKILL])
def test_a_signal_the_kernel_refuses_is_recorded_and_reported(
    refused: signal.Signals,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused signal is recorded, and still reported as nothing done.

    The child ignores ``SIGTERM``, so refusing the graceful signal and refusing
    the escalation are separate reaches into the code under test: the first is
    refused before the grace period begins, the second only once the grace has
    expired and the escalation has been considered. Both have to be recorded,
    because the caller receives the same ``False`` either way.

    The record must name the signal, because the two states it distinguishes
    are very different: the first leaves a process that was never asked to
    leave, the second leaves one that was asked and did not. The return value
    is asserted too — reporting must not *replace* the outcome, or a caller
    branching on it would change behaviour.
    """
    kernel = _RefusingKernel(
        refused,
        OSError(1, "Operation not permitted"),
        exits_on_sigterm=False,
    )
    monkeypatch.setattr(os, "killpg", kernel.killpg)

    with caplog.at_level(logging.WARNING, logger=_SIGNAL_LOGGER):
        handled = asyncio.run(_teardown(kernel.target, owns_group=True))

    assert handled is False, (
        "a signal that could not be delivered is not a completed teardown"
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

    assert handled is True, "the child accepts the signal and the run settles"
    assert _records(caplog) == [], (
        "a teardown whose signals arrive must record nothing at warning level; "
        f"got {caplog.messages}"
    )
