"""What a settlement timeout records as the duration it waited.

The capped post-``SIGKILL`` group wait publishes a duration alongside the
``settlement_timeout`` outcome, and that figure reaches both an operator-facing
histogram and the log record. A duration read off the configured bound is
therefore a fixed value on every sample: the histogram would carry no
distribution at all, and the recorded figure would describe what the wait was
allowed to take rather than what it took.

The two are equal only in one case — a wait that runs its bound out to the last
instant — so the distinction is invisible unless the wait returns well before
its bound does. These tests stage exactly that: the group poll is replaced with
a double that gives up early while reporting members outstanding, which is the
answer :func:`~cuprum._process_group._await_group_settlement` produces when its
bound expires. The figure the teardown records must then be the double's own
short wait, which no bound can produce.

The bound is widened rather than the clock scripted, and that is deliberate.
Scripting ``time.monotonic`` would mean patching the ``time`` module itself,
which asyncio reads to schedule every timer it holds — the event loop's own
clock would stop advancing and the test would hang rather than fail. Widening
the bound instead keeps the real clock, so the recorded figure is genuinely
measured, and it removes any dependence on host load: a double that returns
after a fifth of a second cannot be mistaken for one that ran out an eight
second bound however busy the machine is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

from cuprum import _group_teardown, _process_group
from cuprum.unittests._process_signal_identity_support import (
    _LEADER_PID,
    _teardown,
    _TeardownTarget,
)

# The double's wait. The bound is widened far past it below, so the two figures
# differ by more than an order of magnitude and a bound leaked into the record
# cannot pass for a measurement.
_STUBBED_WAIT_S = 0.2

# Wide enough that no plausible scheduling delay turns the double's short
# return into a real expiry, so the assertion below reads a measurement rather
# than the cap it ran under. The teardown never actually waits this long.
_WIDENED_BOUND_S = 8.0

# The escalation only runs when the child outlives its grace period, so the
# grace here is short enough to reach it promptly and non-zero so the deadline
# actually suspends rather than settling the run on the first phase.
_GRACE_S = 0.05

_SETTLEMENT_LOGGER = _group_teardown.__name__

# ``report_group_teardown`` formats its template with a positional argument
# list, and the settlement-timeout template is written to agree with the tuple
# below. Reading the record's own arguments keeps this test on the value that
# was logged rather than on a re-parse of its rendered text.
_SETTLE_ARG = 2


def _settlement_timeout_args(caplog: pytest.LogCaptureFixture) -> tuple[object, ...]:
    """Return the one settlement-timeout record's log arguments, or fail loudly."""
    records = [
        record
        for record in caplog.records
        if "process_group_settlement_timeout" in record.getMessage()
    ]
    assert len(records) == 1, (
        "the expired wait must be reported exactly once, or the figure below "
        f"is read off the wrong record; got {caplog.messages}"
    )
    args = records[0].args
    assert isinstance(args, tuple), (
        f"the record must carry positional log arguments; got {args!r}"
    )
    return args


class _MembersOutstandingGroup:
    """A group poll that gives up early while reporting members left.

    ``MEMBERS`` is the answer the capped wrapper produces when its bound
    expires, so a double returning it models a wait that ran out rather than
    one that found the group empty — without the test having to wait out a real
    bound to reach that state.

    The signature matches the real exit poll's, because the teardown gathers its
    targets with ``return_exceptions=True`` and a wider one would raise into
    that gather, letting the test pass without the wait ever being reached.
    """

    async def __call__(self, pgid: int) -> _process_group._GroupProbe:
        """Report members outstanding after a wait far shorter than the bound."""
        del pgid
        await asyncio.sleep(_STUBBED_WAIT_S)
        return _process_group._GroupProbe.MEMBERS


def test_the_recorded_settlement_duration_is_the_measured_wait(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The duration recorded is the one the wait took, not the bound it ran under.

    A teardown that passed the configured bound to the report records the
    widened figure below and fails here, because the wait it is describing
    returned after a fifth of a second. That gap is the whole reason the poll
    is scripted to return early rather than to run out a real bound.
    """
    poll = _MembersOutstandingGroup()

    async def run_case() -> None:
        """Escalate against a group that will not settle, and read the record."""
        target = _TeardownTarget(
            is_group_live=True,
            has_exited=False,
            exits_on_sigterm=False,
        )
        # The double's pid is a plausible group name, so leaving the real kernel
        # call in place would signal whatever holds that number *on the host*
        # rather than exercising the teardown. Scripting it keeps the test's
        # signals inside the double, which is where they are meant to land.
        monkeypatch.setattr(os, "killpg", target.record_killpg)
        # The wrapper reads this as a module global when the capped wait opens,
        # so patching the module is what widens the cap that applies here.
        monkeypatch.setattr(_process_group, "_POST_KILL_SETTLEMENT_S", _WIDENED_BOUND_S)
        # Replacing the poll is what makes ``MEMBERS`` reachable without waiting
        # out a real bound; patching the capped wrapper instead would leave the
        # poll it wraps running and the test would not reach the state it names.
        monkeypatch.setattr(_process_group, "_await_group_exit", poll)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=_SETTLEMENT_LOGGER):
            delivered = await _teardown(
                target,
                owns_group=True,
            )
        assert delivered, (
            "the escalation must have been delivered, or the settlement wait "
            "this test observes was never reached"
        )
        assert time.monotonic() - started < _WIDENED_BOUND_S, (
            "the teardown must not have waited out the widened bound, or the "
            "figure it records is a real expiry and the assertion below reads "
            "the cap rather than the measurement"
        )

    asyncio.run(run_case())

    pid, owns_group, settle_s = _settlement_timeout_args(caplog)
    assert isinstance(settle_s, float), (
        f"the record must carry a duration, not {settle_s!r}"
    )
    assert settle_s == pytest.approx(_STUBBED_WAIT_S, abs=_STUBBED_WAIT_S), (
        "the record must carry the duration the wait ran, so a figure near the "
        f"stub's own wait is expected; got {settle_s!r}"
    )
    assert settle_s != pytest.approx(_WIDENED_BOUND_S), (
        "a figure equal to the configured bound is the defect this test exists "
        "to catch, since the histogram it feeds could then never vary"
    )
    assert pid == _LEADER_PID, f"the record must name the group it probed; got {pid!r}"
    assert owns_group is True, (
        f"the record must name the route it took; got {owns_group!r}"
    )
