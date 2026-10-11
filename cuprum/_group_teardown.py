"""Report the process-group teardowns that ended without settling.

An owning teardown has three ways to end with members still running, and an
operator has to be able to tell them apart. The group can hold members this
process may not signal, so no signal this run sends will ever reach them. The
capped post-``SIGKILL`` wait can expire with members outstanding, which is a
group the run could not reap rather than one it could not reach. Or the kernel
can refuse a signal outright with an ``OSError`` wider than the tolerated race,
so a target this run believes is running was never signalled at all. Each
leaves a process alive after its run has ended, and none of them is visible in
an exit code, a timeout, or a cancellation: the run looks finished either way.

All three are reported here, so a caller cannot report through one channel and
omit the other. Each report is emitted twice, through channels that answer
different questions. The diagnostic log record is what a developer reads while
debugging one run. The ``group_teardown`` execution event is what the metrics
and tracing adapters consume, so a teardown that left a process behind becomes
a countable, alertable series rather than a line in a log nobody is tailing.
Without the event, a failed settlement and an ordinary cancellation reach the
aggregate as the same terminal outcome.

The event carries an outcome category rather than the record's prose, so the
counter it feeds is labelled with a bounded value and no pid, error string, or
path can reach a metric label. The measured settle duration rides along only on
the outcome that took one, for the same reason a histogram observes only where
a figure was actually measured.

Reporting is best-effort. A teardown outlives the run that asked for it, so an
observe hook that raises must not displace the ``CancelledError`` or
``TimeoutError`` already propagating, nor abort the teardown itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import logging
import os
import signal
import types
import typing as typ

from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_group import _GroupProbe
from cuprum.events import GroupTeardownOutcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER = logging.getLogger(__name__)

# The operation each outcome is filed under, and the value the event's
# ``operation`` field carries. A refusal names the signal phase, because the
# two refusals are different faults: one leaves a process that was never asked
# to leave, the other one that was asked and did not. The value is built from
# the signal number rather than from the outcome alone, so a reader of the
# event can tell the two refusals apart without a second field to correlate.
_SIGNAL_OPERATION = "teardown_signal_%s"
_SETTLEMENT_OPERATION = "teardown_settlement"


@dc.dataclass(frozen=True, slots=True)
class GroupTeardownReport:
    """One teardown outcome, with what a reader needs to place it.

    Grouped rather than passed as loose keywords so the reporter's signature
    stays fixed as fields are added, and so a caller names the outcome's
    meaning where it is known rather than at the point of reporting.
    """

    outcome: GroupTeardownOutcome
    #: The command that was running when the group was left behind. It doubles
    #: as the signal phase: a refusal during the grace period and one during
    #: the ``SIGKILL`` escalation are different faults, and this separates them.
    operation: str
    pid: int | None
    owns_group: bool
    #: How long the wait had run when it expired, on the one outcome that
    #: measured a wait. ``None`` for the outcomes that observed none, so an
    #: absent figure is not read as an instant one.
    settle_s: float | None = None
    #: The signal the kernel refused, on that outcome alone. Carried so the
    #: record names it: the caller receives the same ``False`` whichever
    #: signal was refused, so the record is the only place the two states can
    #: be told apart.
    signal_number: int | None = None

    @classmethod
    def signal_refused(
        cls,
        *,
        pid: int | None,
        owns_group: bool,
        signal_number: int,
    ) -> GroupTeardownReport:
        """Describe a signal the kernel refused to deliver.

        A classmethod rather than a module-level function so the report stays
        one value with one constructor surface: every outcome is built through
        a named constructor that states which outcome it is, and a caller
        cannot assemble an outcome from fields that do not belong together.

        Returns
        -------
        GroupTeardownReport
            The outcome a refused signal is filed under, naming the signal.
        """
        return cls(
            outcome=GroupTeardownOutcome.SIGNAL_REFUSED,
            operation=_refusal_operation(signal_number),
            pid=pid,
            owns_group=owns_group,
            signal_number=signal_number,
        )

    @classmethod
    def settlement_timeout(
        cls,
        pid: int | None,
        *,
        settle_s: float,
    ) -> GroupTeardownReport:
        """Describe a capped wait that expired with members outstanding.

        *settle_s* is the duration the wait ran, measured by the caller that
        waited rather than read from the bound that capped it. The two are
        equal only when the wait runs its bound out to the last instant, so
        the record describes what happened instead of what was configured.

        Returns
        -------
        GroupTeardownReport
            The outcome a capped wait that expired with members outstanding
            reports under.
        """
        return cls(
            outcome=GroupTeardownOutcome.SETTLEMENT_TIMEOUT,
            operation=_SETTLEMENT_OPERATION,
            pid=pid,
            owns_group=True,
            settle_s=settle_s,
        )


def _refusal_operation(signal_number: int) -> str:
    """Name the signal phase a refusal belongs to, for the event's operation.

    Built from the number rather than from a signal name so the value cannot
    depend on which names this platform's ``signal`` module happens to define:
    the event contract is a stable string an operator filters on, and it must
    be the same one on every platform that can reach this code.

    Returns
    -------
    str
        The operation the events of a refusal at this phase are filed under.
    """
    return _SIGNAL_OPERATION % signal_number


def unsignalable_report(
    value: _GroupProbe,
    pid: int | None,
) -> GroupTeardownReport | None:
    """Describe a group the teardown could not signal, or ``None``.

    Only the unsignalable answer is a failure. ``EMPTY`` is the teardown
    succeeding. ``MEMBERS`` is a live group that has not settled *yet*: the
    caller is mid-escalation and a later ask may still succeed, so reporting it
    would make a normal teardown indistinguishable from a failed one.

    Returns
    -------
    GroupTeardownReport | None
        The unreachable-group outcome for an unsignalable probe, and ``None``
        for every answer that is not a failure.
    """
    if value is not _GroupProbe.UNSIGNALABLE:
        return None
    return GroupTeardownReport(
        outcome=GroupTeardownOutcome.UNSIGNALABLE,
        operation=_SETTLEMENT_OPERATION,
        pid=pid,
        owns_group=True,
    )


# The message each outcome logs, and the wording that makes the distinction the
# enum documents legible to a reader who has not read the enum. Held together
# because the two must agree: a message describing a different outcome than the
# one it is filed under would misdirect exactly the reader this record exists
# for.
_MESSAGES: cabc.Mapping[GroupTeardownOutcome, str] = types.MappingProxyType({
    GroupTeardownOutcome.UNSIGNALABLE: (
        "cuprum.process_group_unsignalable pid=%s owns_group=%s; group not "
        "signalled, treated as unreachable rather than settled"
    ),
    GroupTeardownOutcome.SETTLEMENT_TIMEOUT: (
        "cuprum.process_group_settlement_timeout pid=%s owns_group=%s "
        "settle_s=%s; the group still holds members this run cannot reap"
    ),
    GroupTeardownOutcome.SIGNAL_REFUSED: (
        "cuprum.teardown_signal_failed pid=%s signal=%s owns_group=%s error=%r; "
        "the target was not signalled, so a process this run believes is "
        "running may have outlived its teardown"
    ),
})


def _log_args(
    report: GroupTeardownReport,
    error: OSError | None,
) -> tuple[object, ...]:
    """Return the log arguments *report*'s message template consumes.

    The arguments are positional because that is what the ``%``-style logging
    templates take; each outcome's template and its argument list are written
    to agree, and the outcome selects both.

    Returns
    -------
    tuple[object, ...]
        The positional arguments that outcome's template ``%``-formats.
    """
    match report.outcome:
        case GroupTeardownOutcome.SIGNAL_REFUSED:
            return (report.pid, report.signal_number, report.owns_group, error)
        case GroupTeardownOutcome.SETTLEMENT_TIMEOUT:
            return (report.pid, report.owns_group, report.settle_s)
        case _:
            return (report.pid, report.owns_group)


def _emit_event(
    observation: _StageObservation | None,
    report: GroupTeardownReport,
) -> None:
    """Publish the outcome as an execution event, best-effort.

    An absent observation means this route has no execution to correlate the
    outcome with — the spawn-failure and abandoned-run paths reach teardown
    without one — so the event is skipped rather than invented against a
    fabricated identity. Those runs keep the log record, which is why the two
    channels are emitted together rather than the event replacing the record.

    A hook failure is swallowed for the reason the module docstring gives:
    teardown is already unwinding a cancellation or a timeout.
    """
    if observation is None:
        return
    details = _EventDetails(
        pid=report.pid,
        operation=report.operation,
        owns_group=report.owns_group,
        group_outcome=report.outcome,
        settle_s=report.settle_s,
    )
    with contextlib.suppress(Exception, asyncio.CancelledError):
        observation.emit("group_teardown", details)


def report_group_teardown(
    observation: _StageObservation | None,
    report: GroupTeardownReport,
    error: OSError | None = None,
) -> None:
    """Log one group-teardown outcome and publish it as an execution event.

    The two channels travel together so a caller cannot report through one and
    omit the other, which is the failure this module exists to prevent: a
    record nobody sees, or a metric with no diagnostic beside it.

    *error* is the kernel's refusal, carried only by the outcome that has one.
    Nothing raises from here: every caller is already unwinding a run that has
    ended, and an observability failure must not displace the error that ended
    it.
    """
    _LOGGER.warning(_MESSAGES[report.outcome], *_log_args(report, error))
    _emit_event(observation, report)


def signal_target_or_report(
    process: asyncio.subprocess.Process,
    *,
    observation: _StageObservation | None,
    owns_group: bool,
    group_signal: int,
) -> bool:
    """Signal the run's teardown target, reporting what cannot report itself.

    One function rather than a delivery helper plus a wrapper: the syscall's
    answer and its interpretation are one decision, and this is the only place
    the kernel's refusal is observed, so the tolerance and the report share a
    frame rather than needing two that agree on which exception means what.

    *observation* is the execution this signal acts for, and it is a single
    value rather than a sequence because a signal has exactly one target: the
    caller pairs it with the target it is about to signal, so a pipeline
    teardown reaching stage two reports against stage two rather than against
    whichever execution happened to be first. ``None`` is the routes with no
    execution to correlate with, which keep the log record alone.

    *owns_group* is true only for a child spawned with
    ``start_new_session=True``, which makes it the leader of its own session and
    group. The group is therefore named by the child's own process identifier,
    which needs no ``os.getpgid`` lookup and is one this run created. That flag
    is the whole of the route choice, so the direct-child route is derived from
    it here rather than passed alongside it: the two could otherwise contradict
    each other, describing a group signal delivered to a direct child.

    The name identifies a group this run made; it does not guarantee that group
    still exists or still holds the name. The caller reaches this function only
    after the settlement probe declined to veto, and
    :mod:`cuprum._process_group` records the reuse windows that leaves open.

    Signalling the group rather than the direct child is what lets a child's
    descendants reach end-of-file on inherited pipes;
    :class:`~cuprum.sh.ProcessGroupPolicy` documents what stays out of reach.

    The two ways a signal can fail to arrive are not the same. A
    ``ProcessLookupError`` is the race both routes already tolerate — the target
    went before the signal could be delivered — so it is passed over silently. A
    wider ``OSError`` is a target this run believes is there and could not
    signal, and silence there would conflate two outcomes that both reach the
    caller as the same ``False``: a teardown that found nothing to do, and one
    that failed. Only a record distinguishes them. The routes report an absent
    target differently too: ``os.killpg`` raises ``ProcessLookupError`` where
    :meth:`subprocess.Popen.send_signal` suppresses it (bpo-40550).

    A logging handler the application installed can still raise out of the
    ``warning`` call, which is left to propagate: the branch is already abnormal.

    Returns
    -------
    bool
        Whether the signal call returned without raising. That is weaker than
        "the signal was delivered", for the ``send_signal`` suppression above.
    """
    try:
        if owns_group and process.pid is not None:
            os.killpg(process.pid, group_signal)
        else:
            # ``terminate`` is SIGTERM and ``kill`` is SIGKILL, and *group_signal*
            # is only ever one of those two, so the direct route follows from the
            # signal rather than needing a caller to name the method as well.
            process.terminate() if group_signal == signal.SIGTERM else process.kill()
    except ProcessLookupError:
        return False
    except OSError as exc:
        report_group_teardown(
            observation,
            GroupTeardownReport.signal_refused(
                pid=process.pid,
                owns_group=owns_group,
                signal_number=group_signal,
            ),
            exc,
        )
        return False
    return True


__all__ = [
    "GroupTeardownReport",
    "report_group_teardown",
    "signal_target_or_report",
    "unsignalable_report",
]
