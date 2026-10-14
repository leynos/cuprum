"""What ``signal()`` reports when it cannot deliver, and why one refusal wins.

Every case here asserts the same thing from a different angle: a signal that was
not delivered must still tell the caller *why*, precisely enough to choose what
to do next. The handle has three ways to fail a delivery — the child is already
gone, someone else collected its status first, or the platform has no route to a
caller-named signal — and each carries different facts.

The platform refusal is reported ahead of the child-state refusal, and that
ordering is asserted here rather than left to inspection, because only one of
the two is actionable: a caller told the child had exited gives up, while a
caller told the platform cannot take the request knows to reach for ``terminate``
or ``kill``.

The doubles these cases drive — one that is already gone, one whose status is
published during the delivery attempt, and the thin recorder they build on —
live in :mod:`cuprum.unittests._running_command_support`, alongside the ones the
other handle modules use.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import pytest

from cuprum.sh import (
    SignalAfterExitError,
    UnsupportedSignalError,
)
from cuprum.unittests._running_command_support import (
    _EARLY_EXIT,
    _ConcurrentlyReapedProcess,
    _handle,
    _StubProcess,
)


def test_signal_after_exit_names_the_recorded_code() -> None:
    """Signalling an exited child reports how it ended rather than a bare race.

    A caller coordinating several signals cannot avoid racing the child's own
    exit, so the refusal carries the exit code the handle recorded and the PID
    it belonged to, and nothing is delivered.
    """

    async def run_case() -> None:
        """Attempt a signal after the child has already exited."""
        process = _StubProcess(pid=4323)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)

        with pytest.raises(SignalAfterExitError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.exit_code == _EARLY_EXIT, (
            f"the refusal must carry the child's status {_EARLY_EXIT}, "
            f"got {raised.value.exit_code}"
        )
        assert raised.value.pid == process.pid, (
            "the refusal must name the process it could not reach"
        )
        assert not process.signals, "nothing may be delivered to a gone child"

    asyncio.run(run_case())


def test_signal_refused_after_a_concurrent_reap_names_the_real_code() -> None:
    """A delivery refused for a reaped child still reports how it ended.

    The probe catches a child that exited but has not been reaped. Once
    *someone else* has collected the status — a settle, or a caller's own
    ``wait()`` that started first — the kernel has no child at that PID and
    the probe can no longer observe the ending, so the delivery's own refusal
    is the only signal that the child is gone. The handle still has to name
    the exit code, and the value the collecting reaper published is where it
    comes from.

    This is the case a suppressed ``ProcessLookupError`` would hide: the
    delivery would read as success and the caller would believe a signal
    reached a process that had already been collected. The refusal is asserted
    to carry the real code rather than the ``-1`` placeholder, because a
    caller deciding whether the command's ending already satisfies them needs
    the actual status. The case is witnessed rather than arranged: the double
    records the attempt, and the kernel refused it, so the recording is intent
    rather than delivery. If a probe short-circuited the call the record would
    be empty, the code would be read rather than reported from a refusal, and
    the branch under test would go unexercised while still satisfying every
    assertion about the error.
    """

    async def run_case() -> None:
        """Attempt a signal as a concurrent reaper collects the child."""
        process = _ConcurrentlyReapedProcess(exit_code=_EARLY_EXIT)
        handle = _handle(process)

        with pytest.raises(SignalAfterExitError) as raised:
            await handle.signal(signal.SIGTERM)

        assert process.signals == [signal.SIGTERM], (
            "the delivery must have been attempted with the requested signal, "
            "or the refusal came from the probe and the suppressed-error "
            f"branch this case exists to cover was never reached (got "
            f"{process.signals})"
        )
        assert raised.value.exit_code == _EARLY_EXIT, (
            "the refusal must carry the reaped child's real status "
            f"{_EARLY_EXIT}, not the -1 placeholder, got "
            f"{raised.value.exit_code}"
        )
        assert raised.value.pid == process.pid, (
            "the refusal must name the process it could not reach"
        )

    asyncio.run(run_case())


def test_signal_is_refused_when_platform_cannot_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no route to the signal is named, and nothing is sent.

    The refusal carries both facts a caller needs to choose what to do next —
    the signal that was asked for and the platform that declined — so they
    never have to re-read the handle to find out why it was refused. The
    platform is compared against :data:`sys.platform` rather than merely
    checked for truth: a caller reading a log wants the spelling they would
    grep for, and a bare ``os.name`` reads ``"nt"`` where the documented answer
    is ``"win32"``.
    """

    async def run_case() -> None:
        """Attempt a signal the platform cannot deliver."""
        process = _StubProcess(pid=4324)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the refusal must name the signal that was asked for"
        )
        assert raised.value.platform_name == sys.platform, (
            "the refusal must name the platform as sys.platform spells it, got "
            f"{raised.value.platform_name!r}"
        )
        assert not process.signals, "an undeliverable signal must not be sent"
        assert process.returncode is None, "the child must be left running"

    asyncio.run(run_case())


def test_platform_refusal_is_reported_ahead_of_the_child_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gone child on a restricted platform reports the platform, not the race.

    Both refusals describe a signal that was not delivered, but only one of
    them is actionable: a caller told the child had exited gives up, while a
    caller told the platform cannot take the request knows to reach for
    ``terminate`` or ``kill`` instead. The platform is therefore checked first,
    and this case is what holds that ordering in place — with a child that has
    already exited, a swap of the two checks would silently report the race.
    """

    async def run_case() -> None:
        """Attempt a signal where both refusals apply."""
        process = _StubProcess(pid=4325)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the platform's refusal must be the one reported"
        )

    asyncio.run(run_case())


def _platform_cannot_deliver() -> bool:
    """Report that this platform cannot deliver a caller-named signal."""
    return False
