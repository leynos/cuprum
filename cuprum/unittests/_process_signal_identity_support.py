"""The scripted teardown double behind the identifier-guarantee test module.

`_TeardownTarget` presents a process handle whose group liveness and group
ownership are scripted independently, which is what lets the identity tests
tell "this run's group still answers" apart from "some group holds this
number". It lives here so the test module holds only the guarantees it pins.

The kernel's answer about a group is about a *number*, and it can never say
whose group holds it. That asymmetry is the whole subject of the module that
imports this one, so the double models it literally: liveness is what the
kernel reports, and ownership is what it does not report at all. A signal is
therefore delivered wherever the name answers, exactly as `killpg` behaves, and
the tests read the recorded names rather than the double's intent.

A double that called `asyncio.sleep` by name would recurse into itself once a
test patches that name, so the real function is captured here at import time.
"""

from __future__ import annotations

import asyncio
import signal
import typing as typ

from cuprum import _process_signal
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation

# The direct child's pid, which is also the name of the group it led: a child
# spawned with `start_new_session=True` is its own group's leader, so the group
# identifier equals its process identifier.
_LEADER_PID = 7005

# Taken before any test patches ``asyncio.sleep``, so the double that intercepts
# the group poller can still wait for real.
_REAL_SLEEP = asyncio.sleep


class _TeardownTarget:
    """A process handle whose group identity and liveness are scripted.

    Four things vary independently, which is what lets one double cover the
    stale-identity hazard, the live group it is contrasted with, the
    number-reused-by-a-stranger hazard, and the inherited control:

    *is_group_live*
        What the liveness probe answers. ``False`` models a group that has
        emptied and whose identifier no longer answers at all. ``True`` models
        a name that still answers — but on its own that does **not** say whose
        group it is, which is why the tests must read the signals rather than
        trust a live probe.
    *has_exited*
        Whether the direct child has already been reaped. An owning run must
        still reach the group it led; an inheriting run has nothing left to do.
    *exits_on_sigterm*
        Whether a graceful signal is enough. ``False`` models a `SIGTERM`-
        immune child, which is what makes the escalation observable rather
        than letting the grace phase finish the job.
    *goes_dark_on_grace_expiry*
        Whether the group disappears at the instant the grace period runs out,
        as its last member finally exits. This is the state a fresh settlement
        check exists to notice: every poll during the grace answered live, and
        by the time the escalation is considered the name answers for nobody.

        The transition is anchored to the deadline because that is the only
        instant at which it is deterministic. A group that went dark *during*
        the grace would be noticed by the poller and end the grace phase
        normally, so the escalation would never be reached and the check would
        go unexercised; a group that went dark on a probe count would make the
        test's outcome depend on how many polls the interval backoff fitted
        into the window.

    There is deliberately no ownership flag. A flag would let a test assert
    that the double *declined* to signal a stranger, which is precisely the
    behaviour the real kernel cannot produce: it takes a number and delivers to
    whoever holds it. The hazard the module pins is that an answering probe
    cannot tell the two apart, so the double must not be able to either.
    """

    def __init__(
        self,
        *,
        is_group_live: bool,
        has_exited: bool = True,
        exits_on_sigterm: bool = True,
        goes_dark_on_grace_expiry: bool = False,
    ) -> None:
        """Record the group's initial state and start with nothing observed."""
        self.pid: int | None = _LEADER_PID
        self.returncode: int | None = 0 if has_exited else None
        self.is_group_live = is_group_live
        self.exits_on_sigterm = exits_on_sigterm
        self.goes_dark_on_grace_expiry = goes_dark_on_grace_expiry
        self.group_signals: list[tuple[int, int]] = []
        # Every signal *attempted* under a group name, recorded before the
        # liveness check decides whether it can be delivered. An attempt into a
        # name that answers for nobody is the thing that reaches a stranger, so
        # it has to be observable even though nothing is delivered.
        self.attempts: list[tuple[int, int]] = []
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
        and delivers nothing. Any other signal is delivered whenever the name
        answers at all — which is the point. The kernel has no way to say
        *whose* group answers, so a signal aimed at a number the kernel has
        since handed to a stranger's group is delivered to that stranger. The
        double models exactly that: delivery follows liveness alone.
        """
        if sig == 0:
            self._record_probe(pgid)
            return
        self.attempts.append((pgid, sig))
        self._require_live_group(pgid)
        self.group_signals.append((pgid, sig))
        self._apply_signal_effect(sig)

    def _record_probe(self, pgid: int) -> None:
        """Record the existence probe, refusing a name that stopped answering."""
        self.probes.append(pgid)
        self._require_live_group(pgid)

    async def _sleep(self, seconds: float) -> None:
        """Wait one probe interval, going dark if cancellation lands on it.

        A probe interval is where the grace period ends. The deadline cancels
        the group poller mid-sleep, so the last sleep of a run is the one the
        deadline falls in, and a group scripted to go dark does so here. That
        puts the transition exactly on the deadline: every probe the grace
        period took was answered live, so the escalation is reached, and the
        name answers for nobody by the time the escalation is considered.

        Raises
        ------
        asyncio.CancelledError
            Always, when the group is cancelled at the deadline. The poller
            cancels this sleep as the grace period ends; the transition is
            applied before the cancellation is re-raised so it holds whatever
            the poller does next.
        """
        try:
            await _REAL_SLEEP(seconds)
        except asyncio.CancelledError:
            if self.goes_dark_on_grace_expiry:
                self.is_group_live = False
            raise

    def _require_live_group(self, pgid: int) -> None:
        """Raise unless the group named *pgid* still answers.

        An answering name is all the kernel reports. It does not report whether
        the group behind it is the one this run created.

        Raises
        ------
        ProcessLookupError
            When the scripted group has no members left. This is the kernel's
            own answer for an absent name, and modelling it exactly is what
            lets the probe's veto be observed rather than assumed.
        """
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
    observation: _StageObservation | None = None,
) -> cabc.Coroutine[None, None, bool]:
    """Tear the target down under a policy carrying *owns_group*.

    *observation* is the execution the teardown reports its outcomes against.
    It defaults to ``None`` because most cases here read the log record or the
    signals the double received, and the reporter skips the event half entirely
    for an absent observation. A case that wants the metric a teardown produces
    has to supply the execution it belongs to.

    Returns
    -------
    cabc.Coroutine of (None, None, bool)
        The teardown awaiting delivery, handing the result back to whichever
        loop runs it.
    """

    async def wait_for_exit() -> int:
        """Await the direct child's exit, as the real teardown does."""
        return await target.wait()

    return _process_signal._terminate_process_with_wait(
        _as_process(target),
        policy=_TeardownPolicy(0.05, owns_group=owns_group),
        target=_process_signal._SettlementTarget(
            is_done=lambda: target.returncode is not None,
            wait_for_exit=wait_for_exit,
        ),
        observation=observation,
    )
