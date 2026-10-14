"""The closed vocabularies an execution event is described in.

Split from ``cuprum.events`` so that module holds the one payload type,
:class:`~cuprum.events.ExecEvent`, and its consumers can reach a vocabulary
without taking the event with it. The seam is real rather than arithmetic: a
phase, an outcome, and a mode are *labels*, and every one of them exists
because a filter somewhere keys on its exact string — a log query, a metric
label, a set of span attributes. ``ExecEvent`` is the record those labels ride
on, and the two change for different reasons: a new label is a new thing an
operator can filter on, while a new field is a new thing a producer must fill
in.

Every member here is stable, low-cardinality, and closed. That is the whole
point of the module: :data:`ExecPhase` is a ``Literal`` and the rest are
``StrEnum`` members, so a typo at a call site is a type error rather than a
value an operator's filter silently never matches. Nothing here may be built
from a runtime value.

``cuprum.events`` re-exports all of these, so ``cuprum.events.ExecPhase`` and
every other spelling that module published keeps working; the names are also
reachable from this module directly for callers that want a vocabulary without
importing the payload.

The one ordering rule is inherited from :class:`~cuprum.events.ExecEvent` and
recorded here because it is a property of the taxonomy rather than of the
record: ``pipeline_fail_fast`` is not a lifecycle phase of a command but a
decision the pipeline coordinator took *about* a stage, and ``group_teardown``
reports a fact about a run that has already finished rather than a step in it.
Members are therefore grouped by what they describe rather than appended in
the order they were introduced.
"""

from __future__ import annotations

import enum
import typing as typ
import uuid

# ``plan`` … ``stdin_error`` describe one command's own lifecycle.
# ``pipeline_fail_fast`` is different in kind: it reports a decision the
# pipeline coordinator took about a stage: its non-zero exit was the first
# failure and every other still-running stage — upstream producer and downstream
# consumer alike — is about to be torn down. The stage's own ``exit`` event
# still follows. ``group_teardown`` is different again: it reports that the
# teardown could not establish settlement, on either route, which is a fact
# about a run that has already finished rather than a phase of it.
type ExecPhase = typ.Literal[
    "plan",
    "start",
    "stdout",
    "stderr",
    "exit",
    "settled",
    "stdin",
    "stdin_error",
    "timeout",
    "teardown_error",
    "capture_eof_grace_expired",
    "pipeline_fail_fast",
    "group_teardown",
]


class GroupTeardownOutcome(enum.StrEnum):
    """Why a teardown could not establish settlement.

    A closed vocabulary because these values label a metric: an operator
    filters on the set of reasons a group was left behind, so every member is a
    stable, low-cardinality string that no runtime value can reach.

    ``SIGNAL_REFUSED`` is the one outcome emitted on both routes: it reports a
    signal the kernel refused rather than a count of surviving members.

    ``UNSIGNALABLE`` and ``SETTLEMENT_TIMEOUT`` both leave members running, and
    what separates them is what can still be done about it. An unsignalable
    group is outside this run's authority: nothing it sends reaches those
    members, so the teardown stops rather than waiting out a process it can
    never end. A settlement timeout was within reach in principle — the members
    were compelled with ``SIGKILL`` — and the capped wait expired because
    something outside this run owns their reaping. The first is a permissions
    fact about the group; the second is a race with an init process, and only
    the second is worth retrying.
    """

    UNSIGNALABLE = "unsignalable"
    SETTLEMENT_TIMEOUT = "settlement_timeout"
    SIGNAL_REFUSED = "signal_refused"


class TerminalOutcome(enum.StrEnum):
    """Why an observed execution reached its definitive terminal event.

    The values are shared with presentation sinks, so execution telemetry and
    output adapters classify the same outcome with the same bounded labels.
    """

    EXIT_ZERO = "exit_zero"
    EXIT_NONZERO = "exit_nonzero"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


# A stable, per-execution correlation token. It is minted once when an
# execution begins and propagated unchanged through every lifecycle event
# (``start``, ``stdout``, ``stderr``, ``exit``) for that execution, so
# consumers can correlate the events of a single execution even when the
# operating system recycles a process identifier across executions.
ExecId = typ.NewType("ExecId", uuid.UUID)

# The stable ``timeout_mode`` values, shared by the ``timeout`` observe event
# and the ``cuprum.timeout`` log records so a consumer keying on either sees
# the same two strings. ``elapsed_deadline`` is a positive wall-clock deadline
# that ran out; ``non_positive_immediate`` is a ``timeout <= 0`` that expired
# at once, without ever awaiting the process.
type TimeoutMode = typ.Literal["elapsed_deadline", "non_positive_immediate"]


# The stable ``resource_usage_mode`` values, naming how a terminal ``exit``
# event obtained the child resource figures it carries. They are shared by the
# observe event, the ``cuprum_`` log extras, the span attributes, and the
# metrics label, so a consumer keying on any of them sees the same strings.
#
# ``wait4_child`` is the attributable case: one reaped child's own usage, with
# ``max_rss_bytes`` populated. ``aggregate_cpu_delta`` is the CPU-only
# fallback, where ``RUSAGE_CHILDREN`` snapshots bracket the run and
# ``max_rss_bytes`` stays ``None`` because that high-water mark spans every
# reaped child. ``unavailable`` means the platform offers neither source.
class ResourceUsageMode(enum.StrEnum):
    """How a terminal ``exit`` event obtained the resource figures it carries.

    A closed set, like :class:`~cuprum.pump_events.RustPumpDeclineReason`: the
    value is a metric label and a log extra that operators filter on, so a
    typo at a new call site would produce a value their filters silently miss.
    As an enum it is a type error instead.

    Members are `str`, so the observe event, the ``cuprum_`` log extras, the
    span attributes, and the metrics label all keep carrying plain strings —
    the constant this replaced was a ``Literal`` of exactly these three values,
    and a member formats as its own value.

    Examples
    --------
    The member value is the string consumers see::

        assert ResourceUsageMode.WAIT4_CHILD == "wait4_child"

    """

    WAIT4_CHILD = "wait4_child"
    AGGREGATE_CPU_DELTA = "aggregate_cpu_delta"
    UNAVAILABLE = "unavailable"


# The stable ``capture_mode`` values, naming what a run's captures hold.
class CaptureMode(enum.StrEnum):
    """How a run's captured output is reported by the telemetry adapters.

    A closed set, like :class:`ResourceUsageMode`: the mode is a metric label
    and a log extra operators filter on, so a typo at a new call site would miss
    their filters rather than raise. It rides each event as a trusted field
    rather than an observation tag, for the reason ``env_mode`` does: a caller
    may shadow any tag key, and a mode a consumer cannot rely on is pointless.
    """

    TEXT = "text"
    BYTES = "bytes"


def new_exec_id() -> ExecId:
    """Return a fresh, process-unique execution correlation token.

    Returns
    -------
    ExecId
        A new :data:`ExecId` wrapping a random UUID (:func:`uuid.uuid4`),
        distinct on every call, used to correlate all lifecycle events of a
        single execution.
    """
    return ExecId(uuid.uuid4())


__all__ = [
    "CaptureMode",
    "ExecId",
    "ExecPhase",
    "GroupTeardownOutcome",
    "ResourceUsageMode",
    "TerminalOutcome",
    "TimeoutMode",
    "new_exec_id",
]
