"""Deterministic coverage of the identity guarantee teardown relies on.

`cuprum._process_signal` decides *what* a teardown signal is delivered to.
For an owning run that target is the group its child led, addressed by the
child's own process identifier. The kernel keeps that identifier reserved only
while a group holds a member, and once the group empties the number is an
ordinary free slot it may hand to anyone. The guarantee this module pins is
therefore deliberately one-way, and the weaker direction is the one that
matters:

- A probe that has stopped answering proves the number is unused, so it
  certainly is not this run's group. That is a sound **veto**: no signal may
  be sent.
- A probe that still answers proves only that *some* group holds the number.
  It cannot prove the group is this run's, because the original could have
  emptied and the kernel could have re-used the number **before** the probe
  ran. A live probe is necessary for a signal to be sound, never sufficient
  to make it so.

The second case is the residual these tests **witness** rather than forbid, and
it is easy to mistake for safety. The double behind them models the kernel
faithfully — it cannot tell whose group holds a number either, so it delivers
wherever the name answers — and the set of signalled names is read directly, so
no timing is involved.

What these tests pin is observable signalling behaviour, and that is a weaker
thing than it might look. A teardown that treats an answering probe as proof of
ownership sends *exactly* the same signals as one that treats it as a veto that
did not fire: same names, same order, same refusal when the probe goes dark.
The two readings differ in what they conclude, not in what they do, so no input
here can separate them and no assertion below fails under the wrong one. The
misreading is prevented by the documentation that states the direction, not by
these tests; what the tests establish is that the behaviour on the wire is the
behaviour the documentation describes.

The direct-child route is covered too, as the control. A run that did not
create a group has no identity to ask about, so it must reach its target
through the process handle and never through a group name.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from cuprum import _process_signal
from cuprum._process_group import _GroupProbe
from cuprum.unittests._process_signal_identity_support import (
    _LEADER_PID,
    _as_process,
    _teardown,
    _TeardownTarget,
)

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only


def test_reuse_by_an_unrelated_live_group_is_indistinguishable_from_our_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residual the probe cannot close, pinned as a witness rather than a claim.

    The sequence this models is the one the module docstring names and the one
    an "an answering probe proves ownership" reading misses entirely:

    1. this run's group empties after its leader is reaped,
    2. the kernel hands the number to an unrelated group,
    3. teardown probes — and that stranger's group is *live*, so the probe
       answers exactly as this run's own would,
    4. the signal under that name reaches the stranger.

    No reuse happens between the probe and the signal here; the reuse is already
    in the past by the time the probe runs. There is no lifetime-safe handle on
    a process group to close this — ``pidfd`` is per-pid — so the ambiguity is
    inherent to addressing a group by a number, not a defect this code can fix.

    That is why this test asserts the outcome rather than forbidding it. A
    teardown that refused to signal whenever it could not *prove* ownership
    would refuse always, since no probe can prove it, and would abandon the
    descendants the option exists to reclaim. What the code owes is an honest
    statement of the residual, and what this test owes is a witness: the
    stranger is signalled, with the probe as the only input consulted.

    The witness is of the signalling, not of the reasoning. An implementation
    that read the live probe as *proof* of ownership would send the same two
    signals to the same name, so nothing asserted below would change; what
    separates the readings is what they claim about the name, and the place to
    fix a wrong claim is the documentation that makes it.
    """
    stranger = _TeardownTarget(is_group_live=True)
    monkeypatch.setattr(os, "killpg", stranger.record_killpg)

    handled = asyncio.run(_teardown(stranger, owns_group=True))

    assert handled.delivered is True, (
        "teardown proceeds once the probe has answered, because a live answer "
        "does not veto; this is the documented residual, not a safe outcome"
    )
    assert stranger.probes, (
        "no liveness probe ran, so the signals below went out with nothing "
        "consulted at all"
    )
    assert stranger.probes[0] == _LEADER_PID, (
        "the probe must be asked about this run's own group name, even though "
        f"the answer cannot be attributed to this run; got {stranger.probes!r}"
    )
    assert stranger.group_signals == [
        (_LEADER_PID, signal.SIGTERM),
        (_LEADER_PID, signal.SIGKILL),
    ], (
        "a live probe cannot distinguish this run's group from a stranger's "
        "that inherited the number, so the signals go out under that name. "
        "This is the documented residual, not a safe outcome; got "
        f"{stranger.group_signals!r}"
    )


def test_the_escalation_is_not_attempted_at_a_name_that_emptied_in_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group that empties during the grace period is not escalated into.

    The last member of this run's group hangs on through the graceful signal —
    the whole reason there is a grace period — and exits as it runs out. Every
    poll the grace period took was answered live, so the escalation is reached
    on the strength of an answer that has since stopped being true.

    Asking again before the escalation costs one syscall and removes exactly
    that case: an absent name is conclusive, in a way a live one is not, and it
    is the only thing a fresh check *can* establish. Sending ``SIGKILL`` into a
    name nothing holds is at best a wasted syscall and at worst — once the
    kernel has handed the number on — a signal to a stranger's group.
    """
    target = _TeardownTarget(
        is_group_live=True,
        exits_on_sigterm=False,
        goes_dark_on_grace_expiry=True,
    )
    monkeypatch.setattr(os, "killpg", target.record_killpg)
    monkeypatch.setattr(asyncio, "sleep", target._sleep)

    handled = asyncio.run(_teardown(target, owns_group=True))

    assert target.attempts == [(_LEADER_PID, signal.SIGTERM)], (
        "the live group does not veto the graceful signal; the escalation is "
        "vetoed, because the group has emptied by the time it is considered "
        f"and a fresh probe says so; got {target.attempts!r}"
    )
    assert handled.escalated is True, (
        "the grace window ran out with the graceful signal already delivered, "
        f"so the ending is an escalated one; got {handled!r}"
    )
    assert handled.waiter_completed is False, (
        "the escalation was vetoed by the fresh probe, so no reaping wait "
        f"returned; got {handled!r}"
    )


def test_a_settled_group_reports_settlement_without_regard_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement is asked of the group, and a group with no members answers.

    The predicate behind both checks in :func:`_terminate_process_with_wait`,
    asked directly. It is worth pinning on its own because its two answers have
    opposite weights and the module docstring now says so: a group that answers
    *yes* is only ever evidence that some group holds the name, while a group
    that answers *no* is conclusive. The child here is deliberately **live**,
    so the group's answer is the only thing that can settle this run — an
    implementation that consulted the child instead would report the opposite.
    """
    target = _TeardownTarget(is_group_live=False, has_exited=False)
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    settled = _process_signal._settlement_of(
        _as_process(target),
        owns_group=True,
        target=_process_signal._SettlementTarget(
            is_done=lambda: target.returncode is not None,
            wait_for_exit=target.wait,
        ),
    )

    assert settled is _GroupProbe.EMPTY, (
        "an owning run is settled by its group, not by its child: the child "
        "here is still running, and the group has no members left, so there is "
        "nothing left for a teardown to reach"
    )
    assert target.probes == [_LEADER_PID], (
        f"the group's own name is what must be asked; got {target.probes!r}"
    )


def test_an_inherited_run_asks_the_child_rather_than_probing_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inherited route keeps the child's answer, and never probes a name.

    Control for the test above, and the reason the predicate branches at all. A
    run that did not create its group has no name of its own to ask about:
    probing would ask whether *some* group holds the child's pid, which for an
    inherited child is the caller's own group — a question with no bearing on
    whether this child has exited, and one whose answer would be yes for as
    long as the caller lives.
    """
    target = _TeardownTarget(is_group_live=True, has_exited=False)
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    settled = _process_signal._settlement_of(
        _as_process(target),
        owns_group=False,
        target=_process_signal._SettlementTarget(
            is_done=lambda: target.returncode is not None,
            wait_for_exit=target.wait,
        ),
    )

    assert settled is _GroupProbe.MEMBERS, (
        "an inherited run is settled by its child's exit, and this child has "
        "not exited, so the run is not settled however many groups hold its pid"
    )
    assert not target.probes, (
        "an inherited run has no group of its own to ask about; probing the "
        f"child's pid answers for whichever group holds it; got {target.probes!r}"
    )


def test_an_emptied_group_is_never_signalled_under_its_recycled_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that no longer answers is not this run's group, so it is left alone.

    This is the stale-identity hazard stated as a test. The group has emptied
    and the kernel may have handed the identifier to a stranger, so the probe
    answers with nothing and no signal may follow — under either the graceful
    phase or the escalation. Signalling here would reach a group this run never
    created, which is the one thing group addressing must not do.
    """
    target = _TeardownTarget(is_group_live=False)
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    handled = asyncio.run(_teardown(target, owns_group=True))

    assert handled.delivered is False, (
        "a group whose probe does not answer has already settled, so teardown "
        "must report that it signalled nothing"
    )
    assert not target.group_signals, (
        "no signal may be sent under a recycled identifier; got "
        f"{target.group_signals!r}"
    )
    assert not target.direct_signals, (
        "an owning run reaches its target through the group, and the group is "
        f"gone, so the direct route must not be substituted; got "
        f"{target.direct_signals!r}"
    )
    assert target.probes == [_LEADER_PID], (
        "the decision must rest on the liveness probe for this run's own group "
        f"name; got {target.probes!r}"
    )


def test_a_live_group_is_probed_before_it_is_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the probe answers, teardown proceeds under this run's group name.

    This is the ordinary path rather than a safety proof. A group that still
    holds a member keeps its number, so the probe answers — but the answer
    cannot attribute the name to this run, as the reuse test above shows: the
    original could have emptied and the number been recycled before the probe
    ran. What the implementation relies on is only the negative direction. An
    answering probe does not veto, so teardown continues, and it does so under
    the name it asked about.

    Ordering matters for a different reason than safety. The probe comes first
    because it is the only chance to point at a name that is still held; a
    probe issued after the signal would be reporting on a group this run had
    already asked to exit.

    The leader here has already been reaped, which is the case a
    direct-child-only teardown would mistake for settlement.
    """
    target = _TeardownTarget(is_group_live=True)
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    handled = asyncio.run(_teardown(target, owns_group=True))

    assert handled.delivered is True, "a live group must be signalled and awaited"
    assert target.probes, (
        "no liveness probe ran, so the signal below had no veto consulted before it"
    )
    assert target.probes[0] == _LEADER_PID, (
        "the liveness probe must run before any signal is sent, so a name is "
        f"never used on the strength of its history alone; got {target.probes!r}"
    )
    assert target.group_signals == [
        (_LEADER_PID, signal.SIGTERM),
        (_LEADER_PID, signal.SIGKILL),
    ], (
        "the group must be asked to exit and then compelled, both under this "
        f"run's own identifier; got {target.group_signals!r}"
    )
    assert not target.direct_signals, (
        "an owned target is reached through its group, never the direct route; "
        f"got {target.direct_signals!r}"
    )
    assert target.waits >= 1, "the direct child must still be reaped"


def test_an_inherited_run_never_probes_or_signals_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default policy keeps the direct-child route, group untouched.

    The control for the two tests above. A run that did not create a group has
    no identity to check and nothing to address by group name, so neither the
    probe nor a group signal may be reached — the process handle is the whole
    target, exactly as it was before ownership existed.

    The child is `SIGTERM`-immune so both phases of the direct route run, the
    escalation included. That is what makes "no group was used" a statement
    about both signals rather than only the first.
    """
    target = _TeardownTarget(
        is_group_live=True,
        has_exited=False,
        exits_on_sigterm=False,
    )
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    handled = asyncio.run(_teardown(target, owns_group=False))

    assert handled.delivered is True, "the direct child is still signalled and awaited"
    assert not target.probes, (
        f"an inherited run has no group identity to ask about; got {target.probes!r}"
    )
    assert not target.group_signals, (
        f"an inherited run owns no group to signal; got {target.group_signals!r}"
    )
    assert target.direct_signals == ["terminate", "kill"], (
        "the direct child must be asked to exit and then compelled, through "
        f"the process handle itself; got {target.direct_signals!r}"
    )
