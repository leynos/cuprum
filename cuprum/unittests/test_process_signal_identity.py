"""Deterministic coverage of the identity guarantee teardown relies on.

`cuprum._process_signal` decides *what* a teardown signal is delivered to.
For an owning run that target is the group its child led, addressed by the
child's own process identifier. The kernel keeps that identifier reserved
only while the group holds a member, so the guarantee this module asserts is
deliberately one-way and must be pinned as such:

- A probe that still answers proves the name is still this run's group, so
  teardown may signal it. That is the direction the implementation relies on.
- A probe that has stopped answering proves only that the group emptied. The
  identifier is then an ordinary free slot the kernel may have handed to an
  unrelated group, so no signal may be sent under it.

The second case is the defect these tests exist to catch. A teardown that
signalled on the strength of "the name was ours when we started" rather than
on a live probe would aim `killpg` at whatever group now holds that number —
someone else's processes. The doubles here make that deterministic: the
probe's answer is the whole input, so the set of signalled names is read
directly and no timing is involved.

The direct-child route is covered too, as the control. A run that did not
create a group has no identity to ask about, so it must reach its target
through the process handle and never through a group name.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import typing as typ

import pytest

from cuprum import _process_signal
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# The identifier a hypothetical recycled group now holds. Any value proves the
# point; this one is chosen so a signalled name is unmistakable in a failure
# message, because it is not the leader's pid below.
_RECYCLED_PGID = 4242

# The direct child's pid, which is also the name of the group it led: a child
# spawned with `start_new_session=True` is its own group's leader, so the group
# identifier equals its process identifier.
_LEADER_PID = 7005


class _TeardownTarget:
    """A process handle whose group identity and liveness are scripted.

    Three things vary independently, which is what lets one double cover the
    stale-identity hazard, the live group it is contrasted with, and the
    inherited control:

    *is_group_live*
        What the liveness probe answers. ``False`` models a group that has
        emptied and whose identifier the kernel may since have recycled —
        the hazard. ``True`` models a group that still holds a member, so the
        name is still this run's.
    *has_exited*
        Whether the direct child has already been reaped. An owning run must
        still reach the group it led; an inheriting run has nothing left to do.
    *exits_on_sigterm*
        Whether a graceful signal is enough. ``False`` models a `SIGTERM`-
        immune child, which is what makes the escalation observable rather
        than letting the grace phase finish the job.
    """

    def __init__(
        self,
        *,
        is_group_live: bool,
        has_exited: bool = True,
        exits_on_sigterm: bool = True,
    ) -> None:
        self.pid: int | None = _LEADER_PID
        self.returncode: int | None = 0 if has_exited else None
        self.is_group_live = is_group_live
        self.exits_on_sigterm = exits_on_sigterm
        self.group_signals: list[tuple[int, int]] = []
        self.direct_signals: list[str] = []
        self.probes: list[int] = []
        self.waits = 0
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        """Record the graceful direct signal, exiting only if not immune."""
        self.direct_signals.append("terminate")
        if self.exits_on_sigterm:
            self._record_exit(-15)

    def kill(self) -> None:
        """Record the direct escalation, which always exits."""
        self.direct_signals.append("kill")
        self._record_exit(-9)

    async def wait(self) -> int:
        """Await the child's exit, as the real reap does."""
        self.waits += 1
        if self.returncode is None:
            await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def record_killpg(self, pgid: int, sig: int) -> None:
        """Probe or signal the named group, recording each attempt.

        ``sig == 0`` is an existence probe: it answers for the group's liveness
        and delivers nothing. Any other signal is delivered only while the name
        still answers, and is refused otherwise — a signal delivered under an
        identifier that is no longer this run's is the defect under test.
        """
        if sig == 0:
            self._record_probe(pgid)
            return
        self._require_live_group(pgid)
        self.group_signals.append((pgid, sig))
        self._apply_signal_effect(sig)

    def _record_probe(self, pgid: int) -> None:
        """Record the existence probe, refusing a name that stopped answering."""
        self.probes.append(pgid)
        self._require_live_group(pgid)

    def _require_live_group(self, pgid: int) -> None:
        """Raise unless the group named *pgid* still answers."""
        if not self.is_group_live:
            raise ProcessLookupError(pgid, "No such process")

    def _apply_signal_effect(self, sig: int) -> None:
        """Model a delivered group signal reaching the child that leads it.

        The direct child is a member of its own group, so a delivered group
        signal kills it too. Modelling that keeps a signal sent down the wrong
        route from presenting as a hang: the child exits and the assertions
        upstream report which route was taken instead of a bare timeout.
        """
        if sig == signal.SIGTERM and self.exits_on_sigterm:
            self._record_exit(-15)
        if sig == signal.SIGKILL:
            self._record_exit(-9)
            self.is_group_live = False

    def _record_exit(self, code: int) -> None:
        """Publish an exit code and release any waiter."""
        if self.returncode is None:
            self.returncode = code
        self._exited.set()


def _as_process(target: _TeardownTarget) -> asyncio.subprocess.Process:
    """Present the double as the process handle teardown expects."""
    return typ.cast("asyncio.subprocess.Process", target)


def _teardown(
    target: _TeardownTarget,
    *,
    owns_group: bool,
) -> cabc.Coroutine[None, None, bool]:
    """Tear the target down under a policy carrying *owns_group*."""

    async def wait_for_exit() -> int:
        """Await the direct child's exit, as the real teardown does."""
        return await target.wait()

    return _process_signal._terminate_process_with_wait(
        _as_process(target),
        policy=_TeardownPolicy(0.05, owns_group=owns_group),
        is_done=lambda: target.returncode is not None,
        wait_for_exit=wait_for_exit,
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

    assert handled is False, (
        "a group whose probe does not answer has already settled, so teardown "
        "must report that it signalled nothing"
    )
    assert not target.group_signals, (
        f"no signal may be sent under a recycled identifier; got "
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
    """While the probe answers, the name is still this run's and is signalled.

    The converse of the test above, and the direction the implementation
    actually relies on: a group that still holds a member cannot have had its
    identifier recycled, so signalling it reaches this run's own descendant
    rather than a stranger. Ordering matters as much as the outcome — the
    probe has to come first, because it is what licenses the signal.

    The leader here has already been reaped, which is the case a
    direct-child-only teardown would mistake for settlement.
    """
    target = _TeardownTarget(is_group_live=True)
    monkeypatch.setattr(os, "killpg", target.record_killpg)

    handled = asyncio.run(_teardown(target, owns_group=True))

    assert handled is True, "a live group must be signalled and awaited"
    assert target.probes, (
        "no liveness probe ran, so the signal below was not licensed by one"
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

    assert handled is True, "the direct child is still signalled and awaited"
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
